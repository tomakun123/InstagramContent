# Render benchmarks

Timings for a single ~106-second 1080x1920 Short on an RTX 3050.

## After the ffmpeg rewrite (current)

Measured on story 36, repeated runs:

```
TTS + word timings:  0m 1.91s     (edge-tts, includes subtitle timings)
Music mix:           0m 1.39s     (ffmpeg amix)
Render (ffmpeg):     0m 13.24s    (crop, scale, libass burn-in, NVENC)
Total:               0m 16.54s
```

**~19 minutes -> ~17 seconds**, about a 70x improvement on the render step.

## Before (MoviePy)

| Change | Subtitles | Video render | Total |
|---|---|---|---|
| Baseline (CPU) | - | - | ~30m |
| Moved Whisper + render to CUDA | 75.12s | 1435.95s | 25m 11s |
| Changed `TextClip` size | 1m 10.95s | 17m 45.12s | 18m 56s |
| Added `interline` and `margin` | 1m 06.16s | 17m 51.32s | 18m 57s |
| Further `TextClip` tuning | 1m 07.29s | 19m 12.11s | 20m 19s |
| Doubled `max_chars_per_clip` (25 -> 50) | 1m 08.45s | 18m 47.67s | 19m 56s |

Every row in that table optimises subtitle *appearance*. None of them touched the
actual bottleneck, which is why they all land within two minutes of each other.

## What the time was actually going to

The "Video render" column was labelled NVENC, but NVENC was mostly idle. The cost
was MoviePy compositing in Python: decoding the 2.3 GB background, drawing 60-100
`TextClip` overlays per frame through PIL, and handing NVENC one finished frame at
a time - about 170 ms per frame across 6,354 frames.

Isolating the pieces with `-f null` (nothing written) confirmed it:

| Filter chain | Time |
|---|---|
| `scale=-2:1920,crop=1080:1920` @ 60 fps (MoviePy's geometry) | 21.2 s |
| `crop=608:1080,scale=1080:1920` @ 30 fps | 11.0 s |
| ...same, plus styled text burn-in | **11.04 s** |

Burning in text costs **0.04 seconds** in ffmpeg and ~17 minutes in MoviePy.

## The three changes

1. **Compositing moved into ffmpeg.** Subtitles are emitted as an ASS file and
   burned in by libass inside the filter graph, in C, instead of being composited
   per-frame in Python.
2. **30 fps instead of 60.** The source is 60 fps and the output inherited that.
   Static subtitles over gameplay do not need it, and Shorts/Reels/TikTok do not
   reward it. Halves the frames and shrinks the file (31.4 MB -> 29.2 MB).
3. **Crop before scale.** The old path upscaled 1920x1080 to 3413x1920 and then
   discarded 68% of every frame. Cropping to the 9:16 region first is identical
   output for roughly half the pixel work.

## Whisper removed entirely

The pipeline used to synthesise speech and then run ASR over its own output to
find out where the words were. edge-tts reports word boundaries directly
(`Communicate(..., boundary="WordBoundary")` - note it defaults to
`SentenceBoundary`, which is too coarse). This removed ~70 s per run plus a
460 MB model load, dropped `torch`, `openai-whisper` and `whisper-timestamped`
from the dependency list, and made timings exact rather than a re-recognition of
synthesised audio.

Measured on story 36: 386 words -> 80 subtitle lines, covering 99.4% of the
audio, no overlapping lines, mean gap between lines 0.055 s.

## Subtitle styling note

`Fontsize` in ASS is not the same unit as MoviePy's `font_size`. The old
`font_size=48` renders ~1.4x too small in libass. `subtitles.FONT_SIZE = 68` was
calibrated by rendering the same string through both paths and comparing: 829 px
vs 820 px wide, identical 45 px height, both centred at x=540.
