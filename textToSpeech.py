# textToSpeech_edge.py
import sys
from pathlib import Path
from datetime import datetime
import asyncio
import edge_tts

# -------- CONFIG --------
OUTPUT_DIR = Path("HorrorAudio")
COUNTER_FILE = Path("HorrorStories/counter.txt")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

VOICE = "en-US-GuyNeural"     # try: en-US-DavisNeural, en-GB-RyanNeural
RATE  = "-15%"               # speed: slower is negative, faster is positive
PITCH = "-15Hz"              # deeper voice: more negative Hz
VOLUME = "+0%"               # optional

# -------- FILE NAMING --------
story_number = int(COUNTER_FILE.read_text().strip()) if COUNTER_FILE.exists() else 1
date_str = datetime.now().strftime("%Y-%m-%d")
output_path = OUTPUT_DIR / f"HorrorAudioOutput{story_number}_{date_str}.mp3"

# -------- LOAD TEXT --------
file_path = Path(sys.argv[1])
text = file_path.read_text(encoding="utf-8")
print("Loaded:", file_path)
print("Saving audio to:", output_path)
print(f"Voice={VOICE} Rate={RATE} Pitch={PITCH} Volume={VOLUME}")

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
print("✅ Done:", output_path)
