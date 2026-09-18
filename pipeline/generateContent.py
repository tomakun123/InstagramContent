"""End-to-end content pipeline: story text -> narration -> music mix -> rendered Short.

Runs as a single pass for the story number currently in HorrorStories/counter.txt:

  1. TTS        - edge_tts narration of HorrorStories/HorrorStory{n}.txt, capturing
                  word boundaries as it streams (no separate transcription step)
  2. Background - split the narration into 10-15 s beats, have LM Studio write a
                  shot description for each, unload the story model, generate one
                  shot per beat in ComfyUI (a Flux still animated with a slow
                  camera move by default, a Wan 2.2 clip with --style video, or
                  a still plus chained Wan image-to-video shots with --style
                  film), stitch them, reload the model. Any failure here falls back to
                  a random slice of the Minecraft footage so a broken generator
                  never blocks publishing.
  3. Music      - ffmpeg mix of the narration with looped background music
  4. Render     - one ffmpeg pass: crop the background to 9:16, scale to 1080x1920,
                  burn in the subtitles via libass, mux the audio, encode with NVENC

The final file is written to a .part.mp4 and only renamed to .mp4 once the encode
completes, so downstream consumers never observe a partial file.
"""
import argparse
import asyncio
import json
import random
import shutil
import subprocess
import sys
import time

import edge_tts
from dotenv import load_dotenv

import beats as beats_mod
import clips
import paths
import subtitles
import visualPrompts

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
parser.add_argument(
    "--background",
    choices=["ai", "minecraft"],
    default=paths.background_mode(),
    help="ai: generate a background per beat with ComfyUI (Minecraft on failure); "
         "minecraft: skip generation. Default from BACKGROUND_MODE in .env.",
)
parser.add_argument(
    "--style",
    choices=["image", "video", "film"],
    default=paths.background_style(),
    help="image: Flux still + slow camera move per beat (sharp, ~1 min/beat); "
         "video: Wan 2.2 clip per beat, ping-ponged (real motion, soft, ~5 min/beat); "
         "film: Flux still + chained Wan 2.2 image-to-video shots per beat "
         "(continuous motion, needs a distilled 4-step model; see SETUP section 10). "
         "Default from BACKGROUND_STYLE in .env.",
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


def lms(*cmd_args) -> None:
    """Run an `lms` CLI command, or do nothing if the CLI is not installed."""
    exe = shutil.which("lms")
    if not exe:
        print("[!] lms CLI not on PATH; cannot manage LM Studio's VRAM")
        return
    # lms prints UTF-8 progress glyphs; the console default (cp1252) chokes on them.
    subprocess.run([exe, *cmd_args], check=False, capture_output=True,
                   encoding="utf-8", errors="replace")


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

narration_duration = probe_duration(voice_path)


# ======================= 2. BACKGROUND CLIPS =======================
# Order matters: the shot descriptions need the story model, the video model
# needs the VRAM the story model is holding (7 GB of 8). So: prompts first,
# then unload, generate, and reload before the render so the next n8n run does
# not wait on a cold model.
background = None          # None -> Minecraft fallback
prompt_time = clip_time = 0.0
beat_count = 0

if args.background == "ai":
    llm_unloaded = False
    try:
        beat_list = beats_mod.segment(
            words, narration_duration, beats_mod.sentence_ends(text, words))
        beat_count = len(beat_list)
        print(f"Background ({args.style}): {beat_count} beats -> "
              + ", ".join(f"{b.duration:.1f}s" for b in beat_list))

        # Check the generator is there before spending an LLM round-trip on
        # prompts nothing will consume.
        if not clips.is_available():
            raise clips.ClipError(f"ComfyUI not reachable at {paths.comfy_url()}")

        prompt_start = time.perf_counter()
        prompts = visualPrompts.for_beats(text, beat_list, args.style)
        prompt_time = time.perf_counter() - prompt_start
        prefix_len = len(visualPrompts.style_prefix(args.style))
        for k, pr in enumerate(prompts):
            print(f"  beat {k}: {pr[prefix_len:]}")

        lms("unload", "--all")
        llm_unloaded = True

        clip_start = time.perf_counter()
        background = clips.build_background(
            story_number, beat_list, prompts, visualPrompts.NEGATIVE, args.style)
        clip_time = time.perf_counter() - clip_start
        print(f"[OK] Background clips: {background}")
    except Exception as e:  # noqa: BLE001 - any failure means "use the stock footage"
        print(f"[!] AI background failed ({type(e).__name__}: {e}); "
              f"falling back to {paths.BACKGROUND_VIDEO.name}")
        background = None
    finally:
        if llm_unloaded:
            clips.free_gpu()
            lms("load", paths.lms_model(), "-y")
else:
    print("Background: minecraft (--background minecraft)")


# ======================= 3. BACKGROUND MUSIC =======================
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


# ======================= 4. RENDER =======================
audio_duration = probe_duration(mixed_path)

if background is not None:
    # generated clips tile the narration exactly, so play them from the top
    background_video = background
    start_time = 0.0
else:
    background_video = paths.BACKGROUND_VIDEO
    video_duration = probe_duration(background_video)
    if video_duration <= audio_duration:
        start_time = 0.0
    else:
        start_time = random.uniform(0, video_duration - audio_duration)

print(f"Saving video to (temp): {temp_output_path}")
print(f"Final video will be:    {output_path}")
print(f"Renderer: {args.renderer}  |  {audio_duration:.1f}s @ {FPS}fps "
      f"from offset {start_time:.1f}s of {background_video.name}")

render_start = time.perf_counter()

if args.renderer == "ffmpeg":
    subtitles.write_ass(lines, subs_path)

    # crop to the 9:16 region FIRST, then scale up. Scaling first would build a
    # 3413x1920 intermediate frame and then discard two thirds of it.
    vf = (
        f"[0:v]crop='min(iw,ih*9/16)':ih,"
        # lanczos rather than the default bicubic: the crop is only 608px wide,
        # so this frame is always being upscaled and the scaler choice shows.
        f"scale={TARGET_W}:{TARGET_H}:flags=lanczos,"
        # A light finish on every background: a touch of sharpening for the
        # upscale, temporal film grain (hides residual softness and banding in
        # the dark areas) and a soft vignette. Before the subtitles so the
        # text stays clean.
        "unsharp=3:3:0.4,noise=alls=6:allf=t,vignette=PI/5,"
        f"ass={rel(subs_path)}:fontsdir=assets[v]"
    )

    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-stats",
            # -ss before -i is input seeking: cheap even far into a 69-minute file
            "-ss", f"{start_time:.3f}", "-t", f"{audio_duration:.3f}",
            "-i", rel(background_video),
            "-i", rel(mixed_path),
            "-filter_complex", vf,
            "-map", "[v]", "-map", "1:a",
            "-r", str(FPS),
            # Quality settings. Without an explicit rate control NVENC falls back
            # to roughly 2 Mbps, which is what both this renderer and the old
            # MoviePy one were shipping at 1080x1920 - well under YouTube's ~8
            # Mbps recommendation for 1080p30.
            #
            # Constant quality rather than a fixed -b:v, so calm scenes spend
            # less and high-motion parkour spends more. Measured over a 44s
            # segment (encode time / size / bitrate):
            #     current p4, no rate control   6.00s   11 MB   2.21 Mbps
            #     cq 21                         6.44s   69 MB   13.2 Mbps
            #     cq 23                         6.44s   54 MB   10.4 Mbps
            #     cq 25                         6.44s   42 MB   8.18 Mbps  <- here
            #     cq 27                         6.44s   33 MB   6.42 Mbps
            # so the whole upgrade costs +0.44s (+7%) of render time.
            #
            # spatial_aq matters more than the bitrate on this content: horror
            # over dark Minecraft footage is mostly low-luma, which is exactly
            # where flat quantization bands.
            "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
            "-rc", "vbr", "-cq", "25", "-b:v", "0",
            "-maxrate", "12M", "-bufsize", "18M",
            "-spatial_aq", "1", "-aq-strength", "8", "-rc-lookahead", "20",
            "-pix_fmt", "yuv420p",
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

    video_clip = VideoFileClip(str(background_video)).without_audio()
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

total = tts_time + prompt_time + clip_time + mix_time + render_time
print("\n====== PERFORMANCE SUMMARY ======")
print(f"TTS + word timings:  {format_time(tts_time)}")
if background is not None:
    kind = {"image": "Stills", "video": "Clips"}.get(args.style, "Shots")
    print(f"Shot prompts (LLM):  {format_time(prompt_time)}")
    print(f"{kind} ({beat_count} beats):    {format_time(clip_time)}")
print(f"Music mix:           {format_time(mix_time)}")
print(f"Render ({args.renderer:<7}):     {format_time(render_time)}")
print(f"Total:               {format_time(total)}")
print("================================\n")


# ======================= 5. NOTIFY n8n =======================
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
            "background": args.style if background is not None else "minecraft",
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
