# For textToSpeech.py
import requests
from tqdm import tqdm
import sys
from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()  # loads .env into environment variables

URL = "https://developer.voicemaker.in/voice/api"
API_KEY = os.getenv("VOICEMAKER_API_KEY")

if not API_KEY:
    raise RuntimeError("VOICEMAKER_API_KEY is missing from .env")

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

# From createFinalv2.py

from moviepy import VideoFileClip, AudioFileClip, TextClip, CompositeVideoClip
from moviepy.video.tools.subtitles import SubtitlesClip
import random
import numpy as np
import torch
import time

import whisper_timestamped as whisper

########### Allows file to save in specific directory ######################################
from pathlib import Path
from datetime import datetime

# -------- CONFIG --------
OUTPUT_DIR = Path("HorrorVideos")
COUNTER_FILE = Path("HorrorStories/counter.txt")

# Ensure output directory exists
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Read counter
if COUNTER_FILE.exists():
    story_number = (int(COUNTER_FILE.read_text().strip()))

# Get today's date
date_str = datetime.now().strftime("%Y-%m-%d")

# Build filename
output_filename = f"HorrorStory{story_number}_{date_str}.mp4"
output_path = OUTPUT_DIR / output_filename

print(f"Saving video to: {output_path}")

########################################################################
# ----------------- helpers -----------------
def format_time(seconds: float) -> str:
    minutes = int(seconds // 60)
    remaining_seconds = seconds % 60
    return f"{minutes}m {remaining_seconds:.2f}s"
# -------------------------------------------

video_clip = VideoFileClip("MCParkour.mp4")  # for videos
video_clip = video_clip.without_audio()
# video file clips already have fps and duration
print("Clip duration: {}".format(video_clip.duration))
print("Clip fps: {}".format(video_clip.fps))

audio_clip = AudioFileClip("finalOutput.mp3")  # keep for now (we’ll re-bind after segment)

# Select random segment of video
start_time = random.uniform(0, video_clip.duration - audio_clip.duration)
video_segment = video_clip.subclipped(start_time, start_time + audio_clip.duration)

# ✅ re-bind audio to segment duration (more robust muxing)
audio_clip = (
    AudioFileClip("finalOutput.mp3")
    .with_start(0)
    .with_duration(video_segment.duration)
)

def get_transcribed_text(filename):
    audio = whisper.load_audio(filename)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print("Whisper device:", device)

    model = whisper.load_model("small", device=device)

    if device == "cuda":
        torch.cuda.synchronize()
    results = whisper.transcribe(model, audio, language="en")
    if device == "cuda":
        torch.cuda.synchronize()

    return results["segments"]

def get_text_clips(text, max_chars_per_clip=35):
    text_clips = []

    for segment in text:
        words = segment.get("words") or []
        if not words:
            continue

        current_text = ""
        current_start = words[0]["start"]
        current_end = words[0]["end"]

        for word in words:
            if len(current_text) + len(word["text"]) + 1 <= max_chars_per_clip:
                if current_text:
                    current_text += " "
                current_text += word["text"]
                current_end = word["end"]
            else:
                safe_text = current_text + "\n"  # ✅ prevents descender clipping
                text_clips.append(
                    TextClip(
                        text=safe_text,
                        method="caption",
                        font_size=34,
                        size=(1400, 260),
                        stroke_width=5,
                        stroke_color="black",
                        font="font/use.ttf",
                        color="white",
                        text_align="center",
                        interline=6,
                        margin=(20, 30)
                    )
                    .with_position(("center", "center"))
                    .with_start(current_start)
                    .with_end(current_end)
                )

                current_text = word["text"]
                current_start = word["start"]
                current_end = word["end"]

        if current_text:
            safe_text = current_text + "\n"  # ✅ prevents descender clipping
            text_clips.append(
                TextClip(
                    text=safe_text,
                    method="caption",
                    font_size=34,
                    size=(1400, 260),
                    stroke_width=5,
                    stroke_color="black",
                    font="font/use.ttf",
                    color="white",
                    text_align="center",
                    interline=6,
                    margin=(20, 30)
                )
                .with_position(("center", "center"))
                .with_start(current_start)
                .with_end(current_end)
            )

    return text_clips

# ===================== SUBTITLES TIMER =====================
subtitle_start = time.perf_counter()

# Loading the video as a VideoFileClip
transcribed_text = get_transcribed_text("finalOutput.mp3")
# Generate text elements for video using transcribed text
text_clip_list = get_text_clips(text=transcribed_text)

subtitle_end = time.perf_counter()
subtitle_time = subtitle_end - subtitle_start
# ===========================================================

# Create a CompositeVideoClip that we write to a file
final_clip = CompositeVideoClip([video_segment] + text_clip_list)
final_clip = final_clip.with_audio(audio_clip)

# ===================== VIDEO TIMER =========================
video_start = time.perf_counter()

# write_videofile: ✅ audio=True + faststart
final_clip.write_videofile(
    str(output_path),
    codec="h264_nvenc",
    audio=True,
    audio_codec="aac",
    fps=video_segment.fps,
    preset="p4",
    ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
    temp_audiofile="temp-audio.m4a",
    remove_temp=True
)


video_end = time.perf_counter()
video_time = video_end - video_start
# ===========================================================

final_clip.close()
video_segment.close()
video_clip.close()
audio_clip.close()

total_time = subtitle_time + video_time

print("\n====== PERFORMANCE SUMMARY ======")
print(f"Subtitles (Whisper + TextClips): {format_time(subtitle_time)}")
print(f"Video render (NVENC):            {format_time(video_time)}")
print(f"Total pipeline time:             {format_time(total_time)}")
print("================================\n")