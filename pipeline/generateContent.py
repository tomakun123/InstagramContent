"""End-to-end content pipeline: story text -> narration -> music mix -> rendered Short.

Runs as a single pass for the story number currently in HorrorStories/counter.txt:

  1. TTS      - edge_tts narration of HorrorStories/HorrorStory{n}.txt, capturing
                word boundaries as it streams (no separate transcription step)
  2. Music    - ffmpeg mix of the narration with looped background music
  3. Render   - one ffmpeg pass: crop the background to 9:16, scale to 1080x1920,
                burn in the subtitles via libass, mux the audio, encode with NVENC

The final file is written to a .part.mp4 and only renamed to .mp4 once the encode
completes, so downstream consumers never observe a partial file.
"""
import argparse
import asyncio
import json
import random
import subprocess
import sys
import time

import edge_tts
from dotenv import load_dotenv

import paths
import subtitles

load_dotenv()
paths.ensure_dirs()

# -------- VOICE CONTROLS --------
VOICE = "en-US-ChristopherNeural"
RATE = "+40%"      # Speed
PITCH = "-10Hz"    # Depth
VOLUME = "+50%"    # Presence

# -------- VIDEO --------
TARGET_W, TARGET_H = 1080, 1920  # 9:16 vertical
FPS = 30                         # Shorts/Reels/TikTok; the source is 60 but does not need to be

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--renderer",
    choices=["ffmpeg", "moviepy"],
    default="ffmpeg",
    help="ffmpeg (fast, default) or moviepy (legacy, for A/B comparison)",
)
parser.add_argument(
    "--story",
    type=int,
    default=None,
    help="Story number to render. Defaults to the value in counter.txt.",
)
notify = parser.add_mutually_exclusive_group()
notify.add_argument(
    "--notify", dest="notify", action="store_true", default=None,
    help="POST to N8N_RENDER_WEBHOOK when the render finishes (triggers publishing).",
)
notify.add_argument(
    "--no-notify", dest="notify", action="store_false",
    help="Render only; do not trigger publishing.",
)
args = parser.parse_args()

# Publishing is triggered by default for a normal pipeline run, but NOT when a
# story number was named explicitly - that is a manual re-render or backfill,
# and silently republishing it would be a nasty surprise. Pass --notify to
# override.
should_notify = args.notify if args.notify is not None else (args.story is None)

story_number = args.story if args.story is not None else paths.story_number()

story_path = paths.STORIES / f"HorrorStory{story_number}.txt"
voice_path = paths.AUDIO / f"HorrorAudioOutput{story_number}.mp3"
mixed_path = paths.AUDIO / f"HorrorAudioMusicOutput{story_number}.mp3"
subs_path = paths.VIDEOS / f"HorrorStory{story_number}.ass"
output_path = paths.VIDEOS / f"HorrorStory{story_number}.mp4"

# temp file still ends with .mp4 so ffmpeg knows the container
temp_output_path = output_path.with_name(output_path.stem + ".part" + output_path.suffix)


def format_time(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes}m {seconds % 60:.2f}s"


def probe_duration(path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def rel(path) -> str:
    """Path relative to the repo root, forward slashes.

    ffmpeg filter arguments treat ':' and '\\' as syntax, so absolute Windows
    paths need escaping inside a filtergraph. Running ffmpeg with cwd=ROOT and
    passing relative paths sidesteps that entirely.
    """
    return str(path.relative_to(paths.ROOT)).replace("\\", "/")


# ======================= 1. TEXT TO SPEECH =======================
if not story_path.exists():
    print(f"[X] Input text file not found: {story_path}")
    sys.exit(1)

text = story_path.read_text(encoding="utf-8")

print("Loaded text from:", story_path)
print("Saving audio to:", voice_path)
print(f"Voice={VOICE} Rate={RATE} Pitch={PITCH} Volume={VOLUME}")

tts_start = time.perf_counter()


async def synthesize():
    """Stream the narration, collecting word boundaries as they arrive.

    edge-tts reports exactly when it spoke each word, so there is no need to run
    speech recognition over our own output to recover timings.
    """
    tts = edge_tts.Communicate(
        text=text,
        voice=VOICE,
        rate=RATE,
        pitch=PITCH,
        volume=VOLUME,
        # Defaults to SentenceBoundary, which is far too coarse for subtitles.
        boundary="WordBoundary",
    )
    sub_maker = edge_tts.SubMaker()

    with open(voice_path, "wb") as f:
        async for chunk in tts.stream():
            if chunk["type"] == "audio":
                # "data" is NotRequired on TTSChunk, and a type checker cannot
                # narrow the TypedDict from the "type" value, so read it through
                # .get(). edge-tts does emit empty audio chunks; skipping them is
                # correct, and writing None would raise.
                data = chunk.get("data")
                if data:
                    f.write(data)
            elif chunk["type"] == "WordBoundary":
                sub_maker.feed(chunk)

    return sub_maker


sub_maker = asyncio.run(synthesize())
tts_time = time.perf_counter() - tts_start

words = subtitles.words_from_cues(sub_maker.cues)
lines = subtitles.group_words(words)
print(f"[OK] Audio saved: {voice_path}  ({len(words)} words -> {len(lines)} subtitle lines)")

if not lines:
    print("[X] No word boundaries returned by edge-tts; cannot build subtitles.")
    sys.exit(1)


# ======================= 2. BACKGROUND MUSIC =======================
print(f"Mixing music into: {mixed_path}")
mix_start = time.perf_counter()

# Lower music volume, loop it to match voice length, then mix.
# duration=first keeps the output the same length as the narration, so the word
# timings captured above stay valid.
subprocess.run(
    [
        "ffmpeg", "-y", "-v", "error",
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
mix_time = time.perf_counter() - mix_start
print(f"[OK] Mixed audio: {mixed_path}")


# ======================= 3. RENDER =======================
audio_duration = probe_duration(mixed_path)
video_duration = probe_duration(paths.BACKGROUND_VIDEO)

if video_duration <= audio_duration:
    start_time = 0.0
else:
    start_time = random.uniform(0, video_duration - audio_duration)

print(f"Saving video to (temp): {temp_output_path}")
print(f"Final video will be:    {output_path}")
print(f"Renderer: {args.renderer}  |  {audio_duration:.1f}s @ {FPS}fps "
      f"from offset {start_time:.1f}s")

render_start = time.perf_counter()

if args.renderer == "ffmpeg":
    subtitles.write_ass(lines, subs_path)

    # crop to the 9:16 region FIRST, then scale up. Scaling first would build a
    # 3413x1920 intermediate frame and then discard two thirds of it.
    vf = (
        f"[0:v]crop='min(iw,ih*9/16)':ih,"
        f"scale={TARGET_W}:{TARGET_H},"
        f"ass={rel(subs_path)}:fontsdir=assets[v]"
    )

    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-stats",
            # -ss before -i is input seeking: cheap even far into a 69-minute file
            "-ss", f"{start_time:.3f}", "-t", f"{audio_duration:.3f}",
            "-i", rel(paths.BACKGROUND_VIDEO),
            "-i", rel(mixed_path),
            "-filter_complex", vf,
            "-map", "[v]", "-map", "1:a",
            "-r", str(FPS),
            "-c:v", "h264_nvenc", "-preset", "p4", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            rel(temp_output_path),
        ],
        cwd=str(paths.ROOT),
        check=True,
    )

else:
    # Legacy path, kept only for visual A/B comparison against the ffmpeg output.
    # Uses the same subtitle lines, so the only variable is the renderer.
    from moviepy import AudioFileClip, CompositeVideoClip, TextClip, VideoFileClip

    SUB_W = int(TARGET_W * 0.92)
    SUB_H = int(TARGET_H * 0.05)

    video_clip = VideoFileClip(str(paths.BACKGROUND_VIDEO)).without_audio()
    segment = video_clip.subclipped(start_time, start_time + audio_duration)

    w, h = segment.size
    if (w / h) > (TARGET_W / TARGET_H):
        segment = segment.resized(height=TARGET_H)
    else:
        segment = segment.resized(width=TARGET_W)
    w2, h2 = segment.size
    segment = segment.cropped(
        x1=int((w2 - TARGET_W) / 2), y1=int((h2 - TARGET_H) / 2),
        width=TARGET_W, height=TARGET_H,
    ).with_position(("center", "center"))

    text_clips = [
        TextClip(
            text=l.text + "\n",
            method="caption",
            font_size=subtitles.FONT_SIZE,
            size=(SUB_W, SUB_H),
            stroke_width=subtitles.OUTLINE,
            stroke_color="black",
            font=str(paths.FONT),
            color="white",
            text_align="center",
            interline=6,
            margin=(20, 15),
        ).with_position(("center", "center")).with_start(l.start).with_end(l.end)
        for l in lines
    ]

    audio = AudioFileClip(str(mixed_path)).with_start(0).with_duration(segment.duration)
    final = CompositeVideoClip([segment] + text_clips, size=(TARGET_W, TARGET_H)).with_audio(audio)

    final.write_videofile(
        str(temp_output_path),
        codec="h264_nvenc", audio=True, audio_codec="aac", fps=FPS, preset="p4",
        ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
        temp_audiofile=str(paths.VIDEOS / f"temp-audio-{story_number}.m4a"),
        remove_temp=True,
    )

    final.close()
    segment.close()
    video_clip.close()
    audio.close()

render_time = time.perf_counter() - render_start

# Atomic finalize: rename .part -> .mp4 so n8n only sees a complete file
temp_output_path.replace(output_path)
print(f"[OK] Finalized video: {output_path}")

total = tts_time + mix_time + render_time
print("\n====== PERFORMANCE SUMMARY ======")
print(f"TTS + word timings:  {format_time(tts_time)}")
print(f"Music mix:           {format_time(mix_time)}")
print(f"Render ({args.renderer:<7}):     {format_time(render_time)}")
print(f"Total:               {format_time(total)}")
print("================================\n")


# ======================= 4. NOTIFY n8n =======================
# Event-driven handoff: publishing starts when the render actually finishes,
# rather than after a fixed sleep that cannot observe it.
webhook = paths.render_webhook()
if not should_notify:
    print(f"[i] Not notifying n8n (manual run for story {story_number}); "
          f"pass --notify to publish it.")
elif webhook:
    try:
        import urllib.request

        payload = json.dumps({
            "story_number": story_number,
            "story_file_name": f"HorrorStory{story_number}",
            "video_path": str(output_path),
            "metadata_path": str(paths.METADATA / f"HorrorStory{story_number}_metadata.json"),
            "duration_seconds": round(audio_duration, 2),
            "render_seconds": round(render_time, 2),
        }).encode("utf-8")

        req = urllib.request.Request(
            webhook, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"[OK] Notified n8n ({resp.status}): {webhook}")
    except Exception as e:
        # The video is already on disk and finalized; a failed notification is
        # recoverable by hand, so do not fail the run over it.
        print(f"[!] Could not notify n8n at {webhook}: {e}")
else:
    print("[i] N8N_RENDER_WEBHOOK not set; skipping publish notification.")
