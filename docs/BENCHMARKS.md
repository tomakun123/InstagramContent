# Render benchmarks

Timings for a single ~3-minute 1080x1920 Short on an RTX 3050.

| Change | Subtitles | Video render | Total |
|---|---|---|---|
| Baseline (CPU) | — | — | ~30m |
| Moved Whisper + render to CUDA | 75.12s | 1435.95s | 25m 11s |
| Changed `TextClip` size (text slightly cut off) | 1m 10.95s | 17m 45.12s | 18m 56s |
| Added `interline` and `margin` | 1m 06.16s | 17m 51.32s | 18m 57s |
| Further `TextClip` tuning | 1m 07.29s | 19m 12.11s | 20m 19s |
| Doubled `max_chars_per_clip` (25 -> 50) — not worth it | 1m 08.45s | 18m 47.67s | 19m 56s |

## Reading these numbers

The "Video render" column is labelled NVENC, but that is misleading. NVENC encoding
a 3-minute 1080x1920 clip takes well under a minute on this hardware. The ~18
minutes is **MoviePy compositing in Python**: it decodes the 2.3 GB background
file, composites 60-100 `TextClip` overlays per frame through PIL, and hands NVENC
one finished frame at a time. The GPU spends most of that time idle.

This is why the tuning above barely moved the needle — every row is optimising
subtitle appearance, not the actual bottleneck.

## Next optimisation (not yet done)

Take the compositing out of Python entirely:

1. Convert the `whisper_timestamped` word timings into an `.ass` subtitle file.
2. Do the crop, subtitle burn-in, and audio mux in one `ffmpeg` invocation
   (`-vf "crop=...,ass=subs.ass"`), letting libass render text in C.

Expected to bring the render step to roughly real-time or faster, and it removes
the GPU requirement — which would make cloud hosting viable on a cheap CPU VPS.
