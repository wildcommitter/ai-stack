# llm image (llamacpp-rocm)

Builds `localhost/llamacpp-rocm:gfx1201-v263`, the llama-swap + llama.cpp (ROCm
gfx1201) image used by the `llm` service in `compose.yaml`.

```bash
podman build -t localhost/llamacpp-rocm:gfx1201-v263 -f Containerfile .
```

Model definitions and swap/TTL rules live in `../../config/llama-swap.yaml`.
GGUF model files live outside the repo at `$MODELS_DIR` (see `.env`).
