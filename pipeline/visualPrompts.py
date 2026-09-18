"""Turn each beat of the story into an image or video prompt.

One request to LM Studio for the whole story rather than one per beat: the
model sees every beat at once, so the shots share a setting and a look instead
of drifting from "forest" to "warehouse" between clips, and the 12B model is
only round-tripped once before it is unloaded to make room for the video model.

The LLM writes the *scene*; the fixed style prefix and NEGATIVE prompt below
carry the look, so every video on the channel is graded the same way. The
prefix differs by background style: a still wants photographic language and
no camera direction, a clip wants a slow move.
"""
import json
import re
import urllib.request
from typing import List, Sequence

import paths
from beats import Beat

# Prepended to every prompt the model writes.
# Measured on Wan 2.2 5B: stacking "dark", "dim" and "deep shadows" drove the
# clips to a near-black average luma of ~28/255. One mood word and a visible
# light source keep them readable under the white subtitles.
STYLE_IMAGE = (
    "cinematic horror film still, 35mm photograph, moody, cold blue moonlight, "
    "atmospheric fog, desaturated colour grade, subtle film grain, "
    "shallow depth of field, highly detailed, "
)
STYLE_VIDEO = (
    "cinematic horror film, moody, cold blue moonlight, atmospheric fog, "
    "desaturated colour grade, subtle film grain, slow creeping camera movement, "
)
STYLE = STYLE_VIDEO   # kept for callers that predate the image style


def style_prefix(style: str) -> str:
    # film starts from a Flux still, so its LLM prompt is an image prompt
    return STYLE_VIDEO if style == "video" else STYLE_IMAGE


# Camera moves for the film style's image-to-video shots, rotated per shot so
# the 2-3 shots of a beat differ in motion but not in subject. Written in
# code, not by the LLM: the model would invent new objects mid-beat, and the
# still already fixes the scene.
MOTIONS = [
    "slow dolly forward, fog drifting through the frame",
    "slow pan to the left, the light flickering faintly",
    "slow pull back, shadows deepening at the edges",
    "gentle handheld drift, dust and mist moving in the light",
    "slow tilt upward, the light source swaying slightly",
    "slow pan to the right, fog thickening in the distance",
]


def motion_prompt(scene: str, k: int, j: int) -> str:
    """Prompt for shot j of beat k: the channel look, the still's scene, one move."""
    return f"{STYLE_VIDEO}{scene.rstrip('. ')}. {MOTIONS[(k + j) % len(MOTIONS)]}"
# The clip sits under burned-in subtitles and a voice-over, so anything that
# competes with them - text, faces talking, bright flat light - is unwanted.
NEGATIVE = (
    "text, subtitles, captions, watermark, logo, talking, speech, lips moving, "
    "bright daylight, overexposed, cartoon, anime, low quality, blurry, "
    "distorted hands, extra limbs, static image, still frame"
)
# Continuation shots start on the previous shot's last frame and must stay in
# the same place - a scene change reads as a glitch, not a cut.
NEGATIVE_I2V = NEGATIVE + ", scene change, cut, new location, fast motion, jump"

MAX_PROMPT_WORDS = 60
TIMEOUT_S = 180
ATTEMPTS = 2
# The model tends to run past its own 40-word limit; at ~1.5 tokens a word
# plus JSON punctuation a beat needs ~100 tokens, so give it a wide margin -
# a truncated array is unparseable and costs a whole retry.
MAX_TOKENS_PER_BEAT = 250

_SYSTEM_COMMON = (
    "Never describe sound, dialogue, text, or a person's face. Never use the "
    "words 'video', 'camera' as a subject, or quotation marks. Every shot "
    "must contain one visible light source (moon, lamp, screen, flashlight, "
    "fire) so it is not pitch black. Keep the same location and colour "
    "palette across all shots unless the story clearly moves. Reply with a "
    "JSON array of strings only."
)
SYSTEM_IMAGE = (
    "You write shot descriptions for an AI image generator. Each description "
    "is one sentence of at most 40 words: one strong composition - setting, "
    "time of day, light, one or two objects, foreground and background. "
    + _SYSTEM_COMMON
)
SYSTEM_VIDEO = (
    "You write shot descriptions for an AI video generator. Each description "
    "is one sentence of at most 40 words: a concrete visual scene - setting, "
    "time of day, light, one or two objects, and one slow camera move. "
    + _SYSTEM_COMMON
)


def _template(beat: Beat) -> str:
    """Fallback prompt when the model is unavailable or returns garbage."""
    words = beat.text.split()[:MAX_PROMPT_WORDS]
    return "a dark scene inspired by: " + " ".join(words)


def _extract_array(raw: str, n: int) -> List[str]:
    """Pull a JSON array of n strings out of a possibly messy completion.

    The story model is known to wrap JSON in code fences and to leak its stop
    tokens (</s>, [TOOL_CALLS]) as text; the n8n metadata parser tolerates the
    same, so this does too.
    """
    for marker in ("</s>", "[TOOL_CALLS]", "<|im_end|>"):
        raw = raw.split(marker)[0]
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.S)
    m = re.search(r"\[.*\]", raw, flags=re.S)
    if not m:
        raise ValueError("no JSON array in completion")
    arr = json.loads(m.group(0))
    if not isinstance(arr, list) or len(arr) != n or not all(isinstance(x, str) for x in arr):
        raise ValueError(f"expected {n} strings, got {arr!r:.200}")
    return [x.strip() for x in arr]


def _ask(story_text: str, beats: Sequence[Beat], style: str) -> List[str]:
    numbered = "\n".join(f"{i + 1}. {b.text}" for i, b in enumerate(beats))
    user = (
        f"Full story, for context:\n{story_text.strip()}\n\n"
        f"Write exactly {len(beats)} shot descriptions, one for each numbered "
        f"passage below, in order:\n{numbered}"
    )
    payload = json.dumps({
        "model": paths.lms_model(),
        "messages": [{"role": "system",
                      "content": SYSTEM_VIDEO if style == "video" else SYSTEM_IMAGE},
                     {"role": "user", "content": user}],
        "temperature": 0.7,
        "max_tokens": MAX_TOKENS_PER_BEAT * len(beats) + 100,
    }).encode("utf-8")

    req = urllib.request.Request(
        paths.lms_url() + "/chat/completions", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return _extract_array(body["choices"][0]["message"]["content"], len(beats))


def for_beats(story_text: str, beats: Sequence[Beat], style: str = "image") -> List[str]:
    """One full prompt (style prefix + scene) per beat. Never raises: on any
    failure the scene falls back to a template built from the beat's words,
    so a flaky LLM degrades the pictures rather than stopping the render."""
    scenes = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            scenes = _ask(story_text, beats, style)
            break
        except Exception as e:  # noqa: BLE001 - deliberately broad, see docstring
            print(f"[!] Visual prompts: attempt {attempt}/{ATTEMPTS} failed ({e})")
    if scenes is None:
        print("[!] Visual prompts: using template prompts")
        scenes = [_template(b) for b in beats]

    prefix = style_prefix(style)
    prompts = []
    for scene in scenes:
        scene = " ".join(scene.split()[:MAX_PROMPT_WORDS])
        prompts.append(prefix + scene)
    return prompts
