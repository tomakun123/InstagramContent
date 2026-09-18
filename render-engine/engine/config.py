"""Settings. Everything is overridable by environment variable so the same code runs
against a local tunnel and against a pod without edits."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is convenience, not a requirement
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class ComfyConfig:
    """Where ComfyUI is reachable.

    Default is localhost:8188, which assumes the SSH tunnel from RUNBOOK step 6 is up.
    Pointing this at a *.proxy.runpod.net URL works but exposes an unauthenticated
    ComfyUI to anyone who learns the hostname — see RUNBOOK step 6.
    """

    host: str = _env("COMFY_HOST", "127.0.0.1")
    port: int = _env_int("COMFY_PORT", 8188)
    # Generation is minutes-long; a short read timeout just breaks valid jobs.
    timeout_s: int = _env_int("COMFY_TIMEOUT_S", 1800)
    poll_interval_s: float = 2.0

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True)
class RunPodConfig:
    api_key: str | None = _env("RUNPOD_API_KEY")
    network_volume_id: str | None = _env("RUNPOD_NETWORK_VOLUME_ID")
    gpu_type_id: str = _env("RUNPOD_GPU_TYPE_ID", "NVIDIA GeForce RTX 5090")
    # LTX-2.3 needs 32 GB+ VRAM. The 5090 has exactly that, so there is no headroom to
    # fall back to a 4090 without the low_vram_loaders/FP8 path.
    min_vram_gb: int = 32
    image: str = _env("RUNPOD_IMAGE", "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04")
    volume_mount_path: str = "/runpod-volume"
    pod_name: str = _env("RUNPOD_POD_NAME", "horror-render-engine")
    # Backstop for the "left it running all weekend" failure. The batch runner also
    # terminates in a finally block; this covers the case where the runner itself dies.
    idle_timeout_min: int = _env_int("RUNPOD_IDLE_TIMEOUT_MIN", 30)


@dataclass(frozen=True)
class RenderConfig:
    """Output geometry. 1080x1920 is what generateContent.py already produces.

    Generation happens at the model's native size and is upscaled during assembly —
    asking the model for 1080x1920 directly is slower and not better.
    """

    gen_width: int = _env_int("GEN_WIDTH", 768)
    gen_height: int = _env_int("GEN_HEIGHT", 512)
    fps: int = _env_int("GEN_FPS", 24)
    frames: int = _env_int("GEN_FRAMES", 97)  # ~4 s at 24 fps, the LTX benchmark shape
    max_rerolls: int = _env_int("MAX_REROLLS", 3)


COMFY = ComfyConfig()
RUNPOD = RunPodConfig()
RENDER = RenderConfig()
