#!/usr/bin/env python3
"""
Generate tool descriptions by running `tldr` (tealdeer) INSIDE the sandbox
container, and write them to mcp/descriptions.json.

For every tool in mcp/tools.yaml we ask tealdeer for its page and extract the
one-line summary. list_tools then advertises these capability descriptions
(falling back to the curated text in tools.yaml when a tool has no tldr page).

Usage:
    python3 scripts/gen-descriptions.py            # uses SANDBOX_NAME or kali-sandbox
    SANDBOX_NAME=kali-sandbox python3 scripts/gen-descriptions.py

The container must be running (scripts/run-sandbox.sh).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CATALOG = ROOT / "mcp" / "tools.yaml"
OUT = ROOT / "mcp" / "descriptions.json"
SANDBOX_NAME = os.environ.get("SANDBOX_NAME", "kali-sandbox")

# Bash run inside the container for ONE tool: refresh nothing (cache already
# updated at build time), ask tldr for the page with color off, and pull out
# the summary. tealdeer prints the `>`-prefixed summary lines (minus the
# "More information" line) as the description.
# Read ONLY tealdeer's cached markdown page. The summary is the `>` lines,
# minus the "More information"/"See also" meta lines. No page -> empty output,
# so list_tools falls back to the curated catalog text. (We never scrape the
# rendered "page not found" notice, which looks like a description but isn't.)
REMOTE_SNIPPET = r'''
extract() {
  local name="$1"
  local md
  md="$(find "$HOME/.cache/tealdeer" -type f -name "${name}.md" 2>/dev/null \
        | grep -E '/(linux|common)/' | head -n1)"
  [ -z "$md" ] && md="$(find "$HOME/.cache/tealdeer" -type f -name "${name}.md" 2>/dev/null | head -n1)"
  [ -z "$md" ] && return
  grep '^>' "$md" \
    | grep -viE 'More information|See also' \
    | sed 's/^> *//' | paste -sd' ' -
}
'''


def remote_descriptions(tools: list[str]) -> dict[str, str]:
    # Build one script that loops over every tool and emits  name<TAB>summary.
    loop = REMOTE_SNIPPET + "\n"
    for t in tools:
        loop += f'printf "%s\\t" {shq(t)}; extract {shq(t)}; printf "\\n"\n'
    cmd = ["podman", "exec", "--user", "operator", SANDBOX_NAME, "bash", "-lc", loop]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0 and not proc.stdout:
        sys.exit(f"podman exec failed: {proc.stderr.strip()}")
    result: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "\t" not in line:
            continue
        name, _, desc = line.partition("\t")
        desc = desc.strip()
        if desc:
            result[name.strip()] = desc
    return result


def shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def load_tools() -> dict[str, str]:
    raw = yaml.safe_load(CATALOG.read_text())
    flat: dict[str, str] = {}
    for group in raw.values():
        if isinstance(group, dict):
            flat.update(group)
    return flat


def main() -> None:
    catalog = load_tools()
    names = sorted(catalog)
    print(f">> Ensuring tealdeer cache is populated in '{SANDBOX_NAME}' (operator)...")
    subprocess.run(
        ["podman", "exec", "--user", "operator", SANDBOX_NAME, "bash", "-lc", "tldr --update"],
        capture_output=True, text=True, timeout=300,
    )
    print(f">> Generating descriptions for {len(names)} tools via tldr in '{SANDBOX_NAME}'...")
    tldr_desc = remote_descriptions(names)
    merged = {
        name: {
            "tldr": tldr_desc.get(name, ""),
            "fallback": catalog[name],
            "description": tldr_desc.get(name) or catalog[name],
            "source": "tldr" if tldr_desc.get(name) else "catalog",
        }
        for name in names
    }
    OUT.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
    got = sum(1 for v in merged.values() if v["source"] == "tldr")
    print(f">> Wrote {OUT} ({got}/{len(names)} descriptions came from tldr, rest from catalog).")


if __name__ == "__main__":
    main()
