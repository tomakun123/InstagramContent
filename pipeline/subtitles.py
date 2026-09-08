"""Subtitle generation.

Word timings come from edge-tts itself (WordBoundary events), not from running
ASR over our own synthesised speech. Words are grouped into short on-screen
lines and emitted as an ASS file, which ffmpeg burns in via libass.

The grouping is the same rule the MoviePy renderer used: accumulate words until
adding the next one would exceed `max_chars`, then flush.
"""
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, List, Sequence, Tuple

# Matches the previous MoviePy TextClip styling.
DEFAULT_MAX_CHARS = 30
PLAY_RES = (1080, 1920)
FONT_NAME = "Arial Black"   # the internal family name inside assets/use.ttf

# Calibrated, not copied. MoviePy's font_size is PIL's em size in pixels;
# libass sizes glyphs differently, so the old font_size=48 renders ~1.4x too
# small in ASS. 68 was measured to reproduce the previous look: rendering the
# same string both ways gives 829px vs 820px wide and identical 45px height.
FONT_SIZE = 68
OUTLINE = 5


@dataclass(frozen=True)
class Line:
    """One subtitle line: when it appears, when it leaves, what it says."""
    start: float   # seconds
    end: float     # seconds
    text: str


def _to_seconds(value) -> float:
    if isinstance(value, timedelta):
        return value.total_seconds()
    return float(value)


def words_from_cues(cues: Iterable) -> List[Tuple[float, float, str]]:
    """Normalise edge-tts SubMaker cues into (start, end, text) tuples.

    SubMaker yields one cue per word, with timedelta offsets.
    """
    words = []
    for cue in cues:
        text = (cue.content or "").strip()
        if not text:
            continue
        words.append((_to_seconds(cue.start), _to_seconds(cue.end), text))
    return words


def group_words(
    words: Sequence[Tuple[float, float, str]],
    max_chars: int = DEFAULT_MAX_CHARS,
) -> List[Line]:
    """Group words into lines of at most `max_chars` characters."""
    lines: List[Line] = []
    if not words:
        return lines

    current = ""
    start = words[0][0]
    end = words[0][1]

    for w_start, w_end, text in words:
        if len(current) + len(text) + 1 <= max_chars:
            if current:
                current += " "
            else:
                # first word of a fresh line sets its start
                start = w_start
            current += text
            end = w_end
        else:
            if current:
                lines.append(Line(start, end, current))
            current = text
            start = w_start
            end = w_end

    if current:
        lines.append(Line(start, end, current))

    return lines


def _ass_time(seconds: float) -> str:
    """ASS timestamps are H:MM:SS.cc (centiseconds)."""
    if seconds < 0:
        seconds = 0.0
    total_cs = int(round(seconds * 100))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    """Braces introduce override tags in ASS, and newlines must be explicit."""
    return (
        text.replace("\\", "\\\\")
            .replace("{", "\\{")
            .replace("}", "\\}")
            .replace("\n", " ")
            .strip()
    )


def build_ass(lines: Sequence[Line]) -> str:
    """Render subtitle lines as a complete ASS file.

    Styling mirrors the previous TextClip settings: 48px Arial Black, white with
    a 5px black outline, centred both horizontally and vertically. PlayRes is set
    to the output resolution so font sizes map 1:1 rather than being scaled.
    """
    width, height = PLAY_RES

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{FONT_NAME},{FONT_SIZE},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{OUTLINE},0,5,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events = [
        f"Dialogue: 0,{_ass_time(l.start)},{_ass_time(l.end)},Sub,,0,0,0,,{_ass_escape(l.text)}"
        for l in lines
    ]

    return header + "\n".join(events) + "\n"


def write_ass(lines: Sequence[Line], path) -> None:
    # UTF-8 with BOM: libass is more reliable at detecting encoding with it.
    with open(path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write(build_ass(lines))
