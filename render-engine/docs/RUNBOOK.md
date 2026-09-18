# Step 1 Runbook — stand up the render server

Steps marked **[you]** need a human: account signup, payment, and clicking through the
ComfyUI UI. Steps marked **[code]** are scripted in this directory.

**Budget:** ~$3-6 of GPU time. **Time:** half a day, most of it waiting on downloads.

---

## Corrections to the original plan

Found while reading the actual LTX-2.3 docs. All four change the setup:

1. **LTX-2.3 needs 32 GB+ VRAM.** The RTX 5090 has exactly 32 GB — it is the *minimum
   viable* card, not merely the preferred one. A 4090 (24 GB) only works through the
   `low_vram_loaders` / FP8 path, at a quality and speed cost.
2. **Weights are ~50-75 GB, not ~30 GB.** The 22B checkpoint is only part of it; the text
   encoder is a full **Gemma 3 12B**, plus latent upscalers and LoRAs. Provision a
   **150 GB** network volume (~$10/mo storage, not the $3.50 estimated).
3. **The RTX 3050 cannot run LTX-2.3 at all**, even quantized, even for prototyping. The
   earlier suggestion to iterate locally in WSL2 does not apply to this model. Prompt
   iteration happens against the rented pod; budget for it.
4. **RunPod REST API v1 shuts down 15 Nov 2026.** Pod CRUD still targets v1 and no public
   migration timeline exists yet, so this code uses the **official `runpod` Python SDK**
   rather than raw v1 endpoints. When RunPod moves pod CRUD to v2, that is a version bump
   here instead of a rewrite.

---

## 1. Account and key **[you]**

1. Sign up at runpod.io, add **$20** credit. This step spends ~$3-6 of it.
2. Settings → API Keys → create one with read/write.
3. On the Windows box, copy `.env.example` to `.env` and set `RUNPOD_API_KEY`.
   `.env` is gitignored. Never commit it, and never put it on the pod.

## 2. Network volume **[you]**

Storage → Network Volumes → New.

- **Size: 150 GB** (see correction 2)
- **Region: one that actually stocks RTX 5090s.** Check GPU availability in the region
  *before* creating the volume — a volume pins you to its datacenter, and if that region
  has no 5090s you will have to recreate it elsewhere and re-download everything.

Record the volume ID into `.env` as `RUNPOD_NETWORK_VOLUME_ID`.

## 3. First pod **[you]**

Deploy a Pod from the **ComfyUI** template:

- GPU: **RTX 5090** (32 GB)
- Attach the network volume from step 2 (mounts at `/runpod-volume`)
- Leave HTTP port 8188 exposed for the *first* session only, so you can reach the UI while
  setting up. Switch to the SSH tunnel in step 6.

## 4. Install nodes and weights **[code, runs on the pod]**

SSH in, then:

```bash
curl -fsSL https://raw.githubusercontent.com/<you>/horror-render-engine/main/setup/bootstrap.sh | bash
```

Or paste `setup/bootstrap.sh` over. It installs `ComfyUI-LTXVideo` and fetches the
checkpoint, the Gemma 3 text encoder and the upscalers **onto `/runpod-volume`** so they
survive pod deletion.

> Expect 30-60 min of downloading. You are paying for GPU time while it runs — this is the
> cost the network volume exists to make one-time.

## 5. One clip, by hand **[you]**

In the ComfyUI UI, open the shipped example workflow
`ltx-2.3-i2v-distilled` (exact filename printed by `bootstrap.sh`), point it at a keyframe,
write a motion prompt, and run it.

**Do not automate anything until a clip comes out of the UI.** Debugging a broken workflow
through an API client is far worse than debugging it in the graph editor.

## 6. Lock the door **[you]**

ComfyUI ships with **no authentication**. RunPod's proxy URL
(`https://{pod_id}-8188.proxy.runpod.net`) is unguessable but not authenticated — anyone
with the URL has your GPU and everything on the volume.

Stop exposing 8188. Tunnel instead, from PowerShell:

```powershell
ssh -N -L 8188:localhost:8188 root@<pod-ip> -p <ssh-port> -i $env:USERPROFILE\.ssh\id_ed25519
```

`http://localhost:8188` now reaches the pod, and nothing is public.

## 7. Measure **[code]**

From the Windows box, with the tunnel up:

```powershell
python -m engine.benchmark --keyframe .\samples\keyframe.png `
                           --prompt "the figure slowly turns its head toward the camera" `
                           --runs 5
```

Writes `benchmark-results.json`: seconds/clip at 768x512 and 1280x704, VRAM headroom,
whether generated audio is usable.

## 8. Terminate **[code]**

```powershell
python -m engine.runpod_control terminate
```

A pod bills while it runs whether or not you are using it. One left on over a weekend is
~$60. Also set an idle timeout in the RunPod UI as a backstop — belt and braces.

---

## Done when

- [ ] One LTX-2.3 clip generated from a keyframe, viewable, from a real `HorrorStories/` story
- [ ] A real seconds-per-clip number in `benchmark-results.json`
- [ ] A **second** pod launch reaches ComfyUI without re-downloading weights
- [ ] SSH tunnel works; port 8188 is no longer publicly exposed
- [ ] Pod terminates cleanly and billing stops

**Kill criterion:** worse than ~90 s/clip means 840 clips/day will not fit on one GPU.
Stop and re-scope — fewer shots per video, shorter videos, or two GPUs — before building
anything on top.
