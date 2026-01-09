import subprocess
from pathlib import Path

########### Allows file to save in specific directory ######################################
from pathlib import Path
from datetime import datetime

# -------- CONFIG --------
OUTPUT_DIR = Path("HorrorAudio")
COUNTER_FILE = Path("HorrorStories/counter.txt")

# Ensure output directory exists
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Read counter
if COUNTER_FILE.exists():
    story_number = (int(COUNTER_FILE.read_text().strip()))

# Get today's date
date_str = datetime.now().strftime("%Y-%m-%d")

# Build filename
output_filename = f"HorrorAudioMusicOutput{story_number}_{date_str}.mp3"
output_path = OUTPUT_DIR / output_filename

print(f"Saving audio to: {output_path}")

########################################################################

voice = Path(f"HorrorAudio/HorrorAudioOutput{story_number}_{date_str}.mp3")
music = Path("HorrorAudio/musicOutput.mp3")
out = Path(output_path)

# Lower music volume, loop it to match voice length, then mix
cmd = [
    "ffmpeg", "-y",
    "-i", str(voice),
    "-stream_loop", "-1", "-i", str(music),
    "-filter_complex",
    "[1:a]volume=0.5,aloop=loop=-1:size=2e+09[a1];"
    "[a1]atrim=0:999999,asetpts=N/SR/TB[a2];"
    "[0:a][a2]amix=inputs=2:duration=first:dropout_transition=2",
    "-c:a", "libmp3lame",
    "-q:a", "2",
    str(out),
]

subprocess.run(cmd, check=True)
print("Wrote to:", out)
