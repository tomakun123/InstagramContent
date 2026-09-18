"""Minimal ComfyUI HTTP client: upload a keyframe, submit a graph, wait, fetch the clip.

ComfyUI's API is small and stable:
    POST /upload/image     multipart -> {name, subfolder, type}
    POST /prompt           {prompt, client_id} -> {prompt_id}
    GET  /history/{id}     -> {id: {outputs: {node_id: {<kind>: [{filename, subfolder, type}]}}}}
    GET  /view?filename&subfolder&type -> bytes

Deliberately no websocket. Progress events are nice for a UI and irrelevant to a batch
runner that only needs to know when a job is done.
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import requests

from .config import COMFY, ComfyConfig


class ComfyError(RuntimeError):
    """ComfyUI rejected a request or a job failed."""


@dataclass
class ClipResult:
    path: Path
    prompt_id: str
    seconds: float


class ComfyClient:
    def __init__(self, config: ComfyConfig = COMFY) -> None:
        self.config = config
        self.client_id = str(uuid.uuid4())
        self.session = requests.Session()

    # --- plumbing ------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.config.base_url}{path}"

    def ping(self) -> bool:
        """True if ComfyUI answers. Call before a batch so failures surface immediately
        rather than after a pod has been paid for."""
        try:
            self.session.get(self._url("/system_stats"), timeout=10).raise_for_status()
            return True
        except requests.RequestException:
            return False

    # --- inputs --------------------------------------------------------------

    def upload_image(self, path: Path, *, overwrite: bool = True) -> str:
        """Upload a keyframe into ComfyUI's input/ and return the name to reference."""
        if not path.is_file():
            raise FileNotFoundError(path)

        with path.open("rb") as handle:
            response = self.session.post(
                self._url("/upload/image"),
                files={"image": (path.name, handle, "image/png")},
                data={"overwrite": str(overwrite).lower(), "type": "input"},
                timeout=120,
            )
        if not response.ok:
            raise ComfyError(f"upload failed ({response.status_code}): {response.text[:400]}")

        payload = response.json()
        subfolder = payload.get("subfolder") or ""
        name = payload["name"]
        return f"{subfolder}/{name}" if subfolder else name

    # --- graph ---------------------------------------------------------------

    @staticmethod
    def load_workflow(path: Path) -> dict[str, Any]:
        """Load a workflow exported in **API format** (Save (API Format) in the UI).

        The UI's normal save format is a different shape — nodes as a list, with link
        objects — and /prompt will not accept it. This is the single most common reason a
        workflow that runs fine in the browser 400s from a script.
        """
        graph = json.loads(path.read_text(encoding="utf-8"))
        if "nodes" in graph and isinstance(graph.get("nodes"), list):
            raise ComfyError(
                f"{path.name} is in UI format, not API format. Re-export it with "
                "'Save (API Format)' in ComfyUI."
            )
        return graph

    @staticmethod
    def find_nodes(graph: dict[str, Any], class_type: str) -> list[str]:
        """Node ids whose class_type matches. Node ids are unstable across edits, so
        locate by class rather than hardcoding ids."""
        return [
            node_id
            for node_id, node in graph.items()
            if isinstance(node, dict) and node.get("class_type") == class_type
        ]

    @staticmethod
    def set_input(graph: dict[str, Any], node_id: str, key: str, value: Any) -> None:
        graph[node_id].setdefault("inputs", {})[key] = value

    def parameterize(
        self,
        graph: dict[str, Any],
        *,
        positive_prompt: str | None = None,
        negative_prompt: str | None = None,
        image_name: str | None = None,
        seed: int | None = None,
        width: int | None = None,
        height: int | None = None,
        frames: int | None = None,
    ) -> dict[str, Any]:
        """Apply per-shot values to a copy of the graph.

        Heuristic by design: it edits whichever nodes carry the matching input keys, so it
        survives the node repo renaming classes between releases. It reports what it
        changed so a silently-unparameterized graph — every clip identical, every seed the
        same — is visible instead of mysterious.
        """
        graph = copy.deepcopy(graph)
        touched: dict[str, int] = {}

        def apply(key: str, value: Any, *, only_nodes: list[str] | None = None) -> None:
            count = 0
            for node_id, node in graph.items():
                if only_nodes is not None and node_id not in only_nodes:
                    continue
                inputs = node.get("inputs") if isinstance(node, dict) else None
                if isinstance(inputs, dict) and key in inputs:
                    inputs[key] = value
                    count += 1
            if count:
                touched[key] = count

        if image_name is not None:
            apply("image", image_name, only_nodes=self.find_nodes(graph, "LoadImage"))
        if seed is not None:
            apply("seed", seed)
            apply("noise_seed", seed)
        if width is not None:
            apply("width", width)
        if height is not None:
            apply("height", height)
        if frames is not None:
            apply("num_frames", frames)
            apply("length", frames)

        # Prompts are positional: ComfyUI text encoders all expose "text", so the positive
        # and negative encoders are told apart by graph order, not by name.
        if positive_prompt is not None or negative_prompt is not None:
            text_nodes = sorted(
                node_id
                for node_id, node in graph.items()
                if isinstance(node, dict) and "text" in (node.get("inputs") or {})
            )
            if positive_prompt is not None and text_nodes:
                self.set_input(graph, text_nodes[0], "text", positive_prompt)
                touched["positive"] = 1
            if negative_prompt is not None and len(text_nodes) > 1:
                self.set_input(graph, text_nodes[1], "text", negative_prompt)
                touched["negative"] = 1

        if not touched:
            raise ComfyError(
                "parameterize() changed nothing — the workflow does not expose the "
                "expected inputs. Check it against the node repo's example graph."
            )
        self.last_parameterized = touched
        return graph

    # --- execution -----------------------------------------------------------

    def submit(self, graph: dict[str, Any]) -> str:
        response = self.session.post(
            self._url("/prompt"),
            json={"prompt": graph, "client_id": self.client_id},
            timeout=60,
        )
        if response.status_code == 400:
            # ComfyUI reports which node and input it rejected — surface it verbatim,
            # it is far more useful than the status code.
            raise ComfyError(f"graph rejected: {response.text[:1200]}")
        response.raise_for_status()
        return response.json()["prompt_id"]

    def wait(self, prompt_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.timeout_s
        while time.monotonic() < deadline:
            response = self.session.get(self._url(f"/history/{prompt_id}"), timeout=30)
            response.raise_for_status()
            history = response.json()
            if prompt_id in history:
                entry = history[prompt_id]
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    raise ComfyError(f"job failed: {json.dumps(status)[:1200]}")
                return entry
            time.sleep(self.config.poll_interval_s)
        raise ComfyError(f"job {prompt_id} exceeded {self.config.timeout_s}s")

    @staticmethod
    def iter_outputs(entry: dict[str, Any]) -> Iterator[dict[str, str]]:
        """Yield every file reference in a history entry.

        Output key varies by node — images, gifs, videos — so scan all of them rather
        than guessing which one LTX's save node uses.
        """
        for node_output in (entry.get("outputs") or {}).values():
            if not isinstance(node_output, dict):
                continue
            for items in node_output.values():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, dict) and "filename" in item:
                        yield item

    def download(self, reference: dict[str, str], destination: Path) -> Path:
        response = self.session.get(
            self._url("/view"),
            params={
                "filename": reference["filename"],
                "subfolder": reference.get("subfolder", ""),
                "type": reference.get("type", "output"),
            },
            timeout=600,
            stream=True,
        )
        response.raise_for_status()

        destination.parent.mkdir(parents=True, exist_ok=True)
        # Write to .part and rename — the same atomic-rename discipline generateContent.py
        # uses, so a partial file is never mistaken for a finished clip.
        staging = destination.with_suffix(destination.suffix + ".part")
        with staging.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                handle.write(chunk)
        staging.replace(destination)
        return destination

    # --- the one call the batch runner needs ---------------------------------

    def generate_clip(self, graph: dict[str, Any], destination: Path) -> ClipResult:
        started = time.monotonic()
        prompt_id = self.submit(graph)
        entry = self.wait(prompt_id)
        elapsed = time.monotonic() - started

        videos = [
            ref
            for ref in self.iter_outputs(entry)
            if Path(ref["filename"]).suffix.lower() in {".mp4", ".webm", ".mkv", ".gif"}
        ]
        if not videos:
            raise ComfyError(
                f"job {prompt_id} produced no video output. The graph probably has no "
                f"save-video node. Outputs: {json.dumps(entry.get('outputs', {}))[:600]}"
            )

        self.download(videos[0], destination)
        return ClipResult(path=destination, prompt_id=prompt_id, seconds=elapsed)
