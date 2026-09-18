"""Background generation and assembly: one scene per story beat.

Three styles, all driven through ComfyUI's HTTP API the same way n8n drives LM
Studio (queue a workflow, poll for the result, fetch the file - nothing here
imports torch; the models run in ComfyUI's own process):

  image            Flux.1-schnell still per beat, then an ffmpeg Ken Burns
                   move (slow push-in / pull-out / pan) for the beat's whole
                   duration. Sharp at 1080x1920 and ~1 min per beat.
  video            Wan 2.2 5B text-to-video clip per beat (~3.4 s), ping-ponged
                   and stretched to the beat. Real motion, but the same footage
                   plays forward and back several times per beat.
  film (default)   Flux still per beat as the establishing frame, then a chain
                   of Wan 2.2 5B image-to-video shots: the first animates the
                   still, each next one continues from the previous shot's
                   last frame with a different camera move, until the beat is
                   covered. Continuous motion, nothing reversed or looped.
                   Meant for a distilled 4-step checkpoint/LoRA (COMFY_LORA or
                   COMFY_UNET) so a 5 s shot costs ~1-4 min on an 8 GB card.

See assets/comfy/*.json and docs/SETUP.md section 10.
"""
import json
import os
import random
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import paths

# -------- IMAGE (Flux.1-schnell) --------
# Multiples of 16 (Flux requirement), 1.43 MP; the render's lanczos scale to
# 1080x1920 is only 1.2x, so the still stays sharp. Schnell is a 4-step model
# and ignores the negative prompt at cfg 1.
IMG_W = int(os.environ.get("COMFY_IMG_W", 896))
IMG_H = int(os.environ.get("COMFY_IMG_H", 1600))
IMG_STEPS = 4
IMG_CFG = 1.0

# -------- VIDEO (Wan 2.2 5B) --------
# Wan 2.2 5B is trained at 1280x704 / 704x1280, 24 fps, up to 121 frames, but
# on the RTX 3050 that geometry is compute-bound at ~58 s/step (25 min per
# clip, measured; fp8 weights change nothing because it is not memory-bound).
# 480x832 x 81 frames is ~11 s/step, 4.6 min per clip. Keep the model in
# fp16: a naive fp8 cast of the 5B checkpoint produced flat, near-black clips.
WIDTH = int(os.environ.get("COMFY_WIDTH", 480))
HEIGHT = int(os.environ.get("COMFY_HEIGHT", 832))
LENGTH = int(os.environ.get("COMFY_LENGTH", 81))   # frames; 4k+1 -> 3.4 s at 24 fps
FPS = 24
STEPS = int(os.environ.get("COMFY_STEPS", 20))
CFG = float(os.environ.get("COMFY_CFG", 5.0))
# Override the checkpoint file without editing the workflow.
UNET_OVERRIDE = os.environ.get("COMFY_UNET", "").strip()

# -------- FILM (Flux still -> Wan 2.2 5B image-to-video chain) --------
# Shot geometry defaults to the model's native portrait size: with a 4-step
# distilled model (COMFY_LORA / COMFY_UNET, steps 4, cfg 1) that is ~4 min per
# 5 s shot on the RTX 3050; drop to 544x960 (~1.7 min) if the story budget is
# tight. Run `python pipeline/clips.py --film "<scene>"` to measure.
FILM_W = int(os.environ.get("COMFY_FILM_W", 704))
FILM_H = int(os.environ.get("COMFY_FILM_H", 1280))
SHOT_FRAMES = int(os.environ.get("COMFY_SHOT_FRAMES", 121))   # 4k+1 -> 5 s at 24 fps
FILM_STEPS = int(os.environ.get("COMFY_FILM_STEPS", 4))
FILM_CFG = float(os.environ.get("COMFY_FILM_CFG", 1.0))
FILM_SHIFT = float(os.environ.get("COMFY_FILM_SHIFT", 8.0))
MAX_SHOTS_PER_BEAT = int(os.environ.get("MAX_SHOTS_PER_BEAT", 4))
# Distilled 4-step LoRA file name in ComfyUI's models/loras (empty: the LoRA
# node is dropped and COMFY_UNET is expected to be a distilled checkpoint).
LORA = os.environ.get("COMFY_LORA", "").strip()
LORA_STRENGTH = float(os.environ.get("COMFY_LORA_STRENGTH", 1.0))
# Where the umt5 text encoder runs. "cpu" keeps its ~6.5 GB out of VRAM so the
# whole card is left for the diffusion model; only sensible with >= 32 GB of
# system RAM (the encoder is loaded next to the 10 GB fp16 checkpoint).
TE_DEVICE = os.environ.get("COMFY_TE_DEVICE", "default").strip() or "default"
SHOT_S = SHOT_FRAMES / FPS

CLIP_TIMEOUT_S = int(os.environ.get("CLIP_TIMEOUT_S", 45 * 60))
POLL_S = 5
# A generation can be lost to a transient error or to someone pressing the
# cancel button in the ComfyUI web UI (it interrupts whatever the server is
# running, this job included). One retry with a fresh seed is cheap; giving
# up costs the whole AI background for the story.
CLIP_ATTEMPTS = 2

# -------- ASSEMBLY --------
OUT_W, OUT_H = 1080, 1920
OUT_FPS = 30            # matches generateContent's render FPS
PINGPONG_S = LENGTH / FPS * 2


class ClipError(RuntimeError):
    pass


def _http(method: str, path: str, body: Optional[dict] = None, timeout: float = 30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        paths.comfy_url() + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def is_available() -> bool:
    """True if a ComfyUI server answers at COMFYUI_URL."""
    try:
        _http("GET", "/system_stats", timeout=5)
        return True
    except (urllib.error.URLError, OSError):
        return False


def free_gpu() -> None:
    """Ask ComfyUI to drop its cached models so LM Studio can have the VRAM back."""
    try:
        _http("POST", "/free", {"unload_models": True, "free_memory": True})
    except (urllib.error.URLError, OSError) as e:
        print(f"[!] ComfyUI /free failed: {e}")


class ComfyProvider:
    """Runs one ComfyUI API-format workflow per call and fetches its output.

    `nodes` maps roles to node ids in the workflow file: positive, negative,
    sampler, save, plus whatever `patch` needs (latent, loader). If a workflow
    is re-exported from ComfyUI the ids must be checked against the new file.
    """

    def __init__(self, workflow_path: Path, nodes: Dict[str, str], ext: str,
                 patch: Dict[str, dict], drop: Sequence[str] = ()):
        self.template = json.loads(workflow_path.read_text(encoding="utf-8"))
        self.nodes = nodes
        self.ext = ext
        self.patch = patch      # role -> inputs to set on that node every run
        for role, node in nodes.items():
            if node not in self.template:
                raise ClipError(f"workflow {workflow_path.name} has no node {node} "
                                f"({role}); update the node ids in clips.py")
        for role in drop:
            self._drop_node(nodes[role])

    def _drop_node(self, node: str) -> None:
        """Remove a single-input pass-through node (e.g. an optional LoRA
        loader) and wire whatever consumed it to whatever fed it."""
        upstream = self.template[node]["inputs"]["model"]
        del self.template[node]
        for other in self.template.values():
            for key, val in other["inputs"].items():
                if isinstance(val, list) and len(val) == 2 and val[0] == node:
                    other["inputs"][key] = upstream

    def _build(self, prompt: str, negative: str, seed: int, prefix: str,
               start_image: Optional[str] = None) -> dict:
        wf = json.loads(json.dumps(self.template))   # deep copy
        n = self.nodes
        wf[n["positive"]]["inputs"]["text"] = prompt
        wf[n["negative"]]["inputs"]["text"] = negative
        wf[n["sampler"]]["inputs"]["seed"] = seed
        wf[n["save"]]["inputs"]["filename_prefix"] = prefix
        if start_image is not None:
            wf[n["image"]]["inputs"]["image"] = start_image
        for role, inputs in self.patch.items():
            wf[n[role]]["inputs"].update(inputs)
        return wf

    @staticmethod
    def upload_image(image: Path) -> str:
        """Push a PNG into ComfyUI's input folder; returns the name LoadImage wants."""
        boundary = uuid.uuid4().hex
        # unique name per upload so a re-run never picks up a stale file
        name = f"horror_{image.stem}_{boundary[:8]}.png"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
            f"filename=\"{name}\"\r\nContent-Type: image/png\r\n\r\n"
        ).encode("utf-8") + image.read_bytes() + (
            f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\""
            f"\r\n\r\ntrue\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        req = urllib.request.Request(
            paths.comfy_url() + "/upload/image", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            info = json.loads(resp.read())
        sub = info.get("subfolder", "")
        return f"{sub}/{info['name']}" if sub else info["name"]

    def generate(self, prompt: str, negative: str, out_path: Path,
                 seed: Optional[int] = None, start_image: Optional[Path] = None) -> Path:
        """Queue one generation, wait for it, and copy the output to out_path."""
        seed = seed if seed is not None else random.randrange(2**32)
        prefix = f"horror/{out_path.parent.name}_{out_path.stem}"
        uploaded = self.upload_image(start_image) if start_image is not None else None

        try:
            resp = json.loads(_http("POST", "/prompt", {
                "prompt": self._build(prompt, negative, seed, prefix, uploaded),
                "client_id": "generateContent",
            }))
        except urllib.error.HTTPError as e:
            raise ClipError(f"ComfyUI rejected the workflow: {e.read().decode('utf-8', 'replace')[:500]}")
        prompt_id = resp["prompt_id"]
        print(f"    queued {out_path.name} (seed {seed}, id {prompt_id[:8]})", flush=True)

        deadline = time.monotonic() + CLIP_TIMEOUT_S
        while True:
            time.sleep(POLL_S)
            history = json.loads(_http("GET", f"/history/{prompt_id}"))
            entry = history.get(prompt_id)
            if entry:
                break
            if time.monotonic() > deadline:
                _http("POST", "/interrupt", {})
                raise ClipError(f"{out_path.name} timed out after {CLIP_TIMEOUT_S}s")

        status = entry.get("status", {})
        if status.get("status_str") == "error":
            # An exception carries exception_message; an interrupt (cancel
            # button in the UI, or /interrupt) only carries its event type,
            # so fall back to listing the event types seen.
            kinds = []
            detail = ""
            for m in status.get("messages", []):
                if not isinstance(m, list) or not m:
                    continue
                kinds.append(str(m[0]))
                if m[0] == "execution_error" and len(m) > 1 and isinstance(m[1], dict):
                    detail = m[1].get("exception_message", "")
            if not detail:
                detail = ", ".join(k for k in kinds if k != "execution_start") or "no status messages"
            raise ClipError(f"ComfyUI execution failed: {detail[:500]}")

        # The save node reports its file under the node's outputs; the key name
        # has varied between ComfyUI versions, so look for the extension rather
        # than a specific key.
        for node_out in entry.get("outputs", {}).values():
            for files in node_out.values():
                if not isinstance(files, list):
                    continue
                for f in files:
                    name = f.get("filename", "") if isinstance(f, dict) else ""
                    if name.endswith(self.ext):
                        return self._fetch(f, out_path)
        raise ClipError(f"ComfyUI finished but produced no {self.ext} output")

    @staticmethod
    def _fetch(f: dict, out_path: Path) -> Path:
        from urllib.parse import urlencode
        q = urlencode({"filename": f["filename"], "subfolder": f.get("subfolder", ""),
                       "type": f.get("type", "output")})
        data = _http("GET", f"/view?{q}", timeout=120)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        return out_path


def image_provider(width: int = IMG_W, height: int = IMG_H) -> ComfyProvider:
    """Flux.1-schnell text-to-image (assets/comfy/flux_schnell_t2i_api.json)."""
    return ComfyProvider(
        paths.COMFY_IMAGE_WORKFLOW,
        nodes={"positive": "6", "negative": "7", "sampler": "3", "save": "9", "latent": "5"},
        ext=".png",
        patch={"latent": {"width": width, "height": height},
               "sampler": {"steps": IMG_STEPS, "cfg": IMG_CFG}},
    )


def i2v_provider() -> ComfyProvider:
    """Wan 2.2 5B image-to-video (assets/comfy/wan22_5b_i2v_api.json).

    The start image is uploaded per call; the optional LoRA node is dropped
    from the graph when COMFY_LORA is unset."""
    patch = {"latent": {"width": FILM_W, "height": FILM_H, "length": SHOT_FRAMES},
             "sampler": {"steps": FILM_STEPS, "cfg": FILM_CFG},
             "shift": {"shift": FILM_SHIFT},
             "clip": {"device": TE_DEVICE}}
    if UNET_OVERRIDE:
        patch["unet"] = {"unet_name": UNET_OVERRIDE}
    if LORA:
        patch["lora"] = {"lora_name": LORA, "strength_model": LORA_STRENGTH}
    provider = ComfyProvider(
        paths.COMFY_I2V_WORKFLOW,
        nodes={"positive": "6", "negative": "7", "sampler": "3", "save": "58",
               "latent": "55", "unet": "37", "lora": "61", "shift": "48",
               "clip": "38", "image": "60"},
        ext=".mp4",
        patch=patch,
        drop=() if LORA else ("lora",),
    )
    if UNET_OVERRIDE.lower().endswith(".gguf"):
        # A GGUF checkpoint (e.g. the 5B Turbo quants) loads through the
        # ComfyUI-GGUF custom node, which takes only the file name.
        provider.template["37"] = {
            "inputs": {"unet_name": UNET_OVERRIDE},
            "class_type": "UnetLoaderGGUF",
            "_meta": {"title": "Load Diffusion Model (GGUF)"},
        }
        patch["unet"] = {}
    return provider


def video_provider() -> ComfyProvider:
    """Wan 2.2 5B text-to-video (assets/comfy/wan22_5b_t2v_api.json)."""
    patch = {"latent": {"width": WIDTH, "height": HEIGHT, "length": LENGTH},
             "sampler": {"steps": STEPS, "cfg": CFG}}
    if UNET_OVERRIDE:
        patch["unet"] = {"unet_name": UNET_OVERRIDE}
    return ComfyProvider(
        paths.COMFY_WORKFLOW,
        nodes={"positive": "6", "negative": "7", "sampler": "3", "save": "58",
               "latent": "55", "unet": "37"},
        ext=".mp4",
        patch=patch,
    )


# Ken Burns presets, cycled by beat index. (zoom_start, zoom_end, pan) where
# pan is the horizontal drift as a fraction of the slack: -1 left, +1 right,
# 0 centred. Amounts are small on purpose - it should read as a slow drift
# under the narration, not as a camera move the viewer notices.
_MOVES = [
    (1.00, 1.15, 0.0),    # push in
    (1.15, 1.00, 0.0),    # pull out
    (1.05, 1.15, -1.0),   # drift left while pushing in
    (1.05, 1.15, +1.0),   # drift right while pushing in
]


def ken_burns(image: Path, seconds: float, out: Path, k: int = 0) -> Path:
    """Animate a still into `seconds` of 1080x1920 30 fps video.

    zoompan works on the upscaled source (2x) because it moves the crop
    window in whole pixels: at output size the 15% drift over 300+ frames
    would step visibly. Video only - the narration is muxed in by the render.
    """
    frames = max(2, int(round(seconds * OUT_FPS)))
    z0, z1, pan = _MOVES[k % len(_MOVES)]
    # `on` is the output frame index within this (single) input frame.
    t = f"(on/{frames - 1})"
    z = f"({z0}+({z1}-{z0})*{t})"
    # centre the crop, then offset horizontally by `pan` x the available slack
    x = f"(iw-iw/zoom)/2+({pan})*(iw-iw/zoom)/2*{t}" if pan else "(iw-iw/zoom)/2"
    y = "(ih-ih/zoom)/2"
    vf = (
        f"scale={OUT_W * 2}:{OUT_H * 2}:flags=lanczos,"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={OUT_W}x{OUT_H}:fps={OUT_FPS},"
        "format=yuv420p"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-i", str(image),
         "-vf", vf,
         "-t", f"{seconds:.3f}",
         "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20",
         str(out)],
        check=True,
    )
    return out


def fit_to_duration(clip: Path, seconds: float, out: Path) -> Path:
    """Stretch a ~3.4 s clip to exactly `seconds` of 30 fps video.

    forward+reverse gives a seamless ~7 s loop; that loop is repeated as many
    times as gets closest to the target and the remainder is absorbed by a
    mild setpts stretch (0.75x-1.5x), which on slow atmospheric footage is
    invisible. Video only - the narration is muxed in by the render.
    """
    loops = max(1, round(seconds / PINGPONG_S))
    factor = seconds / (PINGPONG_S * loops)
    vf = (
        "[0:v]split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1:a=0[pp];"
        f"[pp]loop=loop={loops - 1}:size={LENGTH * 2}:start=0,"
        f"setpts={factor:.6f}*PTS,fps={OUT_FPS}[v]"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-i", str(clip),
         "-filter_complex", vf, "-map", "[v]",
         "-t", f"{seconds:.3f}",
         "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20", "-pix_fmt", "yuv420p",
         str(out)],
        check=True,
    )
    return out


def concat(parts: Sequence[Path], out: Path) -> Path:
    """Join the fitted clips in order (same codec/size/fps, so stream copy)."""
    listing = out.with_suffix(".txt")
    # concat demuxer wants forward slashes and single-quoted paths
    listing.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(out)],
        check=True,
    )
    listing.unlink(missing_ok=True)
    return out


def last_frame(clip: Path, out: Path) -> Path:
    """Extract the final frame of a clip as a PNG (the next shot starts on it)."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-sseof", "-0.05", "-i", str(clip),
         "-update", "1", "-frames:v", "1", str(out)],
        check=True,
    )
    if not out.exists() or out.stat().st_size == 0:
        raise ClipError(f"could not extract the last frame of {clip.name}")
    return out


def shots_for(seconds: float) -> int:
    """How many SHOT_S shots cover `seconds` (capped; the fit stretches the rest)."""
    return max(1, min(MAX_SHOTS_PER_BEAT, -(-int(seconds * 1000) // int(SHOT_S * 1000))))


def fit_chain(shots: Sequence[Path], seconds: float, out: Path) -> Path:
    """Join a beat's shots in order and cut to exactly `seconds` at 30 fps.

    Shots normally over-cover the beat (n x 5 s >= beat), so the tail is
    trimmed. If the chain hit MAX_SHOTS_PER_BEAT and came up short, the whole
    beat is slowed by the small factor needed (<= 1.25x, imperceptible on slow
    footage) rather than looped. Video only - the narration is muxed in by
    the render.
    """
    total = SHOT_S * len(shots)
    factor = max(1.0, seconds / total)
    if factor > 1.25:
        raise ClipError(f"{len(shots)} shots ({total:.1f}s) cannot cover a "
                        f"{seconds:.1f}s beat; raise MAX_SHOTS_PER_BEAT")
    inputs = []
    for s in shots:
        inputs += ["-i", str(s)]
    chain = "".join(f"[{i}:v]" for i in range(len(shots)))
    vf = (f"{chain}concat=n={len(shots)}:v=1:a=0,"
          f"setpts={factor:.6f}*PTS,fps={OUT_FPS},format=yuv420p[v]")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", *inputs,
         "-filter_complex", vf, "-map", "[v]",
         "-t", f"{seconds:.3f}",
         "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20",
         str(out)],
        check=True,
    )
    return out


def _generate_with_retry(provider: ComfyProvider, prompt: str, negative: str,
                         out: Path, beat, start_image: Optional[Path] = None) -> None:
    for attempt in range(1, CLIP_ATTEMPTS + 1):
        t0 = time.perf_counter()
        try:
            provider.generate(prompt, negative, out, start_image=start_image)
            break
        except ClipError as e:
            if attempt == CLIP_ATTEMPTS:
                raise
            print(f"[!] {out.name} attempt {attempt}/{CLIP_ATTEMPTS} failed "
                  f"({e}); retrying with a new seed", flush=True)
    print(f"    {out.name}: {time.perf_counter() - t0:.0f}s  "
          f"[{beat.duration:.1f}s beat]  {prompt[-80:]}", flush=True)


def _have(p: Path) -> bool:
    return p.exists() and p.stat().st_size > 0


def build_film(story_dir: Path, beats, prompts: List[str], negative: str) -> List[Path]:
    """Still per beat, then a chain of I2V shots per beat; returns fitted beats.

    Every still is generated before any shot so ComfyUI loads Flux once and
    Wan once - on an 8 GB card the two cannot stay resident together and
    alternating per beat would reload ~10 GB of weights each time.
    """
    import visualPrompts

    stills: List[Path] = []
    flux = None
    for k, (beat, prompt) in enumerate(zip(beats, prompts)):
        still = story_dir / f"beat{k}_still.png"
        if _have(still):
            print(f"    reusing {still.name}", flush=True)
        else:
            flux = flux or image_provider(FILM_W, FILM_H)
            _generate_with_retry(flux, prompt, negative, still, beat)
        stills.append(still)

    wan = None
    chains: List[List[Path]] = []
    for k, (beat, prompt, still) in enumerate(zip(beats, prompts, stills)):
        scene = prompt[len(visualPrompts.STYLE_IMAGE):] if prompt.startswith(
            visualPrompts.STYLE_IMAGE) else prompt
        shots: List[Path] = []
        start = still
        for j in range(shots_for(beat.duration)):
            shot = story_dir / f"beat{k}_shot{j}.mp4"
            if _have(shot):
                print(f"    reusing {shot.name}", flush=True)
            else:
                wan = wan or i2v_provider()
                _generate_with_retry(wan, visualPrompts.motion_prompt(scene, k, j),
                                     visualPrompts.NEGATIVE_I2V, shot, beat, start)
            shots.append(shot)
            start = last_frame(shot, story_dir / f"beat{k}_shot{j}_last.png")
        chains.append(shots)

    # Encoding is cheap; do it after generation so NVENC never competes with
    # the diffusion model for VRAM, and the caller can release the GPU sooner.
    free_gpu()

    fitted = []
    for k, (beat, shots) in enumerate(zip(beats, chains)):
        seconds = beat.duration + (1.0 if k == len(chains) - 1 else 0.0)
        fitted.append(fit_chain(shots, seconds, story_dir / f"fit{k}.mp4"))
    return fitted


def build_background(story_number: int, beats, prompts: List[str], negative: str,
                     style: str = "image") -> Path:
    """Generate one shot per beat, fit each to its beat, join them.

    Returns the background mp4. Raises ClipError (or CalledProcessError from
    ffmpeg) on any failure so the caller can fall back to the stock footage.
    Shots already generated for this story are reused, so a retry after a
    crash only pays for what is missing.
    """
    if style not in ("image", "video", "film"):
        raise ClipError(f"unknown background style {style!r}")
    if not is_available():
        raise ClipError(f"ComfyUI not reachable at {paths.comfy_url()}")

    story_dir = paths.CLIPS / str(story_number)
    story_dir.mkdir(parents=True, exist_ok=True)

    if style == "film":
        fitted = build_film(story_dir, beats, prompts, negative)
    else:
        provider = image_provider() if style == "image" else video_provider()
        ext = provider.ext

        raw: List[Path] = []
        for k, (beat, prompt) in enumerate(zip(beats, prompts)):
            shot = story_dir / f"beat{k}{ext}"
            if _have(shot):
                print(f"    reusing {shot.name}", flush=True)
            else:
                _generate_with_retry(provider, prompt, negative, shot, beat)
            raw.append(shot)

        # Encoding is cheap; do it after generation so NVENC never competes with
        # the diffusion model for VRAM, and the caller can release the GPU sooner.
        free_gpu()

        fitted = []
        for k, (beat, shot) in enumerate(zip(beats, raw)):
            # The last beat runs a second long so rounding never leaves the video
            # shorter than the narration (the render trims to the audio anyway).
            seconds = beat.duration + (1.0 if k == len(raw) - 1 else 0.0)
            out = story_dir / f"fit{k}.mp4"
            if style == "image":
                fitted.append(ken_burns(shot, seconds, out, k))
            else:
                fitted.append(fit_to_duration(shot, seconds, out))

    background = concat(fitted, story_dir / "background.mp4")
    for f in fitted:
        f.unlink(missing_ok=True)
    return background


if __name__ == "__main__":
    # Benchmark: one shot through the exact workflow the pipeline uses.
    #   python pipeline/clips.py "a dark forest trail at night, fog"          (image)
    #   python pipeline/clips.py --video "a dark forest trail at night, fog"  (Wan clip)
    #   python pipeline/clips.py --film "a dark forest trail at night, fog"   (still + I2V shot)
    # Run it with LM Studio's model unloaded (or the pipeline stopped) so the
    # timing reflects the VRAM the pipeline will actually have.
    import sys

    import visualPrompts

    argv = sys.argv[1:]
    style = "video" if "--video" in argv else "film" if "--film" in argv else "image"
    scene = " ".join(a for a in argv if not a.startswith("--")) or "a dark forest trail at night, fog"
    if not is_available():
        sys.exit(f"ComfyUI not reachable at {paths.comfy_url()}")

    if style == "film":
        bench = paths.CLIPS / "_bench"
        stamp = int(time.time())
        still = bench / f"bench_{stamp}_still.png"
        shot = bench / f"bench_{stamp}_shot0.mp4"
        print(f"film: still {FILM_W}x{FILM_H} -> shot {SHOT_FRAMES} frames, "
              f"{FILM_STEPS} steps, cfg {FILM_CFG}, lora={LORA or '-'}, "
              f"unet={UNET_OVERRIDE or 'workflow default'}, TE on {TE_DEVICE}")
        t0 = time.perf_counter()
        image_provider(FILM_W, FILM_H).generate(
            visualPrompts.STYLE_IMAGE + scene, visualPrompts.NEGATIVE, still)
        t_still = time.perf_counter() - t0
        print(f"still took {t_still:.0f}s -> {still}")
        t0 = time.perf_counter()
        i2v_provider().generate(visualPrompts.motion_prompt(scene, 0, 0),
                                visualPrompts.NEGATIVE_I2V, shot, start_image=still)
        t_shot = time.perf_counter() - t0
        free_gpu()
        print(f"shot took {t_shot:.0f}s ({t_shot / 60:.1f} min) -> {shot}")
        est = 5 * (t_still + 3 * t_shot) / 60
        print(f"a 5-beat story (3 shots per beat) needs ~{est:.0f} min")
        sys.exit(0)

    if style == "image":
        provider = image_provider()
        out = paths.CLIPS / "_bench" / f"bench_{int(time.time())}.png"
        print(f"image {IMG_W}x{IMG_H}, {IMG_STEPS} steps -> {out}")
    else:
        provider = video_provider()
        out = paths.CLIPS / "_bench" / f"bench_{int(time.time())}.mp4"
        print(f"video {WIDTH}x{HEIGHT}, {LENGTH} frames, {STEPS} steps -> {out}")

    t0 = time.perf_counter()
    provider.generate(visualPrompts.style_prefix(style) + scene, visualPrompts.NEGATIVE, out)
    took = time.perf_counter() - t0
    free_gpu()
    print(f"{style} took {took:.0f}s ({took / 60:.1f} min); a 4-beat story needs ~{4 * took / 60:.1f} min")
