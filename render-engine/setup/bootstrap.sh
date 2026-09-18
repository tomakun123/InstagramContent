#!/usr/bin/env bash
# One-time setup, run ON the RunPod pod.
#
# Installs ComfyUI-LTXVideo and moves every model directory onto the network volume,
# so weights survive pod deletion. Idempotent: safe to re-run on a fresh pod, which is
# the whole point — the second run should find everything already on the volume.
set -euo pipefail

VOLUME="${RUNPOD_VOLUME:-/runpod-volume}"
NODE_REPO="https://github.com/Lightricks/ComfyUI-LTXVideo.git"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[fail] %s\033[0m\n' "$*" >&2; exit 1; }

# --- locate ComfyUI ----------------------------------------------------------
COMFY=""
for candidate in /workspace/ComfyUI /ComfyUI "$VOLUME/ComfyUI" "$HOME/ComfyUI"; do
  [ -f "$candidate/main.py" ] && { COMFY="$candidate"; break; }
done
[ -n "$COMFY" ] || die "ComfyUI not found. Set COMFY= by hand and re-run."
say "ComfyUI at $COMFY"

[ -d "$VOLUME" ] || die "Network volume not mounted at $VOLUME.
  Weights would land on ephemeral pod disk and be lost on termination — which is the one
  thing this script exists to prevent. Attach the volume and re-run."

# --- move model dirs onto the volume ----------------------------------------
# ComfyUI writes into models/<kind>/. We replace each with a symlink to the volume so
# auto-downloads persist. Any weights already on pod disk are moved across first.
say "Relocating model directories onto $VOLUME"
MODEL_DIRS=(checkpoints text_encoders loras vae latent_upscale_models clip_vision upscale_models)

for kind in "${MODEL_DIRS[@]}"; do
  target="$VOLUME/models/$kind"
  link="$COMFY/models/$kind"
  mkdir -p "$target"

  if [ -L "$link" ]; then
    echo "  $kind -> already linked"
    continue
  fi

  if [ -d "$link" ]; then
    # Preserve anything the template shipped or a previous run left behind.
    if [ -n "$(ls -A "$link" 2>/dev/null)" ]; then
      echo "  $kind -> migrating existing files to volume"
      cp -rn "$link/." "$target/" 2>/dev/null || true
    fi
    rm -rf "$link"
  fi

  ln -s "$target" "$link"
  echo "  $kind -> $target"
done

# --- custom node -------------------------------------------------------------
NODE_DIR="$COMFY/custom_nodes/ComfyUI-LTXVideo"
if [ -d "$NODE_DIR/.git" ]; then
  say "Updating ComfyUI-LTXVideo"
  git -C "$NODE_DIR" pull --ff-only
else
  say "Installing ComfyUI-LTXVideo"
  git clone --depth 1 "$NODE_REPO" "$NODE_DIR"
fi

if [ -f "$NODE_DIR/requirements.txt" ]; then
  say "Installing node requirements"
  python -m pip install --no-cache-dir -r "$NODE_DIR/requirements.txt"
fi

# --- report ------------------------------------------------------------------
say "Example workflows shipped with the node"
# Filenames are the node repo's to choose, not ours to guess. Print what is actually
# there so the runbook's step 5 names a file that exists.
find "$NODE_DIR" -iname '*.json' -path '*workflow*' -printf '  %P\n' 2>/dev/null \
  | sort || warn "no workflow JSON found — check $NODE_DIR by hand"

say "Weights present on the volume"
du -sh "$VOLUME/models"/* 2>/dev/null || echo "  (none yet)"

cat <<'NOTE'

Next:
  1. Start ComfyUI and open one of the i2v workflows listed above.
  2. Required weights AUTO-DOWNLOAD on first use (~50-75 GB: the 22B checkpoint, the
     Gemma 3 12B text encoder, latent upscalers). Because models/ now points at the
     network volume, this happens once, not once per pod.
  3. Expect 30-60 min. You are paying for GPU time while it downloads.
  4. Re-run this script on the NEXT pod and confirm it reports weights already present.
     That is the test that persistence actually works.

NOTE
say "Bootstrap complete"
