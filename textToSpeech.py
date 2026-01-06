# For textToSpeech.py
import requests
from tqdm import tqdm
import sys
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
output_filename = f"HorrorAudioOutput{story_number}_{date_str}.mp3"
output_path = OUTPUT_DIR / output_filename

########################################################################

URL = "https://developer.voicemaker.in/voice/api"
API_KEY = ""

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

# Read text from file
# with open("post1.txt", "r", encoding="utf-8") as file:
#     text = file.read()

file_path = Path(sys.argv[1])
text = file_path.read_text(encoding="utf-8")
print("Loaded: ", file_path)

data = {
    "Engine": "neural",
    "VoiceId": "proplus-Matthew",
    "LanguageCode": "en-US",
    "Text": text,
    "OutputFormat": "mp3",
    "SampleRate": "48000",
    "Effect": "default",
    "MasterVolume": "0",
    "MasterSpeed": "20",
    "MasterPitch": "-20",
    "Stability" : "75"
}

response = requests.post(URL, headers=headers, json=data)

print(f"Saving audio to: {output_path}")

if response.status_code == 200:
    result = response.json()
    if result.get("success"):
        audio_url = result.get("path")
        audio_response = requests.get(audio_url, stream=True)

        total_size = int(audio_response.headers.get('content-length', 0))
        block_size = 1024  # 1 Kibibyte

        with open(output_path, "wb") as file, tqdm(
            desc="Downloading",
            total=total_size,
            unit='iB',
            unit_scale=True,
            unit_divisor=1024,
        ) as bar:
            for chunk in audio_response.iter_content(block_size):
                if chunk:
                    file.write(chunk)
                    bar.update(len(chunk))

        print(f"✅ Audio file saved as {output_path}")
    else:
        print("❌ Error in conversion:", result.get("message"))
else:
    print(f"❌ Request failed with status code {response.status_code}")
    print(response.text)
