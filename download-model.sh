#!/usr/bin/env bash
#
# download-model.sh — fetch a stack model from Hugging Face by id or list number.
#
# The stack's models live outside the repo (see .env: MODELS_DIR for GGUF LLMs,
# COMFYUI_MODELS_DIR for ComfyUI). This script knows where each one comes from
# and where it must land, and downloads it (resumable) with your HF token.
#
# Usage:
#   ./download-model.sh list                 # show the catalog (id, #, where)
#   ./download-model.sh <id|number> [TOKEN]  # download one entry
#   ./download-model.sh <id|number> -t TOKEN # token via flag instead
#   ./download-model.sh llm      [TOKEN]     # all LLM/GGUF entries
#   ./download-model.sh comfyui  [TOKEN]     # all ComfyUI entries
#   ./download-model.sh all      [TOKEN]     # everything
#
# The token is optional for public models but required for gated/private ones.
# It may be given as the 2nd argument, via -t/--token, or the HF_TOKEN env var.
#
# Options:
#   -t, --token TOKEN   Hugging Face token (gated/private repos)
#   --resume            continue an interrupted partial download (default: off;
#                       a size-mismatched file is re-downloaded fresh so two
#                       different files are never spliced together)
#   --no-verify         skip the post-download sha256 check (default: verify)
#
# Downloads are checksum-verified against HF's published sha256 when available;
# a file already present with a matching checksum is skipped.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- model catalog -------------------------------------------------------- #
# id | hf_repo | source_path_in_repo | kind(llm|comfy) | dest_relative_path | label
# kind=llm  -> under $MODELS_DIR        kind=comfy -> under $COMFYUI_MODELS_DIR
read -r -d '' CATALOG <<'EOF' || true
llm-heretic|0bserverx/Qwen3.8-27B-Heretic-Abliterated-Uncensored-GGUF|RVN-Q6_K-multilingual-mtp.gguf|llm|RVN-Q6_K-multilingual-mtp.gguf|Qwen3.8-27B Heretic Q6_K +MTP (chat model)
llm-heretic-vision|0bserverx/Qwen3.8-27B-Heretic-Abliterated-Uncensored-GGUF|RVN-Q5_K_M-multilingual-vision.gguf|llm|RVN-Q5_K_M-multilingual-vision.gguf|Qwen3.8-27B Heretic Q5_K_M (vision)
llm-uncensored|orcarouter/Qwen3.8-27B-Uncensored-GGUF|Qwen3.8-27B-Uncensored-Q6_K.gguf|llm|qwen3.8-27b/Qwen3.8-27B-Uncensored-Q6_K.gguf|Qwen3.8-27B Uncensored Q6_K
llm-coder-fim|Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF|qwen2.5-coder-1.5b-instruct-q8_0.gguf|llm|qwen2.5-coder-1.5b/qwen2.5-coder-1.5b-q8_0.gguf|Qwen2.5-Coder 1.5B Q8_0 (FIM)
comfy-dit|Comfy-Org/Qwen-Image-2.1|diffusion_models/qwen_image_2.1_int8_convrot.safetensors|comfy|diffusion_models/qwen_image_2.1_int8_convrot.safetensors|ComfyUI Qwen-Image-2.1 DiT (int8)
comfy-te-vl|Comfy-Org/Qwen-Image-2.1|text_encoders/qwen3vl_8b_int8_convrot.safetensors|comfy|text_encoders/qwen3vl_8b_int8_convrot.safetensors|ComfyUI text encoder qwen3vl-8b (int8)
comfy-te-t2i|Comfy-Org/Qwen-Image-2.1|text_encoders/qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors|comfy|text_encoders/qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors|ComfyUI text encoder 9b (t2i, int8)
comfy-vae|Comfy-Org/Qwen-Image-2.1|vae/qwen_image_2.1_vae_bf16.safetensors|comfy|vae/qwen_image_2.1_vae_bf16.safetensors|ComfyUI Qwen-Image-2.1 VAE (bf16)
EOF

# ---- resolve destination base dirs (from .env, with sensible fallbacks) --- #
if [ -f "$REPO_DIR/.env" ]; then
  # shellcheck disable=SC1091
  set -a; . "$REPO_DIR/.env"; set +a
fi
MODELS_DIR="${MODELS_DIR:-/srv/models}"
COMFYUI_MODELS_DIR="${COMFYUI_MODELS_DIR:-/home/user/humanai/comfyui/models}"

HF="https://huggingface.co"

# ---- helpers -------------------------------------------------------------- #
rows() { printf '%s\n' "$CATALOG" | grep -v '^[[:space:]]*$'; }

dest_base() { [ "$1" = "llm" ] && echo "$MODELS_DIR" || echo "$COMFYUI_MODELS_DIR"; }

human() { # bytes -> human
  local b=${1:-0}
  awk -v b="$b" 'BEGIN{ s="B K M G T"; split(s,u," "); i=1; while(b>=1024 && i<5){b/=1024;i++} printf (i==1?"%d%s":"%.1f%s"), b, u[i] }'
}

do_list() {
  printf "%-3s %-20s %-10s %s\n" "#" "ID" "ON-DISK" "MODEL  (repo)"
  printf '%s\n' "--------------------------------------------------------------------------------"
  local n=0 id repo src kind rel label base out mark size
  while IFS='|' read -r id repo src kind rel label; do
    [ -z "$id" ] && continue
    n=$((n+1))
    base="$(dest_base "$kind")"; out="$base/$rel"
    if [ -f "$out" ]; then size="$(human "$(stat -c%s "$out" 2>/dev/null || echo 0)")"; mark="✓ $size"; else mark="-"; fi
    printf "%-3s %-20s %-10s %s\n" "$n" "$id" "$mark" "$label"
    printf "    └ %s :: %s\n" "$repo" "$src"
  done < <(rows)
  echo
  echo "LLM/GGUF  -> $MODELS_DIR"
  echo "ComfyUI   -> $COMFYUI_MODELS_DIR"
  echo "Groups: 'llm', 'comfyui', 'all'"
}

# Return the catalog line for an id or 1-based number (empty if none).
resolve() {
  local key="$1" n=0 line id
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    n=$((n+1))
    id="${line%%|*}"
    if [ "$key" = "$id" ] || [ "$key" = "$n" ]; then printf '%s\n' "$line"; return 0; fi
  done < <(rows)
  return 1
}

# Expected "size sha256" for a repo file, from HF's tree API (LFS oid = sha256).
remote_meta() { # repo, src, auth...
  local repo="$1" src="$2"; shift 2
  curl -sL "$@" "$HF/api/models/$repo/tree/main?recursive=true" 2>/dev/null \
    | SRC="$src" python3 -c '
import sys, json, os
src = os.environ["SRC"]
try: data = json.load(sys.stdin)
except Exception: sys.exit(0)
for f in data:
    if f.get("path") == src:
        lfs = f.get("lfs") or {}
        print(lfs.get("size", f.get("size", "")), lfs.get("oid", ""))
        break
' 2>/dev/null
}

download_one() {
  local line="$1" token="$2"
  local id repo src kind rel label base out url
  IFS='|' read -r id repo src kind rel label <<<"$line"
  base="$(dest_base "$kind")"; out="$base/$rel"; url="$HF/$repo/resolve/main/$src"
  echo ">> [$id] $label"
  echo "   from: $repo :: $src"
  echo "   to:   $out"
  mkdir -p "$(dirname "$out")"
  local auth=(); [ -n "$token" ] && auth=(-H "Authorization: Bearer $token")

  local rsize rsha lsize
  read -r rsize rsha < <(remote_meta "$repo" "$src" "${auth[@]}"); rsize="${rsize:-}"; rsha="${rsha:-}"

  if [ -f "$out" ]; then
    lsize="$(stat -c%s "$out" 2>/dev/null || echo 0)"
    if [ -n "$rsha" ]; then
      echo "   verifying existing file..."
      if [ "$(sha256sum "$out" | cut -d' ' -f1)" = "$rsha" ]; then
        echo "   already complete + checksum OK ($(human "$lsize")) — skipping."; return 0
      fi
      echo "   existing file does NOT match checksum."
    elif [ -n "$rsize" ] && [ "$lsize" = "$rsize" ]; then
      echo "   already complete ($(human "$lsize")) — skipping (no checksum available)."; return 0
    fi
    # Mismatch. Only resume if explicitly asked AND sizes suggest a true partial;
    # otherwise re-download fresh so we never splice two different files together.
    if [ "$RESUME" = "1" ] && [ -n "$rsize" ] && [ "$lsize" -lt "$rsize" ]; then
      echo "   --resume: continuing partial at $(human "$lsize") / $(human "$rsize")"
    else
      echo "   re-downloading fresh (use --resume to continue an interrupted download)"
      rm -f "$out"
    fi
  fi

  local attempt
  for attempt in 1 2 3 4 5; do
    if curl -# -L --fail --retry 3 --retry-delay 5 -C - "${auth[@]}" -o "$out" "$url"; then
      lsize="$(stat -c%s "$out" 2>/dev/null || echo 0)"
      if [ "$VERIFY" = "1" ] && [ -n "$rsha" ]; then
        echo "   verifying checksum..."
        if [ "$(sha256sum "$out" | cut -d' ' -f1)" != "$rsha" ]; then
          echo "   FAILED: checksum mismatch after download for $id" >&2; return 1
        fi
        echo "   OK: $(human "$lsize") (sha256 verified)"
      elif [ -n "$rsize" ] && [ "$lsize" != "$rsize" ]; then
        echo "   WARNING: size mismatch (got $(human "$lsize"), expected $(human "$rsize"))" >&2
      else
        echo "   OK: $(human "$lsize")"
      fi
      return 0
    fi
    echo "   attempt $attempt failed; retrying in 10s..." >&2
    sleep 10
  done
  echo "   FAILED: $id (check the token for gated repos, or network)" >&2
  return 1
}

# ---- arg parsing ---------------------------------------------------------- #
TOKEN="${HF_TOKEN:-}"
RESUME=0
VERIFY=1
POSITIONAL=()
while [ $# -gt 0 ]; do
  case "$1" in
    -t|--token) TOKEN="${2:-}"; shift 2 ;;
    --resume) RESUME=1; shift ;;
    --no-verify) VERIFY=0; shift ;;
    -l|--list|list) POSITIONAL+=("list"); shift ;;
    -h|--help)
      sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
set -- "${POSITIONAL[@]:-}"

[ $# -eq 0 ] && { do_list; exit 0; }

SEL="$1"; shift || true
# A bare 2nd positional is treated as the token (per the requested usage).
[ $# -gt 0 ] && [ -n "${1:-}" ] && TOKEN="$1"

case "$SEL" in
  list) do_list; exit 0 ;;
  all)      mapfile -t LINES < <(rows) ;;
  llm)      mapfile -t LINES < <(rows | awk -F'|' '$4=="llm"') ;;
  comfyui|comfy) mapfile -t LINES < <(rows | awk -F'|' '$4=="comfy"') ;;
  *)
    if line="$(resolve "$SEL")"; then LINES=("$line"); else
      echo "error: no model with id/number '$SEL'. Run: $0 list" >&2; exit 1
    fi ;;
esac

[ -z "$TOKEN" ] && echo "note: no HF token given; gated/private models will fail (pass as 2nd arg, -t, or HF_TOKEN)." >&2
rc=0
for l in "${LINES[@]}"; do [ -n "$l" ] && { download_one "$l" "$TOKEN" || rc=1; }; done
exit $rc
