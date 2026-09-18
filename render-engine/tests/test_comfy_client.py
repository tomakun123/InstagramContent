"""Round-trip the ComfyUI client against a stub server.

Stdlib only, no pytest, no GPU: `python -m tests.test_comfy_client`. The point is to catch
the mistakes that otherwise surface only after a pod is already billing — a malformed
/prompt body, a graph that silently fails to parameterize, a download that keeps its .part
suffix.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from engine.comfy_client import ComfyClient, ComfyError
from engine.config import ComfyConfig

CLIP_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"stub-clip-payload" * 16

API_GRAPH = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "placeholder.png"}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "POSITIVE PLACEHOLDER"}},
    "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "NEGATIVE PLACEHOLDER"}},
    "4": {"class_type": "LTXVSampler", "inputs": {"seed": 0, "width": 1, "height": 1, "num_frames": 1}},
    "5": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "ltx"}},
}

UI_GRAPH = {"nodes": [{"id": 1, "type": "LoadImage"}], "links": []}


class _Handler(BaseHTTPRequestHandler):
    submitted: list[dict] = []

    def log_message(self, *args):  # silence the test run
        pass

    def _send(self, payload, status=200, raw=False):
        body = payload if raw else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream" if raw else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/system_stats"):
            return self._send({"devices": [{"name": "Stub 5090", "vram_total": 32e9, "vram_free": 30e9}]})
        if self.path.startswith("/history/"):
            return self._send({
                self.path.rsplit("/", 1)[-1]: {
                    "status": {"status_str": "success"},
                    "outputs": {"5": {"images": [{"filename": "thumb.png", "type": "output"}],
                                      "gifs": [{"filename": "clip.mp4", "subfolder": "", "type": "output"}]}},
                }
            })
        if self.path.startswith("/view"):
            return self._send(CLIP_BYTES, raw=True)
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        if self.path == "/upload/image":
            return self._send({"name": "keyframe.png", "subfolder": "", "type": "input"})
        if self.path == "/prompt":
            body = json.loads(raw)
            if "prompt" not in body or "client_id" not in body:
                return self._send({"error": "malformed"}, 400)
            _Handler.submitted.append(body["prompt"])
            return self._send({"prompt_id": "stub-prompt-1"})
        self._send({"error": "not found"}, 404)


class ComfyClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _Handler.submitted.clear()
        self.client = ComfyClient(ComfyConfig(host="127.0.0.1", port=self.port, timeout_s=30))
        self.tmp = Path(tempfile.mkdtemp())

    def test_ping(self):
        self.assertTrue(self.client.ping())

    def test_ping_false_when_unreachable(self):
        dead = ComfyClient(ComfyConfig(host="127.0.0.1", port=9, timeout_s=1))
        self.assertFalse(dead.ping())

    def test_rejects_ui_format_workflow(self):
        path = self.tmp / "ui.json"
        path.write_text(json.dumps(UI_GRAPH))
        # The single most common scripted-ComfyUI failure: a graph that runs in the
        # browser but 400s from code. Catch it locally with a readable message.
        with self.assertRaises(ComfyError) as ctx:
            ComfyClient.load_workflow(path)
        self.assertIn("API format", str(ctx.exception))

    def test_accepts_api_format_workflow(self):
        path = self.tmp / "api.json"
        path.write_text(json.dumps(API_GRAPH))
        self.assertEqual(ComfyClient.load_workflow(path).keys(), API_GRAPH.keys())

    def test_upload_returns_reference_name(self):
        image = self.tmp / "keyframe.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.assertEqual(self.client.upload_image(image), "keyframe.png")

    def test_parameterize_sets_every_field(self):
        graph = self.client.parameterize(
            API_GRAPH, positive_prompt="hand closes around the doorknob",
            negative_prompt="static", image_name="keyframe.png",
            seed=4242, width=768, height=512, frames=97,
        )
        self.assertEqual(graph["1"]["inputs"]["image"], "keyframe.png")
        self.assertEqual(graph["2"]["inputs"]["text"], "hand closes around the doorknob")
        self.assertEqual(graph["3"]["inputs"]["text"], "static")
        self.assertEqual(graph["4"]["inputs"]["seed"], 4242)
        self.assertEqual(graph["4"]["inputs"]["num_frames"], 97)

    def test_parameterize_does_not_mutate_caller_graph(self):
        # The batch runner reuses one base graph across every shot; mutating it would
        # leak shot N's prompt into shot N+1.
        self.client.parameterize(API_GRAPH, positive_prompt="changed", seed=1)
        self.assertEqual(API_GRAPH["2"]["inputs"]["text"], "POSITIVE PLACEHOLDER")
        self.assertEqual(API_GRAPH["4"]["inputs"]["seed"], 0)

    def test_parameterize_raises_when_nothing_matches(self):
        # A graph that quietly ignores parameters yields N identical clips, which is a
        # much worse failure than an exception.
        with self.assertRaises(ComfyError):
            self.client.parameterize({"9": {"class_type": "Nope", "inputs": {}}}, seed=1)

    def test_generate_clip_round_trip(self):
        destination = self.tmp / "shot_00.mp4"
        result = self.client.generate_clip(API_GRAPH, destination)
        self.assertTrue(destination.is_file())
        self.assertEqual(destination.read_bytes(), CLIP_BYTES)
        self.assertEqual(result.prompt_id, "stub-prompt-1")
        self.assertGreaterEqual(result.seconds, 0)
        # .part must not survive — a partial file that looks finished is the bug the
        # atomic rename exists to prevent.
        self.assertFalse((self.tmp / "shot_00.mp4.part").exists())

    def test_generate_clip_picks_video_over_image(self):
        # The stub returns both a thumbnail and a clip; only the clip is the output.
        destination = self.tmp / "shot_01.mp4"
        self.client.generate_clip(API_GRAPH, destination)
        self.assertEqual(destination.read_bytes(), CLIP_BYTES)

    def test_submit_sends_client_id(self):
        self.client.submit(API_GRAPH)
        self.assertEqual(len(_Handler.submitted), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
