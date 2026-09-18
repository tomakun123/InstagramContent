"""Step 1 deliverable: replace the plan's ~50 s/clip estimate with a measurement.

Every throughput and cost number in the plan hangs off seconds-per-clip. Until this has
run against a real 5090, they are all guesses.

    python -m engine.benchmark --workflow ltx-i2v.api.json \
                               --keyframe samples/keyframe.png \
                               --prompt "the figure slowly turns its head toward the camera"
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .comfy_client import ComfyClient, ComfyError
from .config import REPO_ROOT, RENDER

# From the plan. Reproduced here so the script can judge its own result.
ESTIMATE_S_PER_CLIP = 50.0
KILL_CRITERION_S_PER_CLIP = 90.0
CLIPS_PER_DAY = 840  # 20 videos x 21 shots x 2 rerolls

RESOLUTIONS: list[tuple[str, int, int]] = [
    ("768x512", 768, 512),
    ("1280x704", 1280, 704),
]

NEGATIVE = "blurry, static, still image, frozen, text, watermark, distorted faces"


def _vram() -> dict[str, Any]:
    """VRAM headroom, straight from ComfyUI — it already reports what it sees."""
    try:
        import requests

        from .config import COMFY

        stats = requests.get(f"{COMFY.base_url}/system_stats", timeout=10).json()
        device = (stats.get("devices") or [{}])[0]
        return {
            "name": device.get("name"),
            "total_gb": round((device.get("vram_total") or 0) / 1e9, 1),
            "free_gb": round((device.get("vram_free") or 0) / 1e9, 1),
        }
    except Exception as exc:  # noqa: BLE001 - diagnostics must never fail the run
        return {"error": str(exc)}


def _measure(
    client: ComfyClient,
    base_graph: dict[str, Any],
    image_name: str,
    prompt: str,
    label: str,
    width: int,
    height: int,
    runs: int,
    out_dir: Path,
) -> dict[str, Any]:
    timings: list[float] = []
    failures: list[str] = []

    for index in range(runs):
        graph = client.parameterize(
            base_graph,
            positive_prompt=prompt,
            negative_prompt=NEGATIVE,
            image_name=image_name,
            # Vary the seed: repeating one seed measures cache behaviour, not generation.
            seed=1000 + index,
            width=width,
            height=height,
            frames=RENDER.frames,
        )
        destination = out_dir / f"{label}_{index:02d}.mp4"
        try:
            result = client.generate_clip(graph, destination)
            timings.append(result.seconds)
            print(f"  {label} run {index + 1}/{runs}: {result.seconds:6.1f}s -> {destination.name}")
        except ComfyError as exc:
            failures.append(str(exc)[:300])
            print(f"  {label} run {index + 1}/{runs}: FAILED — {str(exc)[:160]}")

    if not timings:
        return {"resolution": label, "runs": runs, "failures": failures, "usable": False}

    mean = statistics.mean(timings)
    return {
        "resolution": label,
        "width": width,
        "height": height,
        "frames": RENDER.frames,
        "runs": runs,
        "succeeded": len(timings),
        "failures": failures,
        "seconds_per_clip": {
            "mean": round(mean, 1),
            "median": round(statistics.median(timings), 1),
            "min": round(min(timings), 1),
            "max": round(max(timings), 1),
        },
        "projected_gpu_hours_per_day": round(mean * CLIPS_PER_DAY / 3600, 1),
        "fits_one_gpu": mean * CLIPS_PER_DAY / 3600 < 24,
        "usable": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path, required=True,
                        help="LTX i2v workflow exported via 'Save (API Format)'")
    parser.add_argument("--keyframe", type=Path, required=True)
    parser.add_argument("--prompt", required=True, help="motion prompt — the verb")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "benchmark-output")
    parser.add_argument("--results", type=Path, default=REPO_ROOT / "benchmark-results.json")
    args = parser.parse_args()

    client = ComfyClient()
    if not client.ping():
        print(
            f"ComfyUI unreachable at {client.config.base_url}\n"
            "Is the SSH tunnel up?  ssh -N -L 8188:localhost:8188 root@<pod-ip> -p <port>",
        )
        return 1

    base_graph = ComfyClient.load_workflow(args.workflow)
    image_name = client.upload_image(args.keyframe)
    args.out.mkdir(parents=True, exist_ok=True)

    print(f"benchmarking {args.runs} runs per resolution, {RENDER.frames} frames\n")
    results = [
        _measure(client, base_graph, image_name, args.prompt, label, w, h, args.runs, args.out)
        for label, w, h in RESOLUTIONS
    ]

    report = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "client_platform": platform.platform(),
        "gpu": _vram(),
        "workflow": args.workflow.name,
        "motion_prompt": args.prompt,
        "estimate_s_per_clip": ESTIMATE_S_PER_CLIP,
        "kill_criterion_s_per_clip": KILL_CRITERION_S_PER_CLIP,
        "clips_per_day_target": CLIPS_PER_DAY,
        "results": results,
    }
    args.results.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\nwrote {args.results}")
    print(f"gpu: {report['gpu']}")

    usable = [r for r in results if r.get("usable")]
    if not usable:
        print("\nNo successful generations. Fix the workflow before drawing conclusions.")
        return 1

    print("\n" + "=" * 62)
    for result in usable:
        mean = result["seconds_per_clip"]["mean"]
        hours = result["projected_gpu_hours_per_day"]
        verdict = "OK" if mean <= KILL_CRITERION_S_PER_CLIP else "OVER KILL CRITERION"
        fits = "fits one GPU" if result["fits_one_gpu"] else "DOES NOT FIT ONE GPU"
        print(f"{result['resolution']:>10}  {mean:6.1f} s/clip  "
              f"{hours:5.1f} GPU-hr/day  {fits}  [{verdict}]")
    print("=" * 62)

    best = min(r["seconds_per_clip"]["mean"] for r in usable)
    if best > KILL_CRITERION_S_PER_CLIP:
        print(
            f"\nSlowest acceptable was {KILL_CRITERION_S_PER_CLIP:.0f}s; best measured "
            f"{best:.1f}s. Per the plan, STOP and re-scope — fewer shots per video, "
            "shorter videos, or two GPUs — before building on this."
        )
        return 2

    print(f"\nBest {best:.1f} s/clip vs {ESTIMATE_S_PER_CLIP:.0f}s estimated. Proceed to Step 2.")
    print("Terminate the pod:  python -m engine.runpod_control terminate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
