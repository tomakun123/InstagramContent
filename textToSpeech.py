# textToSpeech_edge.py
import sys
from pathlib import Path
from datetime import datetime
import asyncio
import edge_tts

# -------- CONFIG (MATCHES OLD SCRIPT) --------
OUTPUT_DIR = Path("HorrorAudio")
COUNTER_FILE = Path("HorrorStories/counter.txt")

# Ensure output directory exists
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Read counter
if COUNTER_FILE.exists():
    story_number = int(COUNTER_FILE.read_text().strip())

# Get today's date
date_str = datetime.now().strftime("%Y-%m-%d")

# Build filename (IDENTICAL FORMAT)
output_filename = f"HorrorAudioOutput{story_number}_{date_str}.mp3"
output_path = OUTPUT_DIR / output_filename

# -------- VOICE CONTROLS (YOUR GOAT PRESET) --------
VOICE  = "en-US-ChristopherNeural"
RATE   = "+45%"     # Speed
PITCH  = "-27Hz"    # Depth
VOLUME = "+50%"     # Presence

# -------- LOAD TEXT (AUTO FROM COUNTER + DATE) --------

# Build expected input filename
input_filename = f"HorrorStory{story_number}_{date_str}.txt"
input_path = Path("HorrorStories") / input_filename

if not input_path.exists():
    print(f"❌ Input text file not found: {input_path}")
    sys.exit(1)

text = input_path.read_text(encoding="utf-8")

print("Loaded text from:", input_path)
print("Saving audio to:", output_path)
print(f"Voice={VOICE} Rate={RATE} Pitch={PITCH} Volume={VOLUME}")

# -------- TTS --------
async def main():
    tts = edge_tts.Communicate(
        text=text,
        voice=VOICE,
        rate=RATE,
        pitch=PITCH,
        volume=VOLUME,
    )
    await tts.save(str(output_path))

asyncio.run(main())

print(f"✅ Audio file saved as {output_path}")
