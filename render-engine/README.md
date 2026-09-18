# horror-render-engine

Generates per-shot animated video for the horror pipeline. Takes a **shot list** in,
returns **numbered MP4 clips** out. Nothing else crosses the boundary.

This directory is a **staging area for a separate repository**. It is deliberately
self-contained — its own `requirements.txt`, no imports from `pipeline/`, no access to
publishing credentials — so it can be lifted out with `git subtree split` once it works.

## Why it is separate from InstagramContent

|                | `InstagramContent`                        | `horror-render-engine`          |
|----------------|-------------------------------------------|----------------------------------|
| Runs on        | the Windows PC (unchanged)                | a rented GPU, as a container     |
| Runtime        | PowerShell, n8n, LM Studio, ffmpeg        | CUDA, PyTorch, ComfyUI           |
| Dependencies   | 3 packages                                | torch + ~50-75 GB of weights     |
| Secrets        | IG / TikTok / YouTube tokens              | **none — must never see them**   |

A rented pod is hardware you do not control. It has no business holding publishing tokens.

## The contract

```
InstagramContent                         horror-render-engine
─────────────────                        ────────────────────
shotlist/HorrorStory{n}.json   ────────▶ generate N clips
clips/HorrorStory{n}/*.mp4     ◀──────── numbered clips + manifest.json
        │
generateContent.py concats, mixes audio, burns subtitles, NVENC-encodes (unchanged)
```

Schema: `schema/shotlist.schema.json`. Keep a copy in both repos and version it.

## Status

**Step 1 — stand up the render server.** See `docs/RUNBOOK.md`.
Nothing here has run against a real GPU yet; every performance number in the plan is an
estimate until `engine/benchmark.py` replaces it with a measurement.

## Tests

Stdlib only, no GPU, no network, nothing provisioned:

```bash
python -m unittest discover -s tests -t .
```

18 tests covering the ComfyUI round trip against a stub server and the pod-termination
guarantees. They catch the failures that otherwise only surface once a pod is billing.

## Layout

```
docs/RUNBOOK.md            manual steps (account, volume, pod) — you, not the agent
setup/bootstrap.sh         one-time install ON the pod: nodes + weights onto the volume
engine/config.py           settings, all overridable by env
engine/runpod_control.py   pod lifecycle, with guaranteed termination
engine/comfy_client.py     ComfyUI HTTP API: upload, submit, poll, download
engine/benchmark.py        Step 1 measurement — the real seconds-per-clip number
schema/shotlist.schema.json
```
