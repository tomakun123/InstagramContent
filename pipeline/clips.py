"""Background generation and assembly: one shot per story beat.

Two styles, both driven through ComfyUI's HTTP API the same way n8n drives LM
Studio (queue a workflow, poll for the result, fetch the file - nothing here
imports torch; the models run in ComfyUI's own process):

  image (default)  Flux.1-schnell still per beat, then an ffmpeg Ken Burns
                   move (slow push-in / pull-out / pan) for the beat's whole
                   duration. Sharp at 1080x1920 and ~1 min per beat.
  video            Wan 2.2 5B clip per beat (~3.4 s at 480x832, the geometry
                   the RTX 3050 can do in ~5 min), ping-ponged and stretched to
                   the beat. Real motion, but soft after the 2.25x upscale.

See assets/comfy/*.json and docs/SETUP.md section 10.
"""
import json
import os
import random
import subprocess
import time
import urllib.error
import urllib.request
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
STEPS = 20
CFG = 5.0
# Override the checkpoint file without editing the workflow.
UNET_OVERRIDE = os.environ.get("COMFY_UNET", "").strip()

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
                 patch: Dict[str, dict]):
        self.template = json.loads(workflow_path.read_text(encoding="utf-8"))
        self.nodes = nodes
        self.ext = ext
        self.patch = patch      # role -> inputs to set on that node every run
        for role, node in nodes.items():
            if node not in self.template:
                raise ClipError(f"workflow {workflow_path.name} has no node {node} "
                                f"({role}); update the node ids in clips.py")

    def _build(self, prompt: str, negative: str, seed: int, prefix: str) -> dict:
        wf = json.loads(json.dumps(self.template))   # deep copy
        n = self.nodes
        wf[n["positive"]]["inputs"]["text"] = prompt
        wf[n["negative"]]["inputs"]["text"] = negative
        wf[n["sampler"]]["inputs"]["seed"] = seed
        wf[n["save"]]["inputs"]["filename_prefix"] = prefix
        for role, inputs in self.patch.items():
            wf[n[role]]["inputs"].update(inputs)
        return wf

    def generate(self, prompt: str, negative: str, out_path: Path,
                 seed: Optional[int] = None) -> Path:
        """Queue one generation, wait for it, and copy the output to out_path."""
        seed = seed if seed is not None else random.randrange(2**32)
        prefix = f"horror/{out_path.parent.name}_{out_path.stem}"

        try:
            resp = json.loads(_http("POST", "/prompt", {
                "prompt": self._build(prompt, negative, seed, prefix),
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


def image_provider() -> ComfyProvider:
    """Flux.1-schnell text-to-image (assets/comfy/flux_schnell_t2i_api.json)."""
    return ComfyProvider(
        paths.COMFY_IMAGE_WORKFLOW,
        nodes={"positive": "6", "negative": "7", "sampler": "3", "save": "9", "latent": "5"},
        ext=".png",
        patch={"latent": {"width": IMG_W, "height": IMG_H},
               "sampler": {"steps": IMG_STEPS, "cfg": IMG_CFG}},
    )


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


def _generate_with_retry(provider: ComfyProvider, prompt: str, negative: str,
                         out: Path, beat) -> None:
    for attempt in range(1, CLIP_ATTEMPTS + 1):
        t0 = time.perf_counter()
        try:
            provider.generate(prompt, negative, out)
            break
        except ClipError as e:
            if attempt == CLIP_ATTEMPTS:
                raise
            print(f"[!] {out.name} attempt {attempt}/{CLIP_ATTEMPTS} failed "
                  f"({e}); retrying with a new seed", flush=True)
    print(f"    {out.name}: {time.perf_counter() - t0:.0f}s  "
          f"[{beat.duration:.1f}s beat]  {prompt[-80:]}", flush=True)


def build_background(story_number: int, beats, prompts: List[str], negative: str,
                     style: str = "image") -> Path:
    """Generate one shot per beat, fit each to its beat, join them.

    Returns the background mp4. Raises ClipError (or CalledProcessError from
    ffmpeg) on any failure so the caller can fall back to the stock footage.
    Shots already generated for this story are reused, so a retry after a
    crash only pays for what is missing.
    """
    if style not in ("image", "video"):
        raise ClipError(f"unknown background style {style!r}")
    if not is_available():
        raise ClipError(f"ComfyUI not reachable at {paths.comfy_url()}")

    story_dir = paths.CLIPS / str(story_number)
    story_dir.mkdir(parents=True, exist_ok=True)
    provider = image_provider() if style == "image" else video_provider()
    ext = provider.ext

    raw: List[Path] = []
    for k, (beat, prompt) in enumerate(zip(beats, prompts)):
        shot = story_dir / f"beat{k}{ext}"
        if shot.exists() and shot.stat().st_size > 0:
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
    # Run it with LM Studio's model unloaded (or the pipeline stopped) so the
    # timing reflects the VRAM the pipeline will actually have.
    import sys

    import visualPrompts

    argv = sys.argv[1:]
    style = "video" if "--video" in argv else "image"
    scene = " ".join(a for a in argv if a != "--video") or "a dark forest trail at night, fog"
    if not is_available():
        sys.exit(f"ComfyUI not reachable at {paths.comfy_url()}")

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
