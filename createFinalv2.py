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
    story_number = int(COUNTER_FILE.read_text().strip())

# Get today's date
date_str = datetime.now().strftime("%Y-%m-%d")

# Build filename
output_filename = f"HorrorStory{story_number}_{date_str}.mp4"
output_path = OUTPUT_DIR / output_filename

# ✅ temp file still ends with .mp4 so ffmpeg knows the container
temp_output_path = output_path.with_name(output_path.stem + ".part" + output_path.suffix)

print(f"Saving video to (temp): {temp_output_path}")
print(f"Final video will be:    {output_path}")

########################################################################
# ----------------- helpers -----------------
def format_time(seconds: float) -> str:
    minutes = int(seconds // 60)
    remaining_seconds = seconds % 60
    return f"{minutes}m {remaining_seconds:.2f}s"
# -------------------------------------------

video_clip = VideoFileClip("HorrorVideos/MCParkour.mp4")  # for videos
video_clip = video_clip.without_audio()
print("Clip duration: {}".format(video_clip.duration))
print("Clip fps: {}".format(video_clip.fps))

audio_src = f"HorrorAudio/HorrorAudioMusicOutput{story_number}_{date_str}.mp3"
audio_clip = AudioFileClip(audio_src)

# Select random segment of video
start_time = random.uniform(0, video_clip.duration - audio_clip.duration)
video_segment = video_clip.subclipped(start_time, start_time + audio_clip.duration)

# ✅ re-bind audio to segment duration (more robust muxing)
audio_clip = (
    AudioFileClip(audio_src)
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

transcribed_text = get_transcribed_text(audio_src)
text_clip_list = get_text_clips(text=transcribed_text)

subtitle_end = time.perf_counter()
subtitle_time = subtitle_end - subtitle_start
# ===========================================================

final_clip = CompositeVideoClip([video_segment] + text_clip_list)
final_clip = final_clip.with_audio(audio_clip)

# ===================== VIDEO TIMER =========================
video_start = time.perf_counter()

# ✅ write to temp .part first
final_clip.write_videofile(
    str(temp_output_path),
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

# Close resources before rename (helps on Windows file locking)
final_clip.close()
video_segment.close()
video_clip.close()
audio_clip.close()

# ✅ Atomic finalize: rename .part -> .mp4 so n8n triggers only when complete
temp_output_path.replace(output_path)
print(f"✅ Finalized video: {output_path}")

total_time = subtitle_time + video_time

print("\n====== PERFORMANCE SUMMARY ======")
print(f"Subtitles (Whisper + TextClips): {format_time(subtitle_time)}")
print(f"Video render (NVENC):            {format_time(video_time)}")
print(f"Total pipeline time:             {format_time(total_time)}")
print("================================\n")
