# ai-stack

A self-hosted, GPU-accelerated AI stack on rootless **Podman**, wired together so
a local LLM in **Open WebUI** can search the web, generate/inspect images, drive a
headless browser, and run a sandboxed **Kali** red-team toolbox — all behind
`127.0.0.1` and supervised by systemd.

> Personal homelab + offensive-security tooling. Use the Kali tools only against
> systems you are authorized to test.

## Services

| Service | Image | Port (loopback) | Role |
|---|---|---|---|
| `llm` | `llamacpp-rocm` (local) | 8080 | llama-swap: load/unload GGUF models, OpenAI API at `/v1` |
| `comfyui` | `comfyui-rocm` (local) | 8188 | Qwen-Image-2.1 text-to-image / VLM |
| `searxng` | `searxng/searxng` | 8888 | private metasearch backing web search |
| `webui` | `open-webui` | 3000 | chat UI + tool orchestration |
| `webui-init` | `python:3.12-slim` | — | one-shot: installs the ComfyUI tools into Open WebUI |
| `mcp-search` | local build | 8765 | MCP: `web_search` via SearXNG |
| `playwright-mcp` | `mcr.microsoft.com/playwright/mcp` | 8931 | MCP: headless browser |
| `kali-mcp` | local build | 8099 | MCP: Kali red-team tools + bash + file ops |
| `kali-sandbox` | `kali-sandbox` (local) | — | the actual Kali toolbox; egress-pinned; **not** a compose service |

`kali-mcp` holds no tools itself — it execs into the `kali-sandbox` container
(started by `kali-sandbox.service`) over the host's podman socket, so its network
egress stays pinned to one interface/VPN. See
[`services/kali-mcp/README.md`](services/kali-mcp/README.md).

## How it fits together

```
                 ┌───────────────── Open WebUI (chat UI) ─────────────────┐
  you  ──────►   │  default model: qwen3.8-27b-heretic                     │
                 └───┬───────────────┬───────────────┬───────────────┬────┘
                     │ /v1 (chat)    │ MCP           │ MCP           │ MCP
                     ▼               ▼               ▼               ▼
                  llm            mcp-search     playwright-mcp     kali-mcp
               (llama-swap)         │                               │ podman.sock
               loads GGUF           ▼                               ▼  (exec)
               on demand          searxng                      kali-sandbox
                                                            (egress-pinned Kali)
  comfyui (image gen) is called by Open WebUI tools; llama-swap frees VRAM first.
```

All host ports bind to `127.0.0.1`. `ai-stack.service` (systemd, user) runs the
compose stack after `kali-sandbox.service`; see [`AGENTS.md`](AGENTS.md) for how
to work in this repo.

## Layout

```
ai-stack/
├── compose.yaml                 # the whole stack (portable paths via .env)
├── .env.example                 # host values: GPU GIDs, model paths, socket
├── config/
│   ├── llama-swap.yaml           # LLM model definitions / swap rules
│   └── searxng/settings.yml.example
├── services/
│   ├── llm/                      # Dockerfile for the llamacpp-rocm image
│   ├── comfyui/                  # Dockerfile, model downloader, WebUI tool installers
│   ├── mcp-search/               # SearXNG-backed MCP server
│   └── kali-mcp/                 # Kali sandbox image + MCP bridge (own README)
├── systemd/                      # kali-sandbox.service, ai-stack.service (+ env example)
└── scripts/install.sh            # one-shot host setup
```

## Deploy on a fresh host

Prereqs: rootless Podman + `podman-compose`, an AMD ROCm GPU (for `llm`/`comfyui`),
and the local images built (`llamacpp-rocm`, `comfyui-rocm`, and the Kali sandbox
via `services/kali-mcp/scripts/build.sh`).

```bash
git clone <your-fork> ai-stack && cd ai-stack
./scripts/install.sh          # writes .env + secrets, installs systemd units, makes the volume
$EDITOR .env                  # set GPU GIDs + model paths
./download-model.sh list      # see the model catalog
./download-model.sh all <HF_TOKEN>   # fetch every model into MODELS_DIR / COMFYUI_MODELS_DIR
systemctl --user start ai-stack.service
```

### Models (`download-model.sh`)

The model files live outside the repo (at the `.env` paths). `download-model.sh`
knows each one's Hugging Face source and destination:

```bash
./download-model.sh list                 # catalog with ids, numbers, on-disk status
./download-model.sh 1 <HF_TOKEN>          # by list number
./download-model.sh llm-heretic <HF_TOKEN># by id
./download-model.sh comfyui <HF_TOKEN>    # a group (llm | comfyui | all)
```

The HF token is optional for public models, required for gated ones (2nd arg,
`-t`, or `HF_TOKEN`). Downloads are resumable and **sha256-verified** against
HF's published checksum; an already-correct file is skipped. A size-mismatched
file is re-downloaded fresh (pass `--resume` to continue a genuine partial).

`ai-stack.service` pulls in `kali-sandbox.service` first, then runs
`podman-compose up -d`. Everything comes back on reboot (linger is enabled by
`install.sh`).

## Day-to-day

```bash
systemctl --user status ai-stack.service kali-sandbox.service
systemctl --user restart ai-stack.service       # redeploy after editing compose.yaml/.env
podman-compose -f compose.yaml logs -f webui     # tail a service
```

## Notes / host specifics

- **GPU handoff:** the 27B LLM (~28 GB VRAM) and ComfyUI can't coexist on a 32 GB
  card; llama-swap unloads on idle and the Open WebUI image tool frees ComfyUI's
  VRAM — see comments in `config/llama-swap.yaml`.
- **Secrets** (`.env`, `config/searxng/settings.yml`) are generated from `*.example`
  by `install.sh` and are gitignored.
- **Models** (tens of GB) live outside the repo at the paths in `.env`.
- **Open WebUI data** is the external podman volume `user_openwebui`.
