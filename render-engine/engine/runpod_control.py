"""Pod lifecycle, with termination that actually happens.

A pod bills for wall-clock time whether or not work is running. The expensive failure is
not a crashed batch — it is a batch that crashes and leaves the GPU spinning until someone
notices. So every path out of `session()` terminates, including exceptions and Ctrl-C.

Built on the official `runpod` SDK rather than raw REST v1 on purpose: v1 stops serving
traffic on 2026-11-15 and pod CRUD has no published migration path yet. When RunPod moves
it to v2, that is a version bump here instead of a rewrite.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterator

from .config import REPO_ROOT, RUNPOD, RunPodConfig

# Written so `terminate` can clean up without being told an id — including after the
# process that created the pod has died.
STATE_FILE = REPO_ROOT / ".active-pod.json"


class RunPodError(RuntimeError):
    pass


def _sdk():
    try:
        import runpod
    except ImportError as exc:  # pragma: no cover
        raise RunPodError("pip install runpod") from exc

    if not RUNPOD.api_key:
        raise RunPodError("RUNPOD_API_KEY is not set (see .env.example)")
    runpod.api_key = RUNPOD.api_key
    return runpod


def _remember(pod: dict[str, Any]) -> None:
    STATE_FILE.write_text(
        json.dumps({"id": pod["id"], "name": pod.get("name"), "created": time.time()}, indent=2),
        encoding="utf-8",
    )


def _forget() -> None:
    STATE_FILE.unlink(missing_ok=True)


def active_pod_id() -> str | None:
    if not STATE_FILE.is_file():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("id")
    except (json.JSONDecodeError, OSError):
        return None


def create(config: RunPodConfig = RUNPOD) -> dict[str, Any]:
    runpod = _sdk()
    if not config.network_volume_id:
        raise RunPodError(
            "RUNPOD_NETWORK_VOLUME_ID is not set. Without the volume the pod would "
            "re-download ~50-75 GB of weights on every start, on paid GPU time."
        )

    # 8188 is ComfyUI, 22 is SSH. Expose 8188 over http only while setting up by hand;
    # once the tunnel from RUNBOOK step 6 works, an unauthenticated ComfyUI on a public
    # hostname is the whole attack surface, so drop it.
    pod = runpod.create_pod(
        name=config.pod_name,
        image_name=config.image,
        gpu_type_id=config.gpu_type_id,
        gpu_count=1,
        network_volume_id=config.network_volume_id,
        volume_mount_path=config.volume_mount_path,
        ports="8188/http,22/tcp",
    )
    if not isinstance(pod, dict) or "id" not in pod:
        raise RunPodError(f"unexpected create_pod response: {pod!r}")

    _remember(pod)
    return pod


def wait_ready(pod_id: str, *, timeout_s: int = 900) -> dict[str, Any]:
    runpod = _sdk()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pod = runpod.get_pod(pod_id)
        if (pod or {}).get("desiredStatus") == "RUNNING" and (pod or {}).get("runtime"):
            return pod
        time.sleep(5)
    raise RunPodError(f"pod {pod_id} was not RUNNING within {timeout_s}s")


def terminate(pod_id: str | None = None, *, quiet: bool = False) -> bool:
    """Terminate and stop billing. Safe to call twice; safe to call on a dead pod."""
    pod_id = pod_id or active_pod_id()
    if not pod_id:
        if not quiet:
            print("no active pod recorded")
        return False

    try:
        _sdk().terminate_pod(pod_id)
        if not quiet:
            print(f"terminated {pod_id}")
        return True
    except Exception as exc:  # noqa: BLE001 - never let cleanup raise
        # Loud, because the consequence of a missed termination is a running meter.
        print(
            f"!! FAILED TO TERMINATE {pod_id}: {exc}\n"
            f"!! The pod may still be billing. Terminate it by hand at "
            f"https://console.runpod.io/pods",
            file=sys.stderr,
        )
        return False
    finally:
        _forget()


@contextlib.contextmanager
def session(config: RunPodConfig = RUNPOD) -> Iterator[dict[str, Any]]:
    """Create a pod, yield it, and terminate it no matter how the block exits."""
    pod = create(config)
    print(f"pod {pod['id']} starting")
    try:
        yield wait_ready(pod["id"])
    except KeyboardInterrupt:
        print("\ninterrupted — terminating pod before exit", file=sys.stderr)
        raise
    finally:
        terminate(pod["id"])


def _main() -> int:
    parser = argparse.ArgumentParser(description="RunPod lifecycle for the render engine")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create", help="create a pod and print its id")
    sub.add_parser("status", help="show the recorded active pod")
    terminate_parser = sub.add_parser("terminate", help="terminate the active pod")
    terminate_parser.add_argument("--pod-id", help="override the recorded pod id")

    args = parser.parse_args()

    if args.command == "create":
        pod = create()
        ready = wait_ready(pod["id"])
        print(json.dumps({"id": ready["id"], "status": ready.get("desiredStatus")}, indent=2))
        print("\nRemember to terminate when done:  python -m engine.runpod_control terminate")
        return 0

    if args.command == "status":
        pod_id = active_pod_id()
        print(pod_id or "no active pod recorded")
        return 0

    return 0 if terminate(args.pod_id) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
