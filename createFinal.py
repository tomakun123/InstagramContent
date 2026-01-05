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
    story_number = (int(COUNTER_FILE.read_text().strip())-1)

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

audio_clip = AudioFileClip("finalOutput.mp3")

# Select random segment of video
start_time = random.uniform(0, video_clip.duration - audio_clip.duration)
# New Video with New Duration
video_segment = video_clip.subclipped(start_time, start_time + audio_clip.duration)
print(f"Using video segment from {start_time:.2f}s to {start_time + audio_clip.duration:.2f}s")

print("Clip duration: {}".format(video_segment.duration))  # Cuting will update duration
print("Clip fps: {}".format(video_segment.fps))  # and keep fps

# And finally we can write the result into a file
# Here we just save as MP4, inheriting FPS, etc. from final_clip

# Get .srt file from video
def get_transcribed_text(filename):
    audio = whisper.load_audio(filename)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print("Whisper device:", device)

    model = whisper.load_model("small", device=device)
    results = whisper.transcribe(model, audio, language="en")
    return results["segments"]

def get_text_clips(text, max_chars_per_clip=20):
    text_clips = []

    for segment in text:
        words = segment["words"]
        current_text = ""
        current_start = words[0]["start"]
        current_end = words[0]["end"]

        for word in words:
            # Check if adding the next word exceeds the limit
            if len(current_text) + len(word["text"]) + 1 <= max_chars_per_clip:
                if current_text:
                    current_text += " "
                current_text += word["text"]
                current_end = word["end"]
            else:
                # Create the clip for the current group
                text_clips.append(
                    TextClip(
                        text=current_text,
                        method="caption",          # ✅ wrapping
                        font_size=30,
                        size=(1400, 260),         # ✅ wrap to 1400px wide; NOT full frame
                        stroke_width=5,
                        stroke_color="black",
                        font="font/use.ttf",
                        color="white",
                        text_align="center",
                        interline=6,               # ✅ more line spacing
                        margin=(20, 30)            # ✅ (x, y) padding INSIDE box
                    )
                    .with_position(("center", "center"))
                    .with_start(current_start)
                    .with_end(current_end)
                )
                # Start a new group
                current_text = word["text"]
                current_start = word["start"]
                current_end = word["end"]

        # Add the last clip if any text remains
        if current_text:
            text_clips.append(
                TextClip(
                    text=current_text,
                    method="caption",          # ✅ wrapping
                    font_size=34,
                    size=(1400, 260),         # ✅ wrap to 1400px wide; NOT full frame
                    stroke_width=5,
                    stroke_color="black",
                    font="font/use.ttf",
                    color="white",
                    text_align="center",
                    interline=6,               # ✅ more line spacing
                    margin=(20, 30)            # ✅ (x, y) padding INSIDE box
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

# Write the final video once (video with subtitles + audio)
final_clip.write_videofile(
    str(output_path),
    codec="h264_nvenc",
    audio_codec="aac",
    fps=video_segment.fps,
    preset="p4",
    ffmpeg_params=["-pix_fmt", "yuv420p"],
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

