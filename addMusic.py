from pydub import AudioSegment

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
output_filename = f"HorrorAudioMusicOutput{story_number}_{date_str}.mp4"
output_path = OUTPUT_DIR / output_filename

print(f"Saving video to: {output_path}")

########################################################################

# Load your audio files
voice = AudioSegment.from_mp3("voiceOutput.mp3")
background = AudioSegment.from_mp3("musicOutput.mp3")
background = AudioSegment.from_mp3("musicOutput.mp3")

# Adjust background music volume (usually lower so it doesn't overpower the voice)
background = background - 5  # reduce volume by 15 dB

# If background is shorter than the voice, loop it
if len(background) < len(voice):
    background = background * (len(voice) // len(background) + 1)

# Trim background to the length of the voice
background = background[:len(voice)]

# Overlay the two tracks
combined = voice.overlay(background)

# Export the final audio
combined.export("finalOutput.mp3", format="mp3")
