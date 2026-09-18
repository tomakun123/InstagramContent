"""Split the narration into "beats": 10-15 second stretches that each get their
own generated background clip.

Input is the same word list the subtitles are built from (edge-tts word
boundaries), so a beat boundary is always a spoken-word boundary and the clip
changes never land mid-word. Where possible a beat ends on a sentence end, so
the cut reads as intentional rather than arbitrary.
"""
import re
from dataclasses import dataclass
from typing import List, Sequence, Set, Tuple

# Seconds. image/video styles spend one generation per beat (a still with a
# camera move, or a ~3.4 s clip ping-ponged and stretched); the film style
# covers a beat with 2-3 chained 5 s image-to-video shots (clips.SHOT_S), so
# this range keeps every beat at a handful of shots.
TARGET_LEN = 12.0
MIN_LEN = 10.0
MAX_LEN = 15.0

SENTENCE_END = (".", "!", "?", '."', '!"', '?"', ".'", "!'", "?'", ".”", "!”", "?”")

_ALNUM = re.compile(r"[^0-9a-z]+")


def _key(token: str) -> str:
    return _ALNUM.sub("", token.lower())


def sentence_ends(story_text: str, words: Sequence[Tuple[float, float, str]]) -> Set[int]:
    """Indices of spoken words that end a sentence in the written story.

    edge-tts strips punctuation from its WordBoundary text, so the cues alone
    cannot tell "woods." from "woods". Walk the story's whitespace tokens in
    step with the cues, matching on letters/digits only, and note which cue
    lands on a token that ends in terminal punctuation. Tokens the TTS split
    or merged differently are skipped over with a small lookahead; a cue that
    cannot be matched simply is not a sentence end, which only costs one
    possible cut point.
    """
    tokens = story_text.split()
    ends: Set[int] = set()
    t = 0
    for i, (_, _, spoken) in enumerate(words):
        k = _key(spoken)
        if not k:
            continue
        for look in range(t, min(t + 4, len(tokens))):
            if _key(tokens[look]) == k:
                if tokens[look].endswith(SENTENCE_END):
                    ends.add(i)
                t = look + 1
                break
    return ends


@dataclass(frozen=True)
class Beat:
    """One background clip's slot: when it is on screen and what is narrated."""
    start: float   # seconds
    end: float     # seconds
    text: str

    @property
    def duration(self) -> float:
        return self.end - self.start


def segment(
    words: Sequence[Tuple[float, float, str]],
    audio_end: float,
    ends: Set[int] = frozenset(),
    target: float = TARGET_LEN,
    min_len: float = MIN_LEN,
    max_len: float = MAX_LEN,
) -> List[Beat]:
    """Group timed words into beats of roughly `target` seconds.

    A beat is flushed at the first sentence end (`ends`, word indices from
    `sentence_ends`) once it is at least `target` long. If no sentence end
    arrives before `max_len`, the beat is cut back to the last sentence end
    that still leaves it at least `min_len` long, or, failing that, at the
    word boundary before the overflow. Beats tile the whole narration: the
    first starts at 0 and the last ends at `audio_end`, so the concatenated
    clips cover the entire audio track with no gap for the render to fill.
    """
    if not words:
        return []

    def text_of(idx: Sequence[int]) -> str:
        return " ".join(words[i][2] for i in idx)

    beats: List[Beat] = []
    start = 0.0
    chunk: List[int] = []

    for i in range(len(words)):
        w_start, w_end, _ = words[i]

        if chunk and (w_end - start) > max_len:
            # Overflow. Prefer the latest sentence end that keeps the beat long
            # enough; otherwise cut right before this word.
            cut = None
            for j in reversed(chunk):
                if j in ends and words[j][1] - start >= min_len:
                    cut = j
                    break
            if cut is None:
                beats.append(Beat(start, w_start, text_of(chunk)))
                start = w_start
                chunk = []
            else:
                keep = chunk[: chunk.index(cut) + 1]
                rest = chunk[chunk.index(cut) + 1:]
                beats.append(Beat(start, words[cut + 1][0], text_of(keep)))
                start = words[cut + 1][0]
                chunk = rest

        chunk.append(i)
        is_last = i == len(words) - 1

        if not is_last and (w_end - start) >= target and i in ends:
            # a clean cut; the next word begins the next beat
            beats.append(Beat(start, words[i + 1][0], text_of(chunk)))
            start = words[i + 1][0]
            chunk = []

    # The last word never triggers the sentence flush above, so there is always
    # an open chunk here; it runs to the end of the audio, not the last word.
    beats.append(Beat(start, audio_end, text_of(chunk)))

    # A stubby tail (a sentence or two after the last clean cut) would get a
    # clip that is on screen for only a few seconds. Fold it into the beat
    # before it, then, if that made the beat too long, split the pair evenly.
    if len(beats) >= 2 and beats[-1].duration < min_len:
        tail = beats.pop()
        prev = beats.pop()
        merged = Beat(prev.start, tail.end, prev.text + " " + tail.text)
        if merged.duration <= max_len:
            beats.append(merged)
        else:
            beats.extend(_halve(merged, words, ends))

    return beats


def _halve(beat: Beat, words, ends: Set[int]) -> List[Beat]:
    """Split a beat at the word boundary nearest its midpoint, preferring a
    sentence end when one lies within a quarter of the beat of the middle."""
    mid = beat.start + beat.duration / 2
    inside = [i for i, (ws, we, _) in enumerate(words)
              if ws >= beat.start and we <= beat.end and i + 1 < len(words)]
    if len(inside) < 2:
        return [beat]

    def dist(i):
        return abs(words[i + 1][0] - mid)

    sentence = [i for i in inside if i in ends and dist(i) <= beat.duration / 4]
    cut = min(sentence or inside, key=dist)
    split = words[cut + 1][0]
    first = [words[i][2] for i in inside if i <= cut]
    second = [words[i][2] for i in inside if i > cut]
    return [Beat(beat.start, split, " ".join(first)),
            Beat(split, beat.end, " ".join(second))]


if __name__ == "__main__":
    # Self-test against a real story's narration timings, without rendering.
    import asyncio
    import subprocess
    import sys

    import edge_tts

    import paths
    import subtitles

    n = int(sys.argv[1]) if len(sys.argv) > 1 else paths.story_number()
    story = (paths.STORIES / f"HorrorStory{n}.txt").read_text(encoding="utf-8")

    async def timings():
        # same voice settings as generateContent so the timings match a render
        tts = edge_tts.Communicate(story, voice="en-US-ChristopherNeural",
                                   rate="+40%", pitch="-10Hz", volume="+50%",
                                   boundary="WordBoundary")
        sm = edge_tts.SubMaker()
        out = paths.LOGS / "beats-selftest.mp3"
        with open(out, "wb") as f:
            async for chunk in tts.stream():
                if chunk["type"] == "audio" and chunk.get("data"):
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    sm.feed(chunk)
        return sm, out

    sm, audio = asyncio.run(timings())
    words = subtitles.words_from_cues(sm.cues)
    end = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio)],
        check=True, capture_output=True, text=True).stdout)

    ends = sentence_ends(story, words)
    print(f"{len(words)} words, {len(ends)} sentence ends, {end:.2f}s audio")
    for k, b in enumerate(segment(words, end, ends)):
        print(f"beat {k}: {b.start:6.2f} -> {b.end:6.2f}  ({b.duration:4.1f}s)  {b.text[:70]}...")
