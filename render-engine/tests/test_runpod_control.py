"""Termination guarantees for the pod lifecycle.

The expensive failure mode is not a crashed batch — it is a crashed batch that leaves a
GPU billing until somebody notices. These tests pin the guarantee that every exit path
from `session()` terminates, using a fake SDK so nothing is provisioned.
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from engine import runpod_control
from engine.config import RunPodConfig


class FakeSDK:
    def __init__(self):
        self.api_key = None
        self.created: list[dict] = []
        self.terminated: list[str] = []
        self.fail_terminate = False

    def create_pod(self, **kwargs):
        pod = {"id": f"pod-{len(self.created)}", "name": kwargs.get("name"), "kwargs": kwargs}
        self.created.append(pod)
        return pod

    def get_pod(self, pod_id):
        return {"id": pod_id, "desiredStatus": "RUNNING", "runtime": {"uptimeInSeconds": 5}}

    def terminate_pod(self, pod_id):
        if self.fail_terminate:
            raise RuntimeError("runpod api unreachable")
        self.terminated.append(pod_id)


CONFIG = RunPodConfig(
    api_key="test-key",
    network_volume_id="vol-123",
    gpu_type_id="NVIDIA GeForce RTX 5090",
)


class RunPodControlTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSDK()
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_sdk = runpod_control._sdk
        self._orig_state = runpod_control.STATE_FILE
        runpod_control._sdk = lambda: self.fake
        runpod_control.STATE_FILE = self.tmp / ".active-pod.json"

    def tearDown(self):
        runpod_control._sdk = self._orig_sdk
        runpod_control.STATE_FILE = self._orig_state

    def test_create_requires_network_volume(self):
        # Without the volume a pod re-downloads ~50-75 GB on every start, on paid GPU
        # time. Failing loudly here is cheaper than discovering it from an invoice.
        bare = RunPodConfig(api_key="k", network_volume_id=None)
        with self.assertRaises(runpod_control.RunPodError) as ctx:
            runpod_control.create(bare)
        self.assertIn("weights", str(ctx.exception))

    def test_create_records_active_pod(self):
        pod = runpod_control.create(CONFIG)
        self.assertEqual(runpod_control.active_pod_id(), pod["id"])
        self.assertEqual(self.fake.created[0]["kwargs"]["network_volume_id"], "vol-123")

    def test_session_terminates_on_success(self):
        with runpod_control.session(CONFIG) as pod:
            self.assertEqual(pod["desiredStatus"], "RUNNING")
        self.assertEqual(self.fake.terminated, ["pod-0"])

    def test_session_terminates_on_exception(self):
        with self.assertRaises(ZeroDivisionError):
            with runpod_control.session(CONFIG):
                raise ZeroDivisionError("batch blew up")
        self.assertEqual(self.fake.terminated, ["pod-0"])

    def test_session_terminates_on_keyboard_interrupt(self):
        with self.assertRaises(KeyboardInterrupt):
            with runpod_control.session(CONFIG):
                raise KeyboardInterrupt
        self.assertEqual(self.fake.terminated, ["pod-0"])

    def test_terminate_clears_state_even_when_api_fails(self):
        runpod_control.create(CONFIG)
        self.fake.fail_terminate = True
        self.assertFalse(runpod_control.terminate())
        # State is cleared regardless, so a retry is not blocked by a stale record —
        # but the failure is reported so a human can kill it in the console.
        self.assertIsNone(runpod_control.active_pod_id())

    def test_terminate_without_active_pod_is_noop(self):
        self.assertFalse(runpod_control.terminate(quiet=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
