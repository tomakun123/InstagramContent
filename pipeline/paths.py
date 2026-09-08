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
METADATA = ROOT / "Metadata"
LOGS = ROOT / "logs"

# Static assets
ASSETS = ROOT / "assets"
FONT = ASSETS / "use.ttf"
BACKGROUND_VIDEO = ASSETS / "MCParkour.mp4"
BACKGROUND_MUSIC = ASSETS / "musicOutput.mp3"

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
    for d in (STORIES, AUDIO, VIDEOS, METADATA, LOGS):
        d.mkdir(parents=True, exist_ok=True)
