#!/usr/bin/env bash
# Download Qwen-Image-2.1 (Comfy-Org native ComfyUI format) with resume + retry.
set -u
BASE="https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main"
DEST="/home/user/humanai/comfyui/models"

FILES=(
  "diffusion_models/qwen_image_2.1_int8_convrot.safetensors"
  "text_encoders/qwen3vl_8b_int8_convrot.safetensors"
  "text_encoders/qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors"
  "vae/qwen_image_2.1_vae_bf16.safetensors"
)

for f in "${FILES[@]}"; do
  out="$DEST/$f"
  mkdir -p "$(dirname "$out")"
  echo "=== [$(date +%H:%M:%S)] downloading $f -> $out"
  for attempt in 1 2 3 4 5; do
    if curl -sL --fail --retry 3 --retry-delay 5 -C - -o "$out" "$BASE/$f"; then
      echo "=== [$(date +%H:%M:%S)] OK: $f ($(du -h "$out" | cut -f1))"
      break
    fi
    echo "=== [$(date +%H:%M:%S)] attempt $attempt failed for $f, retrying in 10s"
    sleep 10
    if [ "$attempt" -eq 5 ]; then echo "FAILED: $f" >&2; exit 1; fi
  done
done
echo "=== ALL MODELS DOWNLOADED [$(date +%H:%M:%S)]"
