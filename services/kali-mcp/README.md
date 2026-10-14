# Kali red-team sandbox + MCP server

A Podman-based Kali Linux container with **network-sandboxed egress** (all
traffic pinned to one interface or forced through a VPN), the full
`kali-linux-everything` toolset, and an **MCP server** that lets an AI model
drive the box — via a generic bash tool and a validated catalog of ~100 common
Kali tools.

> **Authorized use only.** Point this at systems you own or have explicit
> written permission to test. The egress sandboxing is there so your testing
> traffic goes out exactly one controlled path and nothing leaks onto your
> normal network — it is not a license to scan third parties.

## Layout

```
cyberops/
├── Containerfile                 # Kali + kali-linux-everything + MCP deps
├── scripts/
│   ├── build.sh                  # build the image
│   └── run-sandbox.sh            # launch with controlled network egress
└── mcp/
    ├── server.py                 # MCP server (host-side, execs into container)
    ├── tools.yaml                # catalog / allowlist of ~100 Kali tools
    ├── requirements.txt
    └── mcp-client-config.example.json
```

## 1. Build the image

```bash
cd ~/cyberops
./scripts/build.sh          # large; pulls the full Kali toolset
```

Leaner alternative: edit `Containerfile`, replace `kali-linux-everything` with
`kali-linux-headless kali-tools-top10` (or specific `kali-tools-*` metapackages).

## 2. Launch with network sandboxing

The container gets its own rootless network namespace; you choose how it reaches
the outside world via `NET_MODE`:

**Pin egress to a specific interface or host IP** (default mode). All container
traffic leaves only through that address:

```bash
# via a WireGuard/VPN interface already up on the host:
NET_MODE=iface TUNNEL_IFACE=wg0 ./scripts/run-sandbox.sh
# or a plain NIC / explicit host IP:
NET_MODE=iface TUNNEL_IFACE=eth0 ./scripts/run-sandbox.sh
NET_MODE=iface TUNNEL_IFACE=10.8.0.2 ./scripts/run-sandbox.sh
```

**Run the VPN inside the container** (mount a WireGuard/OpenVPN config into
`./work` first):

```bash
NET_MODE=vpn VPN_CONFIG=/work/vpn/wg0.conf ./scripts/run-sandbox.sh
```

**Offline** (no network at all):

```bash
NET_MODE=none ./scripts/run-sandbox.sh
```

Confirm your egress path:

```bash
podman exec kali-sandbox curl -s https://ifconfig.me; echo
```

Everything written to `/work` in the container lands in `./work` on the host.

## 3. Set up the MCP server

```bash
cd ~/cyberops/mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Register it with your MCP client. For **Claude Code**:

```bash
claude mcp add kali-sandbox -- /home/user/cyberops/mcp/.venv/bin/python /home/user/cyberops/mcp/server.py
```

For **Claude Desktop** or other clients, merge `mcp-client-config.example.json`
into your client config (adjust the absolute paths).

The server execs into the running container, so start the container (step 2)
first. It talks stdio — no ports exposed.

### Tools the AI gets

| Tool | What it does |
|------|--------------|
| `run_bash(command, timeout_seconds, workdir)` | Run any command in the sandbox — the generic driver. |
| `run_tool(tool, args, timeout_seconds)` | Run one catalog tool (validated against `tools.yaml`). |
| `list_tools()` | The catalog, annotated with what's installed. |
| `sandbox_status()` | Container up? What egress IP is it using? |
| `nmap_scan(target, ports, flags)` | Ergonomic nmap wrapper. |
| `http_probe(url_or_host)` | whatweb fingerprinting. |
| `web_content_scan(url, wordlist, extensions)` | gobuster content discovery. |
| `upload_file(host_path, container_path)` | Copy a file/dir from host into the container. |
| `download_file(container_path, host_path)` | Copy a file/dir from the container to the host. |
| `write_file(container_path, content, append)` | Create/overwrite/append a text file (content via stdin). |
| `read_file(container_path, max_bytes)` | Read a text file from the container. |
| `list_files(container_path, long)` | List a directory in the container. |
| `make_dir(container_path)` | Create a directory (with parents). |
| `move_path(src_path, dest_path)` | Move/rename inside the container. |
| `delete_path(container_path, recursive)` | Remove a file/dir (protected paths refused). |

Edit `tools.yaml` to grow or trim the catalog; binaries not present in the
image are flagged as not installed by `list_tools`.

### Generate capability descriptions from `tldr`

The catalog's descriptions are fallbacks. To advertise richer, consistent
descriptions, run tealdeer **inside the container** and cache the results:

```bash
cd ~/cyberops
.venv/bin/python scripts/gen-descriptions.py     # needs the container running
```

This writes `mcp/descriptions.json`; `list_tools` then serves the tldr-derived
description per tool (`description_source: "tldr"`), falling back to the catalog
text where a tool has no tldr page.

## Running inside the ai-stack (Open WebUI)

The server is wired into `~/ai-stack.yml` as the `kali-mcp` service using the
**bridge** topology: it does not hold the tools, it execs into the separately
launched, egress-sandboxed `kali-sandbox` container via the host's podman socket.

```
Open WebUI chat → MCP client → http://kali-mcp:8000/mcp (compose service)
  → /run/podman/podman.sock → podman exec kali-sandbox → tool runs in the sandbox
```

Bring it up:

```bash
# 1. Start the egress-sandboxed Kali container (NOT part of compose):
cd ~/cyberops && NET_MODE=iface TUNNEL_IFACE=wg0 ./scripts/run-sandbox.sh
# 2. Start/refresh the stack service (builds cyberops/mcp -> kali-mcp:latest):
cd ~ && podman compose -f ai-stack.yml up -d kali-mcp
```

Reachable at `http://kali-mcp:8000/mcp` in-network and `http://localhost:8099/mcp`
on the host. It is registered in Open WebUI as the **Kali Red-Team Sandbox** tool
server (`tool_server.connections`, type `mcp`, enabled). The HTTP transport is
selected by `MCP_TRANSPORT=streamable-http` (the image default); unset, the server
speaks stdio for direct Claude Code use. The `kali-mcp` service must see the Kali
container by name, so start `kali-sandbox` **before** the LLM calls a tool.

### Supervised boot (systemd user units)

Two `systemd --user` units bring the whole thing up at boot, in order, with
lingering enabled (no login required):

```
kali-sandbox.service   # starts the egress-pinned Kali container first
   ↑ After= / Wants=
ai-stack.service       # podman-compose up -d (Open WebUI, llama-swap, ComfyUI,
                       # SearXNG, mcp-search, playwright-mcp, kali-mcp) after it
```

- `~/.config/systemd/user/kali-sandbox.service` — runs `scripts/run-sandbox.sh`
  (egress config via `~/.config/kali-sandbox.env`).
- `~/.config/systemd/user/ai-stack.service` — `podman-compose -f ~/ai-stack.yml up -d`,
  ordered `After=kali-sandbox.service`.

Control them:

```bash
systemctl --user status ai-stack.service kali-sandbox.service
systemctl --user restart ai-stack.service     # redeploy the stack
systemctl --user restart kali-sandbox.service  # recycle the sandbox
```

## Notes

- `run-sandbox.sh` grants `CAP_NET_RAW,NET_ADMIN` (needed for raw-packet
  scanners and bringing up tun/wg) instead of running fully privileged.
- Commands run as the non-root `operator` user by default (`EXEC_USER`).
- Output is truncated to `MAX_OUTPUT` bytes; long-running tools are killed at
  their timeout. Tune via env vars (see `server.py` header).
```
