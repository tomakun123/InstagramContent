import json
import requests
from pathlib import Path

N8N_WEBHOOK_URL = "http://localhost:5678/webhook-test/video_ready"

########### Helper Function to find new content path ######################################
from pathlib import Path
from datetime import datetime

# -------- CONFIG --------
BASE_DIR = Path(r"C:\Users\Thomas M\Desktop\InstagramContent")
VIDEOS_DIR = BASE_DIR / "HorrorVideos"
STORIES_DIR = BASE_DIR / "HorrorStories"
COUNTER_FILE = STORIES_DIR / "counter.txt"

# Read counter
if COUNTER_FILE.exists():
    story_number = int(COUNTER_FILE.read_text().strip())

# Get today's date
date_str = datetime.now().strftime("%Y-%m-%d")

# Build filename
output_filename = f"HorrorStory{story_number}_{date_str}.mp4"

# Bild metadata path
metadata_path = BASE_DIR / "Metadata" / f"HorrorStory{story_number}_{date_str}_metadata.json"

########################################################################

with open(metadata_path, "r", encoding="utf-8") as f:
    metadata = json.load(f)

# ---- derive paths from metadata ----
job_id = metadata["path"]  # e.g. "HorrorStory4_2026-01-07"

video_path = VIDEOS_DIR / f"{job_id}.mp4"

# ---- sanity checks ----
if not video_path.exists():
    raise FileNotFoundError(f"Video not found: {video_path}")

# ---- webhook payload (minimal + clean) ----
payload = {
    "job_id": job_id,
    "video_path": str(video_path),
    "metadata_path": str(metadata_path),
}

resp = requests.post(N8N_WEBHOOK_URL, json=payload, timeout=10)
resp.raise_for_status()

print("✅ Sent to n8n:")
print(payload)
