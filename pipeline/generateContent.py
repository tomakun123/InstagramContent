"""End-to-end content pipeline: story text -> narration -> music mix -> rendered Short.

Runs as a single pass for the story number currently in HorrorStories/counter.txt:

  1. TTS      - edge_tts narration of HorrorStories/HorrorStory{n}.txt
  2. Music    - ffmpeg mix of the narration with looped background music
  3. Render   - MoviePy composite over a random background segment, with
                word-timed subtitles from whisper_timestamped, encoded via NVENC

The final file is written to a .part.mp4 and only renamed to .mp4 once the encode
completes, so downstream consumers never observe a partial file.
"""
import asyncio
import subprocess
import sys
import time

import edge_tts
from dotenv import load_dotenv

import paths

load_dotenv()
paths.ensure_dirs()

# -------- VOICE CONTROLS --------
VOICE = "en-US-ChristopherNeural"
RATE = "+40%"      # Speed
PITCH = "-10Hz"    # Depth
VOLUME = "+50%"    # Presence

# -------- VIDEO --------
TARGET_W, TARGET_H = 1080, 1920  # 9:16 vertical

story_number = paths.story_number()

story_path = paths.STORIES / f"HorrorStory{story_number}.txt"
voice_path = paths.AUDIO / f"HorrorAudioOutput{story_number}.mp3"
mixed_path = paths.AUDIO / f"HorrorAudioMusicOutput{story_number}.mp3"
output_path = paths.VIDEOS / f"HorrorStory{story_number}.mp4"

# temp file still ends with .mp4 so ffmpeg knows the container
temp_output_path = output_path.with_name(output_path.stem + ".part" + output_path.suffix)


def format_time(seconds: float) -> str:
    minutes = int(seconds // 60)
    remaining_seconds = seconds % 60
    return f"{minutes}m {remaining_seconds:.2f}s"


# ======================= 1. TEXT TO SPEECH =======================
if not story_path.exists():
    print(f"[X] Input text file not found: {story_path}")
    sys.exit(1)

text = story_path.read_text(encoding="utf-8")

print("Loaded text from:", story_path)
print("Saving audio to:", voice_path)
print(f"Voice={VOICE} Rate={RATE} Pitch={PITCH} Volume={VOLUME}")


async def synthesize() -> None:
    tts = edge_tts.Communicate(
        text=text,
        voice=VOICE,
        rate=RATE,
        pitch=PITCH,
        volume=VOLUME,
    )
    await tts.save(str(voice_path))


asyncio.run(synthesize())
print(f"[OK] Audio file saved as {voice_path}")


# ======================= 2. BACKGROUND MUSIC =======================
print(f"Saving mixed audio to: {mixed_path}")

# Lower music volume, loop it to match voice length, then mix
subprocess.run(
    [
        "ffmpeg", "-y",
        "-i", str(voice_path),
        "-stream_loop", "-1", "-i", str(paths.BACKGROUND_MUSIC),
        "-filter_complex",
        "[1:a]volume=0.5,aloop=loop=-1:size=2e+09[a1];"
        "[a1]atrim=0:999999,asetpts=N/SR/TB[a2];"
        "[0:a][a2]amix=inputs=2:duration=first:dropout_transition=2",
        "-c:a", "libmp3lame",
        "-q:a", "2",
        str(mixed_path),
    ],
    check=True,
)
print("Wrote to:", mixed_path)


# ======================= 3. RENDER =======================
import random  # noqa: E402  (deferred: heavy imports only needed for the render)

import torch  # noqa: E402
import whisper_timestamped as whisper  # noqa: E402
from moviepy import (  # noqa: E402
    AudioFileClip,
    CompositeVideoClip,
    TextClip,
    VideoFileClip,
)

print(f"Saving video to (temp): {temp_output_path}")
print(f"Final video will be:    {output_path}")


def to_vertical_9x16(clip, target_w=TARGET_W, target_h=TARGET_H):
    """Resize to cover the target frame, then centre-crop to exactly target_w x target_h."""
    w, h = clip.size
    target_aspect = target_w / target_h
    src_aspect = w / h

    if src_aspect > target_aspect:
        # wider than 9:16 -> match height
        clip = clip.resized(height=target_h)
    else:
        # taller/narrower -> match width
        clip = clip.resized(width=target_w)

    w2, h2 = clip.size
    x1 = int((w2 - target_w) / 2)
    y1 = int((h2 - target_h) / 2)
    return clip.cropped(x1=x1, y1=y1, width=target_w, height=target_h)


def get_transcribed_text(filename):
    audio = whisper.load_audio(str(filename))
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print("Whisper device:", device)

    model = whisper.load_model("small", device=device)

    if device == "cuda":
        torch.cuda.synchronize()
    results = whisper.transcribe(model, audio, language="en")
    if device == "cuda":
        torch.cuda.synchronize()

    return results["segments"]


def get_text_clips(text, max_chars_per_clip=30):
    text_clips = []

    # Subtitle box sized to fit the 1080-wide vertical frame
    SUB_W = int(TARGET_W * 0.92)
    SUB_H = int(TARGET_H * 0.05)
    SUB_Y = ("center", "center")

    def make_clip(body, start, end):
        return (
            TextClip(
                text=body + "\n",  # trailing newline prevents descender clipping
                method="caption",
                font_size=48,
                size=(SUB_W, SUB_H),
                stroke_width=5,
                stroke_color="black",
                font=str(paths.FONT),
                color="white",
                text_align="center",
                interline=6,
                margin=(20, 15),
            )
            .with_position(SUB_Y)
            .with_start(start)
            .with_end(end)
        )

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
                text_clips.append(make_clip(current_text, current_start, current_end))
                current_text = word["text"]
                current_start = word["start"]
                current_end = word["end"]

        if current_text:
            text_clips.append(make_clip(current_text, current_start, current_end))

    return text_clips


video_clip = VideoFileClip(str(paths.BACKGROUND_VIDEO)).without_audio()
print(f"Clip duration: {video_clip.duration}")
print(f"Clip fps: {video_clip.fps}")

audio_clip = AudioFileClip(str(mixed_path))

# Select a random segment of background video long enough to cover the narration
if video_clip.duration <= audio_clip.duration:
    start_time = 0
else:
    start_time = random.uniform(0, video_clip.duration - audio_clip.duration)

video_segment = video_clip.subclipped(start_time, start_time + audio_clip.duration)

# Convert to vertical 1080x1920 BEFORE subtitles so everything aligns
video_segment = to_vertical_9x16(video_segment).with_position(("center", "center"))

# Re-bind audio to the segment duration (more robust muxing)
audio_clip = (
    AudioFileClip(str(mixed_path))
    .with_start(0)
    .with_duration(video_segment.duration)
)

# ===================== SUBTITLES TIMER =====================
subtitle_start = time.perf_counter()

transcribed_text = get_transcribed_text(mixed_path)
text_clip_list = get_text_clips(text=transcribed_text)

subtitle_time = time.perf_counter() - subtitle_start
# ===========================================================

final_clip = CompositeVideoClip([video_segment] + text_clip_list, size=(TARGET_W, TARGET_H))
final_clip = final_clip.with_audio(audio_clip)
final_size = final_clip.size

# ===================== VIDEO TIMER =========================
video_start = time.perf_counter()

final_clip.write_videofile(
    str(temp_output_path),
    codec="h264_nvenc",
    audio=True,
    audio_codec="aac",
    fps=video_segment.fps,
    preset="p4",
    ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
    temp_audiofile=str(paths.VIDEOS / f"temp-audio-{story_number}.m4a"),
    remove_temp=True,
)

video_time = time.perf_counter() - video_start
# ===========================================================

# Close resources before rename (avoids Windows file locking)
final_clip.close()
video_segment.close()
video_clip.close()
audio_clip.close()

# Atomic finalize: rename .part -> .mp4 so n8n only sees a complete file
temp_output_path.replace(output_path)
print(f"[OK] Finalized video: {output_path}")
print(f"Final video size: {final_size}")

print("\n====== PERFORMANCE SUMMARY ======")
print(f"Subtitles (Whisper + TextClips): {format_time(subtitle_time)}")
print(f"Video render (NVENC):            {format_time(video_time)}")
print(f"Total pipeline time:             {format_time(subtitle_time + video_time)}")
print("================================\n")
