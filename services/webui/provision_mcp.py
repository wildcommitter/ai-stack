#!/usr/bin/env python3
"""Idempotently provision Open WebUI MCP tool servers for the ai-stack.

Mirrors services/comfyui/tools/install_function.py (same signin + REST pattern),
but instead of installing Python functions it:

  1. Registers each MCP server in `tool_server.connections` (type=mcp),
     enabled, upserting by info.id and preserving any other connections.
  2. Adds each `server:mcp:<id>` to the default model's meta.toolIds so the
     tools are selected by default in new chats.

Both steps go through the admin REST API, which refreshes Open WebUI's
in-memory config snapshot (no webui restart needed).

Config via env:
  OPEN_WEBUI_URL        base url (default http://webui:8080)
  WEBUI_ADMIN_EMAIL     admin login (default admin@localhost)
  WEBUI_ADMIN_PASSWORD  admin password (default admin)
  DEFAULT_MODEL         model id to attach the tools to (default DEFAULT_MODELS)
  MCP_SERVERS           JSON list of {id,name,description,url} (see default below)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("OPEN_WEBUI_URL", "http://webui:8080").rstrip("/")
SIGNIN_EMAIL = os.environ.get("WEBUI_ADMIN_EMAIL", "admin@localhost")
SIGNIN_PASSWORD = os.environ.get("WEBUI_ADMIN_PASSWORD", "admin")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "").strip()

# The ai-stack MCP servers, reachable by compose service name on the internal
# network. Override wholesale with the MCP_SERVERS env var (same JSON shape).
DEFAULT_MCP_SERVERS = [
    {
        "id": "searxng-search",
        "name": "SearXNG Search",
        "description": "Web search backed by the local SearXNG instance",
        "url": "http://mcp-search:8000/mcp",
    },
    {
        "id": "playwright",
        "name": "Playwright Browser",
        "description": "Headless browser automation (navigate, click, screenshot, extract) via Playwright",
        "url": "http://playwright-mcp:8931/mcp",
    },
    {
        "id": "kali-sandbox",
        "name": "Kali Red-Team Sandbox",
        "description": "Run Kali tools (nmap, sqlmap, gobuster, hydra, ...), generic bash, and file CRUD inside a network-sandboxed Kali container.",
        "url": "http://kali-mcp:8000/mcp",
    },
]


def load_servers():
    raw = os.environ.get("MCP_SERVERS", "").strip()
    if raw:
        servers = json.loads(raw)
        if isinstance(servers, dict):
            servers = [servers]
        return servers
    return DEFAULT_MCP_SERVERS


def http_json(method, path, payload=None, token=None, timeout=30):
    url = f"{BASE_URL}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} for {method} {path}: {detail}") from e


def wait_for_webui():
    while True:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/health", timeout=5) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        print(f"Waiting for Open WebUI at {BASE_URL}...", flush=True)
        time.sleep(3)


def signin():
    body = http_json(
        "POST",
        "/api/v1/auths/signin",
        payload={"email": SIGNIN_EMAIL, "password": SIGNIN_PASSWORD},
    )
    token = (body or {}).get("token")
    if not token:
        raise RuntimeError(f"No token in signin response: {body}")
    return token


def mcp_connection(server, existing=None):
    """Build a tool_server connection for an MCP server, enabled, merging any
    existing entry so manual tweaks (headers/auth/filters) are preserved."""
    base = dict(existing) if existing else {}
    config = dict(base.get("config") or {})
    config["enable"] = True
    base.update(
        {
            "url": server["url"],
            "path": base.get("path", ""),
            "type": "mcp",
            "auth_type": base.get("auth_type") or "none",
            "headers": base.get("headers"),
            "key": base.get("key"),
            "config": config,
            "info": {
                "id": server["id"],
                "name": server["name"],
                "description": server["description"],
            },
        }
    )
    return base


def provision_tool_servers(token, servers):
    current = http_json("GET", "/api/v1/configs/tool_servers", token=token) or {}
    connections = list(current.get("TOOL_SERVER_CONNECTIONS") or [])
    by_id = {
        (c.get("info") or {}).get("id"): i
        for i, c in enumerate(connections)
        if (c.get("info") or {}).get("id")
    }
    for server in servers:
        conn = mcp_connection(server, existing=connections[by_id[server["id"]]] if server["id"] in by_id else None)
        if server["id"] in by_id:
            connections[by_id[server["id"]]] = conn
            print(f"Updating MCP tool server {server['id']} (enabled)", flush=True)
        else:
            connections.append(conn)
            print(f"Creating MCP tool server {server['id']} (enabled)", flush=True)
    http_json(
        "POST",
        "/api/v1/configs/tool_servers",
        payload={"TOOL_SERVER_CONNECTIONS": connections},
        token=token,
    )
    print(f"Tool server connections set ({len(connections)} total)", flush=True)


def attach_defaults_to_model(token, servers, model_id):
    if not model_id:
        print("No DEFAULT_MODEL set; skipping default-tool attachment.", flush=True)
        return
    try:
        model = http_json("GET", f"/api/v1/models/model?id={model_id}", token=token)
    except RuntimeError as e:
        print(f"WARNING: default model '{model_id}' not found; "
              f"MCP servers are registered+enabled but not set as defaults ({e}).", flush=True)
        return
    if not model:
        print(f"WARNING: default model '{model_id}' not found; skipping default-tool attachment.", flush=True)
        return
    meta = model.get("meta") or {}
    tool_ids = list(meta.get("toolIds") or [])
    wanted = [f"server:mcp:{s['id']}" for s in servers]
    added = [t for t in wanted if t not in tool_ids]
    if not added:
        print(f"Default model '{model_id}' already has all MCP tools as defaults.", flush=True)
        return
    tool_ids.extend(added)
    meta["toolIds"] = tool_ids
    http_json(
        "POST",
        f"/api/v1/models/model/update?id={model_id}",
        payload={
            "id": model["id"],
            "base_model_id": model.get("base_model_id"),
            "name": model["name"],
            "meta": meta,
            "params": model.get("params") or {},
            "access_grants": model.get("access_grants") or [],
            "is_active": model.get("is_active", True),
        },
        token=token,
    )
    print(f"Attached {added} as defaults on model '{model_id}'.", flush=True)


def main():
    servers = load_servers()
    wait_for_webui()
    token = signin()
    print("Signed in to Open WebUI", flush=True)
    provision_tool_servers(token, servers)
    attach_defaults_to_model(token, servers, DEFAULT_MODEL)
    print("MCP provisioning complete.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"webui-mcp-init failed: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
