#!/usr/bin/env bash
# Install the ai-stack onto this host: config files, systemd user units, volume.
# Idempotent. Run as the user who will own the rootless containers.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
cd "$REPO"

echo ">> Repo: $REPO"

# 1. Host config from examples (never overwrite an existing real file).
[ -f .env ] || { cp .env.example .env; echo ">> created .env (edit it: GPU GIDs, model paths)"; }
[ -f config/searxng/settings.yml ] || {
  cp config/searxng/settings.yml.example config/searxng/settings.yml
  secret="$(openssl rand -hex 32)"
  sed -i "s/^\(\s*secret_key:\).*/\1 \"$secret\"/" config/searxng/settings.yml
  echo ">> created config/searxng/settings.yml with a fresh secret_key"
}
[ -f "$HOME/.config/kali-sandbox.env" ] || {
  mkdir -p "$HOME/.config"; cp systemd/kali-sandbox.env.example "$HOME/.config/kali-sandbox.env"
  echo ">> created ~/.config/kali-sandbox.env (set TUNNEL_IFACE for egress pinning)"
}

# 2. Open WebUI data volume (external in compose.yaml so data survives project renames).
podman volume inspect user_openwebui >/dev/null 2>&1 || {
  podman volume create user_openwebui; echo ">> created volume user_openwebui"; }

# 3. systemd user units (installed with %h specifiers intact).
mkdir -p "$UNIT_DIR"
cp systemd/kali-sandbox.service systemd/ai-stack.service "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable kali-sandbox.service ai-stack.service
echo ">> systemd units installed + enabled"

# 4. Linger so the stack starts at boot without an interactive login (needs privilege).
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo ">> enabling linger (sudo):"
  sudo loginctl enable-linger "$USER" || echo "!! could not enable linger; run: sudo loginctl enable-linger $USER"
fi

cat <<EOF

Done. Next:
  1. Edit .env (GPU GIDs, MODELS_DIR, COMFYUI_* paths) and download models
     (see services/comfyui/download-models.sh; GGUF LLMs into MODELS_DIR).
  2. Build/pull the local images referenced in compose.yaml (llamacpp-rocm,
     comfyui-rocm) and the Kali sandbox (services/kali-mcp/scripts/build.sh).
  3. Start:   systemctl --user start ai-stack.service
     (pulls in kali-sandbox.service first; ai-stack runs podman-compose up -d)
EOF
