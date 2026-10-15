# AGENTS.md

Guidance for AI agents (and humans) working in this repo. Read this before
editing — the stack is **live** on this host and several things bite if you
touch them naively.

## What this repo is

A rootless-Podman AI stack (Open WebUI + llama-swap + ComfyUI + SearXNG + three
MCP servers) plus a network-sandboxed Kali toolbox. It is the **live source**:
systemd user units run the stack straight from this directory. Changes here are
not hypothetical — they affect the running host on the next restart.

See [`README.md`](README.md) for the service map and deploy steps.

## Golden rules

1. **Never commit secrets.** `.env` and `config/searxng/settings.yml` are real
   and gitignored; only their `*.example` siblings are tracked. Before any
   commit, verify: `git grep -nE 'secret_key|hf_[A-Za-z0-9]{20,}|AKIA' $(git diff --cached --name-only)` → must be empty.
2. **Models never go in git.** Tens of GB live at the `.env` paths
   (`MODELS_DIR`, `COMFYUI_MODELS_DIR`). `.gitignore` blocks `*.gguf`/`*.safetensors`.
3. **The Open WebUI volume is external** (`user_openwebui`). Do not rename the
   compose project or declare the volume non-external, or you orphan all chat
   data + tool registrations.
4. **Confirm before destructive/outward actions** (removing containers, pushing
   to a remote, deleting volumes). The stack is someone's running system.

## Running & restarting

```bash
systemctl --user status  ai-stack.service kali-sandbox.service
systemctl --user restart ai-stack.service       # after editing compose.yaml / .env
systemctl --user restart kali-sandbox.service    # recycle just the Kali sandbox
podman-compose -f compose.yaml logs -f <svc>     # tail one service
podman-compose -f compose.yaml config            # validate compose + .env substitution
```

- `ai-stack.service` runs `podman-compose up -d`; it is ordered `After=` and
  `Wants=` `kali-sandbox.service`. Containers are named `ai-stack_<svc>_1`.
- Editing a service's build context (`services/mcp-search`, `services/kali-mcp/mcp`)
  needs a rebuild: `podman-compose -f compose.yaml build <svc>`, then recreate
  **only that service** (see next bullet) — not a whole-stack restart.
- **To deploy a change to ONE service, do NOT `systemctl restart ai-stack.service`.**
  That restart does a full `down`/`up` of every service, and the podman-compose
  dependency-link quirk (below) can fail the `up` and leave the **whole stack
  down** (symptom: `executable file 'python3'/'llama-swap' not found in $PATH`).
  Restart the whole unit only after editing `compose.yaml`/`.env`. For a single
  service, recreate just it:
  ```bash
  podman-compose -f compose.yaml build <svc>          # only if its image changed
  podman rm -f ai-stack_<svc>_1
  podman-compose -f compose.yaml up -d --no-deps <svc>
  podman inspect ai-stack_<svc>_1 --format '{{.Image}}' # confirm it's the NEW image, not the stale one
  ```
  If the recreate wedges on `name "ai-stack_<svc>_1" is already in use by <hash>`
  / `directory not empty` on the overlay `merged`, the container is stuck in
  storage: `podman unshare rm -rf <storage>/overlay/<HASH>/merged` then
  `podman rm --force --storage <hash>`, and retry the `up -d --no-deps`.

## Component-specific gotchas

- **llama-swap** has **no default-model / fallback key** — it routes by the
  request's `model` name (`/v1/models` is alphabetical). The stack's default
  model is Open WebUI's `DEFAULT_MODELS` (set in `compose.yaml`) / the
  `ui.default_models` config row. Don't invent a llama-swap default.
- **GPU handoff:** the 27B LLM (~28 GB) and ComfyUI can't share the 32 GB card.
  llama-swap unloads on idle TTL; don't add `persist: true` to the 27B.
- **ROCm kernel cache (cold-load speed):** on the gfx1201 arch, llama.cpp/ROCm
  JIT-compiles kernels on first inference and caches them in `/root/.cache`
  (comgr `llvmcache-*`). This is a **one-time ~3–5 min** cost. The `llm` and
  `comfyui` services mount persistent volumes (`llm_cache`, `comfyui_cache`) at
  `/root/.cache` so that cost survives container recreation — **do not remove
  them**, or the first inference after every `compose up`/rebuild/reboot stalls
  for minutes while it recompiles. With the cache warm, a 27B cold load is ~3 s.
  A truly fresh cache (new GPU/driver) pays the warmup once, then it's permanent.
- **kali-mcp is a bridge**, not a toolbox: it execs into the separate
  `kali-sandbox` container via the mounted podman socket. `kali-sandbox` is
  **not** a compose service — it is `kali-sandbox.service`. If tools report
  "container not running", start that unit.
- **kali egress** is pinned to an interface/VPN by `services/kali-mcp/scripts/run-sandbox.sh`
  (configure via `~/.config/kali-sandbox.env`, `TUNNEL_IFACE=...`).
- **Open WebUI config** lives in the `user_openwebui` volume's sqlite
  (`/app/backend/data/webui.db`, key-value `config` table). It is read into an
  in-memory snapshot at startup, so DB edits need a `webui` restart to take
  effect, and anonymous `/api/config` omits auth-gated fields (use the app's
  `Config.get` to verify, not an unauthenticated curl).

## Models (`download-model.sh`)

`./download-model.sh list` shows the catalog; download by id or number with an
optional HF token (2nd arg / `-t` / `HF_TOKEN`). Downloads are
**sha256-verified** against HF's published checksum.

- A present file whose checksum matches is skipped.
- A size/checksum **mismatch is re-downloaded fresh** — never resumed — so two
  different files are never spliced. Pass `--resume` only for a genuine
  interrupted partial of the *same* file.

## Conventions

- Shell: target `bash` (scripts use `#!/usr/bin/env bash`). Note the interactive
  shell on this host is **zsh**, where unquoted `$var` does not word-split — use
  arrays, quoting, or `xargs` when iterating command output.
- Keep host-specific values in `.env`; keep paths in `compose.yaml` repo-relative
  or `${VAR}`-driven. Absolute `/home/...` paths belong only in `.env`.
- Commit in focused pieces with a clear subject line. End commit messages with:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

## Before you commit

1. `podman-compose -f compose.yaml config` parses cleanly.
2. Secret scan on staged files is empty (rule 1 above).
3. Only intended files staged (`git status`); no `.venv/`, `work/`, models, or
   `*.log`.
