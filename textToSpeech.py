# For textToSpeech.py
import requests
from tqdm import tqdm
import sys
from pathlib import Path

URL = "https://developer.voicemaker.in/voice/api"
API_KEY = "72140430-13b8-11f0-9aa7-3f1fd6fd0a49"

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

if response.status_code == 200:
    result = response.json()
    if result.get("success"):
        audio_url = result.get("path")
        audio_response = requests.get(audio_url, stream=True)

        total_size = int(audio_response.headers.get('content-length', 0))
        block_size = 1024  # 1 Kibibyte

        with open("voiceOutput.mp3", "wb") as file, tqdm(
            desc="Downloading",
            total=total_size,
            unit='iB',
            unit_scale=True,
            unit_divisor=1024,
        ) as bar:
            for data in audio_response.iter_content(block_size):
                file.write(data)
                bar.update(len(data))

        print("✅ Audio file saved as output.mp3")
    else:
        print("❌ Error in conversion:", result.get("message"))
else:
    print(f"❌ Request failed with status code {response.status_code}")
    print(response.text)

# From addMusic.py

import subprocess
from pathlib import Path

voice = Path("voiceOutput.mp3")
music = Path("musicOutput.mp3")
out = Path("finalOutput.mp3")

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
print("Wrote:", out)

