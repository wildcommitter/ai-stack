<div align="center">

# 🧩 ai-stack

### A complete, self-hosted AI stack for the **AMD Radeon AI PRO R9700** — chat, web search, image generation, browser automation, and a sandboxed security lab, all local and GPU-accelerated.

![Podman](https://img.shields.io/badge/Podman-rootless-892CA0)
![ROCm](https://img.shields.io/badge/ROCm-gfx1201%20(RDNA4)-ED1C24)
![AMD R9700](https://img.shields.io/badge/GPU-Radeon%20AI%20PRO%20R9700%2032GB-000000)
![Open WebUI](https://img.shields.io/badge/Open%20WebUI-chat%20%2B%20tools-0A0A0A)
![MCP](https://img.shields.io/badge/MCP-3%20tool%20servers-2EA043)
![systemd](https://img.shields.io/badge/systemd-supervised-30A3DC)

</div>

---

**ai-stack** turns a single AMD GPU workstation into a private, offline-capable AI
platform. One chat window drives a local LLM that can search the web, generate and
describe images, pilot a headless browser, and run a network-sandboxed Linux
security lab — orchestrated with rootless **Podman**, wired together over the
**Model Context Protocol (MCP)**, and brought up on boot by **systemd**. No cloud,
no API keys leaving the box, every port bound to `127.0.0.1`.

It's built and tuned for one specific, increasingly popular card — so if you have
the same hardware, you get the whole experience with a clone and one script.

## 🎯 Built for the AMD Radeon AI PRO R9700

AMD's RDNA4 workstation card (**32 GB**, `gfx1201`) is a compelling local-AI GPU,
but the software path is less trodden than CUDA's. This repo is the opinionated,
working answer: every component runs on **ROCm** against `gfx1201`, and the one
genuinely hard problem on a 32 GB card — **fitting a 27B chat model *and* a
diffusion pipeline on the same VRAM** — is solved here, not hand-waved.

> New to ROCm on RDNA4? That's the point. Clone it, point it at your models, and
> skip the weeks of figuring out which flags, images and VRAM dances actually work.

## ✨ Highlights

- **One chat, many tools.** Open WebUI + a 27B LLM that calls MCP tools for web
  search, image gen/VLM, browsing, and a Kali security lab.
- **Smart VRAM sharing.** `llama-swap` loads/unloads GGUF models on demand and
  frees the GPU so ComfyUI can run — the 27B (~28 GB) and image models never
  fight over the 32 GB card.
- **Rootless & private.** Everything runs as a normal user under Podman; all
  services listen on loopback only.
- **Reproducible.** Portable `compose.yaml` + `.env`, one-shot `install.sh`,
  systemd units for supervised boot, and a checksum-verifying model downloader.
- **Composable by design.** Tools are MCP servers, so the same capabilities plug
  into any MCP-aware client, not just Open WebUI.

## 🗺️ Architecture

```
                 ┌───────────────── Open WebUI (chat UI) ─────────────────┐
  you  ──────►   │  default model: Qwen3.8-27B (local)                     │
                 └───┬───────────────┬───────────────┬───────────────┬────┘
                     │ /v1 (chat)    │ MCP           │ MCP           │ MCP
                     ▼               ▼               ▼               ▼
                  llm            mcp-search     playwright-mcp     kali-mcp
               (llama-swap)         │                               │ podman.sock
               GGUF on demand       ▼                               ▼  (exec)
               ROCm / gfx1201     searxng                      kali-sandbox
                                                            (egress-pinned lab)

  comfyui (image gen, ROCm) is invoked by Open WebUI tools; llama-swap frees
  VRAM first so the diffusion pipeline fits. All ports bind to 127.0.0.1.
```

## 🧱 Services

| Service | Image | Port | Role |
|---|---|---|---|
| `llm` | `llamacpp-rocm` (local) | 8080 | llama-swap: on-demand GGUF load/unload, OpenAI API at `/v1` |
| `comfyui` | `comfyui-rocm` (local) | 8188 | Qwen-Image-2.1 text-to-image / VLM |
| `searxng` | `searxng/searxng` | 8888 | private metasearch behind web search |
| `webui` | `open-webui` | 3000 | chat UI + tool orchestration |
| `mcp-search` | local build | 8765 | MCP: `web_search` via SearXNG |
| `playwright-mcp` | `playwright/mcp` | 8931 | MCP: headless-browser automation |
| `kali-mcp` | local build | 8099 | MCP: security-lab tools + shell + file ops |
| `kali-sandbox` | `kali-sandbox` (local) | — | the Kali lab itself; network-egress-pinned |

The `kali-mcp` server holds no tools itself — it execs into the separate,
egress-pinned `kali-sandbox` container over the host's Podman socket, so the
lab's traffic stays confined to one interface/VPN. See
[`services/kali-mcp/README.md`](services/kali-mcp/README.md).

## 🚀 Quick start

**Prerequisites:** an AMD ROCm GPU (built for the R9700 / `gfx1201`), rootless
Podman + `podman-compose`, and the local images built
(`llamacpp-rocm`, `comfyui-rocm`, and the Kali sandbox via
`services/kali-mcp/scripts/build.sh`).

```bash
git clone https://github.com/wildcommitter/ai-stack.git && cd ai-stack
./scripts/install.sh                 # writes .env + secrets, installs systemd units, makes the volume
$EDITOR .env                         # GPU GIDs, model paths (see getent group render/video)
./download-model.sh list             # browse the model catalog
./download-model.sh all <HF_TOKEN>   # fetch models (sha256-verified) into your model dirs
systemctl --user start ai-stack.service
```

Open **http://localhost:3000** and start chatting. `ai-stack.service` brings the
sandbox up first, then the compose stack; linger keeps it all running across
reboots.

### Models

`download-model.sh` knows each model's Hugging Face source and destination:

```bash
./download-model.sh list                   # ids, numbers, on-disk status
./download-model.sh 1 <HF_TOKEN>            # by number
./download-model.sh comfyui <HF_TOKEN>      # a group (llm | comfyui | all)
```

Downloads are resumable and **sha256-verified** against Hugging Face's published
checksums; a correct file is skipped, a mismatched one is re-fetched cleanly.

## 🧠 The interesting engineering: VRAM handoff

A 32 GB card can hold a 27B LLM (~28 GB with KV cache) *or* an image pipeline —
not both. ai-stack treats VRAM as a shared resource:

- `llama-swap` loads the chat model on first request and **unloads it after an
  idle TTL**, so the GPU frees up when you stop chatting.
- The Open WebUI image tool **unloads the LLM before** submitting a ComfyUI job,
  then frees ComfyUI's VRAM afterward so chat can resume.
- A small coder model (1.5B, for IDE autocomplete) is light enough to coexist.

The result: one GPU, many workloads, no OOM, no manual juggling. The details live
in [`config/llama-swap.yaml`](config/llama-swap.yaml).

## 📂 Layout

```
ai-stack/
├── compose.yaml          # the whole stack (portable paths via .env)
├── .env.example          # host values: GPU GIDs, model paths, socket
├── download-model.sh     # checksum-verified HF model fetcher
├── config/               # llama-swap.yaml, searxng settings (example)
├── services/
│   ├── llm/              # llamacpp-rocm image (ROCm/gfx1201)
│   ├── comfyui/          # ComfyUI image + model downloader + WebUI tools
│   ├── mcp-search/       # SearXNG-backed MCP server
│   └── kali-mcp/         # security lab image + MCP bridge (own README)
├── systemd/              # ai-stack.service, kali-sandbox.service
├── scripts/install.sh    # one-shot host setup
└── AGENTS.md             # guide for contributors / AI agents
```

## 🔐 Security & responsible use

The `kali-mcp` / `kali-sandbox` component is a **learning and authorized-testing**
environment. It runs standard, openly-available security tooling inside a
network-isolated container whose egress you pin to an interface or VPN. Use it
**only** against systems you own or have explicit written permission to test.
It's here to demonstrate sandboxing, MCP tool design and AI-driven automation —
not to attack anyone.

Secrets (`.env`, SearXNG `secret_key`) are generated locally from `*.example`
files and are never committed; model weights live outside the repo.

## 📜 License

See [`LICENSE`](LICENSE).

---

<div align="center">
<sub>Local-first AI on AMD. Built with Podman, ROCm and a lot of VRAM accounting.</sub>
</div>
