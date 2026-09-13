"""Central path definitions for the pipeline.

Every path in the pipeline is derived from the repo root, which is resolved from
this file's own location. This makes the scripts independent of the working
directory they are launched from.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Runtime output directories
STORIES = ROOT / "HorrorStories"
AUDIO = ROOT / "HorrorAudio"
VIDEOS = ROOT / "HorrorVideos"
CLIPS = VIDEOS / "clips"          # one sub-directory per story: clips/<n>/beat<k>.mp4
METADATA = ROOT / "Metadata"
LOGS = ROOT / "logs"

# Static assets
ASSETS = ROOT / "assets"
FONT = ASSETS / "use.ttf"
BACKGROUND_VIDEO = ASSETS / "MCParkour.mp4"
BACKGROUND_MUSIC = ASSETS / "musicOutput.mp3"
COMFY_WORKFLOW = ASSETS / "comfy" / "wan22_5b_t2v_api.json"          # video style
COMFY_IMAGE_WORKFLOW = ASSETS / "comfy" / "flux_schnell_t2i_api.json"  # image style

# State files
COUNTER = STORIES / "counter.txt"
LOCK = STORIES / "generate_lock.lock"

# Scripts
GENERATE_CONTENT = ROOT / "pipeline" / "generateContent.py"


def story_number() -> int:
    """Read the current story counter. Raises if the counter is missing."""
    if not COUNTER.exists():
        raise FileNotFoundError(f"Missing counter file: {COUNTER}")
    return int(COUNTER.read_text().strip())


def ensure_dirs() -> None:
    """Create every runtime output directory if it does not already exist."""
    for d in (STORIES, AUDIO, VIDEOS, CLIPS, METADATA, LOGS):
        d.mkdir(parents=True, exist_ok=True)


def _env(name: str, default: str = "") -> str:
    import os
    return os.environ.get(name, default).strip()


def comfy_url() -> str:
    """Base URL of the ComfyUI server that generates background clips."""
    return _env("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")


def lms_url() -> str:
    """OpenAI-compatible base URL of LM Studio (same server n8n prompts)."""
    return _env("LMS_URL", "http://127.0.0.1:1234/v1").rstrip("/")


def lms_model() -> str:
    """Story model identifier. Same default as scripts/start-pipeline.ps1."""
    return _env("LMS_MODEL", "mn-12b-mag-mell-r1")


def background_mode() -> str:
    """'ai' (generated backgrounds, Minecraft on failure) or 'minecraft' (never generate)."""
    return _env("BACKGROUND_MODE", "ai").lower()


def background_style() -> str:
    """'image' (Flux still + Ken Burns per beat) or 'video' (Wan 2.2 clip per beat)."""
    return _env("BACKGROUND_STYLE", "image").lower()


def render_webhook() -> str:
    """n8n webhook to POST to when a render finishes, or '' if not configured.

    Set N8N_RENDER_WEBHOOK in .env. When unset the pipeline still renders; it
    just does not trigger publishing.
    """
    return _env("N8N_RENDER_WEBHOOK")
