#!/usr/bin/env bash
# Build the Kali sandbox image.
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="${IMAGE:-kali-sandbox:latest}"
echo ">> Building $IMAGE (this pulls the full Kali toolset and takes a while)..."
podman build -t "$IMAGE" -f Containerfile .
echo ">> Done. Image: $IMAGE"
podman images "$IMAGE"
