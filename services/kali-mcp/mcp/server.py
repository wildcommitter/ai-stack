#!/usr/bin/env python3
"""
MCP server for the Kali red-team sandbox.

Design: this server runs on the HOST and executes every command inside the
running Kali container via `podman exec`. The container's network namespace
(set up by scripts/run-sandbox.sh) is the sandbox boundary, so nothing an AI
client asks for can escape the interface/VPN you pinned egress to.

Exposed tools:
  - run_bash:       run any shell command inside the container (generic escape
                    hatch so an AI can drive the box freely).
  - run_tool:       run one catalog tool (validated against tools.yaml) + args.
  - list_tools:     the tool catalog, annotated with what's actually installed.
  - sandbox_status: is the container up, and what's its egress IP.
  - nmap_scan / http_probe / web_content_scan: ergonomic structured wrappers.

Transport: stdio (the standard for local MCP servers). Point your MCP client at
`python3 mcp/server.py`.

Env:
  SANDBOX_NAME   container name (default: kali-sandbox)
  EXEC_USER      user to exec as inside container (default: operator)
  MAX_OUTPUT     max bytes of combined output returned (default: 100000)
  DEFAULT_TIMEOUT seconds before a command is killed (default: 300)
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

SANDBOX_NAME = os.environ.get("SANDBOX_NAME", "kali-sandbox")
EXEC_USER = os.environ.get("EXEC_USER", "operator")
MAX_OUTPUT = int(os.environ.get("MAX_OUTPUT", "100000"))
DEFAULT_TIMEOUT = int(os.environ.get("DEFAULT_TIMEOUT", "300"))
CATALOG_PATH = Path(__file__).with_name("tools.yaml")
DESCRIPTIONS_PATH = Path(__file__).with_name("descriptions.json")

# Transport: "stdio" (default, for direct Claude Code use) or "streamable-http"
# (for running as a service in the ai-stack, reachable at http://host:PORT/mcp).
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))

if MCP_TRANSPORT == "streamable-http":
    mcp = FastMCP("kali-sandbox", host="0.0.0.0", port=MCP_PORT, stateless_http=True)
else:
    mcp = FastMCP("kali-sandbox")


def _load_catalog() -> dict[str, str]:
    """Flatten tools.yaml into {binary: description}."""
    raw = yaml.safe_load(CATALOG_PATH.read_text())
    flat: dict[str, str] = {}
    for group in raw.values():
        if isinstance(group, dict):
            flat.update(group)
    return flat


def _load_descriptions() -> dict[str, dict]:
    """Load tldr-generated descriptions (scripts/gen-descriptions.py), if present."""
    if DESCRIPTIONS_PATH.exists():
        try:
            return json.loads(DESCRIPTIONS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


CATALOG = _load_catalog()


def _describe(name: str) -> dict:
    """Best description for a tool: tldr-generated if available, else catalog."""
    gen = _load_descriptions().get(name, {})
    return {
        "description": gen.get("description") or CATALOG.get(name, ""),
        "source": gen.get("source", "catalog"),
    }


def _as_text(value) -> str:
    """Coerce subprocess output to str.

    On timeout, CPython leaves ``TimeoutExpired.stdout``/``.stderr`` as raw
    *bytes* even when the call used ``text=True`` — the decode step is skipped
    on the exception path. Concatenating that with a str (as the timeout
    handler does) raised ``TypeError: can't concat str to bytes``.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _truncate(text: str) -> str:
    text = _as_text(text)
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n\n[... truncated, {len(text) - MAX_OUTPUT} more bytes ...]"


def _podman_exec(inner_cmd: list[str], timeout: int, workdir: str = "/work") -> dict:
    """Run a command inside the sandbox container and capture the result."""
    cmd = [
        "podman", "exec",
        "--user", EXEC_USER,
        "--workdir", workdir,
        SANDBOX_NAME,
        *inner_cmd,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "exit_code": None,
            "timed_out": True,
            "stdout": _truncate(e.stdout),
            "stderr": _truncate(_as_text(e.stderr) + f"\n[killed after {timeout}s]"),
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "exit_code": None,
            "error": "podman not found on host, or container not reachable.",
        }
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "timed_out": False,
        "stdout": _truncate(proc.stdout),
        "stderr": _truncate(proc.stderr),
    }


def _container_running() -> bool:
    r = subprocess.run(
        ["podman", "inspect", "-f", "{{.State.Running}}", SANDBOX_NAME],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and r.stdout.strip() == "true"


@mcp.tool()
def run_bash(command: str, timeout_seconds: int = DEFAULT_TIMEOUT, workdir: str = "/work") -> dict:
    """Run an arbitrary bash command inside the Kali sandbox container.

    This is the generic escape hatch: use it for anything not covered by a
    named wrapper. The command runs through `bash -lc` inside the container's
    sandboxed network namespace. Results go into /work (persisted to the host).

    Args:
        command: the shell command line to execute.
        timeout_seconds: kill the command after this many seconds.
        workdir: working directory inside the container.
    """
    if not _container_running():
        return {"ok": False, "error": f"Container '{SANDBOX_NAME}' is not running. Start it with scripts/run-sandbox.sh."}
    return _podman_exec(["bash", "-lc", command], timeout_seconds, workdir)


@mcp.tool()
def run_tool(tool: str, args: str = "", timeout_seconds: int = DEFAULT_TIMEOUT) -> dict:
    """Run one catalog tool with arguments inside the sandbox.

    `tool` is validated against the catalog (tools.yaml) so the AI gets a known,
    named menu. For anything outside the catalog, use run_bash.

    Args:
        tool: binary name from the catalog (see list_tools).
        args: argument string (shell-split; e.g. "-sV -p80,443 10.0.0.5").
        timeout_seconds: kill the command after this many seconds.
    """
    if tool not in CATALOG:
        return {"ok": False, "error": f"'{tool}' is not in the catalog. Use list_tools, or run_bash for arbitrary commands."}
    if not _container_running():
        return {"ok": False, "error": f"Container '{SANDBOX_NAME}' is not running."}
    try:
        arg_list = shlex.split(args)
    except ValueError as e:
        return {"ok": False, "error": f"Could not parse args: {e}"}
    return _podman_exec([tool, *arg_list], timeout_seconds)


@mcp.tool()
def list_tools() -> dict:
    """List the tool catalog, annotated with which binaries are installed in the running container."""
    installed: set[str] = set()
    if _container_running():
        names = " ".join(shlex.quote(t) for t in CATALOG)
        probe = _podman_exec(
            ["bash", "-lc", f"for t in {names}; do command -v \"$t\" >/dev/null 2>&1 && echo \"$t\"; done"],
            timeout=60,
        )
        if probe.get("ok"):
            installed = set(probe["stdout"].split())
    return {
        "container_running": _container_running(),
        "descriptions_from_tldr": DESCRIPTIONS_PATH.exists(),
        "tools": [
            {
                "name": name,
                "description": _describe(name)["description"],
                "description_source": _describe(name)["source"],
                "installed": name in installed,
            }
            for name in sorted(CATALOG)
        ],
    }


@mcp.tool()
def sandbox_status() -> dict:
    """Report whether the sandbox is running and what public IP its egress uses.

    The egress IP confirms your traffic is actually leaving through the
    interface/VPN you pinned (not your default route).
    """
    if not _container_running():
        return {"running": False, "message": f"Container '{SANDBOX_NAME}' is not running."}
    egress = _podman_exec(["bash", "-lc", "curl -s --max-time 10 https://ifconfig.me || echo unknown"], timeout=20)
    routes = _podman_exec(["bash", "-lc", "ip route 2>/dev/null; echo '---'; ip -4 addr show 2>/dev/null"], timeout=20)
    return {
        "running": True,
        "name": SANDBOX_NAME,
        "egress_ip": egress.get("stdout", "").strip(),
        "network": routes.get("stdout", ""),
    }


@mcp.tool()
def nmap_scan(target: str, ports: str = "", flags: str = "-sV -T4", timeout_seconds: int = 1800) -> dict:
    """Run an nmap scan against a target.

    Args:
        target: host, CIDR, or file (e.g. 10.0.0.0/24, example.com).
        ports: port spec (e.g. "80,443" or "1-1000"); empty = nmap default.
        flags: nmap flags (default service/version detection, fast timing).
        timeout_seconds: kill after this many seconds.
    """
    args = flags.split()
    if ports:
        args += ["-p", ports]
    args.append(target)
    return run_tool("nmap", " ".join(shlex.quote(a) for a in args), timeout_seconds)


@mcp.tool()
def http_probe(url_or_host: str, extra: str = "", timeout_seconds: int = 120) -> dict:
    """Fingerprint a web target with whatweb (tech, server, title).

    Args:
        url_or_host: URL or hostname.
        extra: extra whatweb flags (e.g. "-a 3" for aggression level).
        timeout_seconds: kill after this many seconds.
    """
    return run_tool("whatweb", f"{extra} {shlex.quote(url_or_host)}".strip(), timeout_seconds)


@mcp.tool()
def web_content_scan(url: str, wordlist: str = "/usr/share/wordlists/dirb/common.txt",
                     extensions: str = "", timeout_seconds: int = 900) -> dict:
    """Directory/content brute force with gobuster against a URL.

    Args:
        url: base URL (e.g. http://target/).
        wordlist: path to a wordlist inside the container.
        extensions: comma list to append (e.g. "php,txt").
        timeout_seconds: kill after this many seconds.
    """
    args = ["dir", "-u", url, "-w", wordlist, "-q"]
    if extensions:
        args += ["-x", extensions]
    return run_tool("gobuster", " ".join(shlex.quote(a) for a in args), timeout_seconds)


# --------------------------------------------------------------------------- #
# File operations (CRUD) against the sandbox container.
# Transfers use `podman cp`; in-container edits/reads use exec. Paths default to
# /work, which is the host-mounted volume, but any container path is allowed.
# --------------------------------------------------------------------------- #

def _safe_container_path(path: str) -> str:
    # Absolute paths are used as-is; relative paths resolve under /work.
    return path if path.startswith("/") else f"/work/{path}"


@mcp.tool()
def upload_file(host_path: str, container_path: str = "/work/") -> dict:
    """Copy a file or directory FROM the host INTO the sandbox container.

    Args:
        host_path: path on the host machine to copy from.
        container_path: destination inside the container (default /work/).
    """
    if not _container_running():
        return {"ok": False, "error": f"Container '{SANDBOX_NAME}' is not running."}
    src = Path(host_path).expanduser()
    if not src.exists():
        return {"ok": False, "error": f"Host path does not exist: {src}"}
    dest = _safe_container_path(container_path)
    r = subprocess.run(["podman", "cp", str(src), f"{SANDBOX_NAME}:{dest}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {"ok": False, "error": _truncate(r.stderr.strip())}
    # podman cp lands as root; hand ownership to the exec user for /work files.
    target = dest if not dest.endswith("/") else dest + src.name
    subprocess.run(["podman", "exec", "--user", "root", SANDBOX_NAME,
                    "chown", "-R", f"{EXEC_USER}:{EXEC_USER}", target],
                   capture_output=True, text=True)
    return {"ok": True, "uploaded_to": target}


@mcp.tool()
def download_file(container_path: str, host_path: str) -> dict:
    """Copy a file or directory FROM the sandbox container TO the host.

    Args:
        container_path: path inside the container to copy from.
        host_path: destination path on the host machine.
    """
    if not _container_running():
        return {"ok": False, "error": f"Container '{SANDBOX_NAME}' is not running."}
    src = _safe_container_path(container_path)
    dest = Path(host_path).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["podman", "cp", f"{SANDBOX_NAME}:{src}", str(dest)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {"ok": False, "error": _truncate(r.stderr.strip())}
    return {"ok": True, "downloaded_to": str(dest)}


@mcp.tool()
def write_file(container_path: str, content: str, append: bool = False) -> dict:
    """Create or overwrite (or append to) a text file inside the container.

    Content is streamed over stdin, so it is safe regardless of quoting.

    Args:
        container_path: file path inside the container (relative -> under /work).
        content: text to write.
        append: append instead of overwriting.
    """
    if not _container_running():
        return {"ok": False, "error": f"Container '{SANDBOX_NAME}' is not running."}
    path = _safe_container_path(container_path)
    redirect = ">>" if append else ">"
    cmd = ["podman", "exec", "-i", "--user", EXEC_USER, SANDBOX_NAME,
           "bash", "-lc", f"mkdir -p \"$(dirname {shlex.quote(path)})\" && cat {redirect} {shlex.quote(path)}"]
    try:
        proc = subprocess.run(cmd, input=content, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "write timed out"}
    if proc.returncode != 0:
        return {"ok": False, "error": _truncate(proc.stderr.strip())}
    return {"ok": True, "path": path, "bytes": len(content.encode()), "mode": "append" if append else "overwrite"}


@mcp.tool()
def read_file(container_path: str, max_bytes: int = MAX_OUTPUT) -> dict:
    """Read a text file from inside the container.

    Args:
        container_path: file path inside the container (relative -> under /work).
        max_bytes: cap on returned content.
    """
    if not _container_running():
        return {"ok": False, "error": f"Container '{SANDBOX_NAME}' is not running."}
    path = _safe_container_path(container_path)
    res = _podman_exec(["bash", "-lc", f"head -c {int(max_bytes)} {shlex.quote(path)}"], timeout=60)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or res.get("error") or "read failed"}
    return {"ok": True, "path": path, "content": res["stdout"]}


@mcp.tool()
def list_files(container_path: str = "/work", long: bool = True) -> dict:
    """List a directory inside the container.

    Args:
        container_path: directory to list (relative -> under /work).
        long: include size/permissions (ls -la) vs. names only.
    """
    if not _container_running():
        return {"ok": False, "error": f"Container '{SANDBOX_NAME}' is not running."}
    path = _safe_container_path(container_path)
    flags = "-la" if long else "-1"
    res = _podman_exec(["bash", "-lc", f"ls {flags} {shlex.quote(path)}"], timeout=60)
    return {"ok": res.get("ok", False), "path": path,
            "listing": res.get("stdout", ""), "error": res.get("stderr", "")}


@mcp.tool()
def delete_path(container_path: str, recursive: bool = False) -> dict:
    """Delete a file or directory inside the container.

    Args:
        container_path: path to remove (relative -> under /work).
        recursive: required true to remove a non-empty directory.
    """
    if not _container_running():
        return {"ok": False, "error": f"Container '{SANDBOX_NAME}' is not running."}
    path = _safe_container_path(container_path)
    if path.rstrip("/") in ("", "/", "/work", "/home", "/usr", "/etc", "/var", "/bin"):
        return {"ok": False, "error": f"Refusing to delete protected path: {path}"}
    flag = "-rf" if recursive else "-f"
    res = _podman_exec(["bash", "-lc", f"rm {flag} {shlex.quote(path)}"], timeout=60)
    return {"ok": res.get("ok", False), "removed": path, "error": res.get("stderr", "")}


@mcp.tool()
def move_path(src_path: str, dest_path: str) -> dict:
    """Move or rename a file/directory inside the container.

    Args:
        src_path: source path (relative -> under /work).
        dest_path: destination path (relative -> under /work).
    """
    if not _container_running():
        return {"ok": False, "error": f"Container '{SANDBOX_NAME}' is not running."}
    src = _safe_container_path(src_path)
    dest = _safe_container_path(dest_path)
    res = _podman_exec(["bash", "-lc", f"mkdir -p \"$(dirname {shlex.quote(dest)})\" && mv {shlex.quote(src)} {shlex.quote(dest)}"], timeout=60)
    return {"ok": res.get("ok", False), "src": src, "dest": dest, "error": res.get("stderr", "")}


@mcp.tool()
def make_dir(container_path: str) -> dict:
    """Create a directory (and parents) inside the container.

    Args:
        container_path: directory path (relative -> under /work).
    """
    if not _container_running():
        return {"ok": False, "error": f"Container '{SANDBOX_NAME}' is not running."}
    path = _safe_container_path(container_path)
    res = _podman_exec(["bash", "-lc", f"mkdir -p {shlex.quote(path)}"], timeout=60)
    return {"ok": res.get("ok", False), "path": path, "error": res.get("stderr", "")}


if __name__ == "__main__":
    if MCP_TRANSPORT == "streamable-http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
