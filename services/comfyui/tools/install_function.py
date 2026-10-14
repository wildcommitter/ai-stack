import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("OPEN_WEBUI_URL", "http://webui:8080").rstrip("/")
SIGNIN_EMAIL = os.environ.get("WEBUI_ADMIN_EMAIL", "admin@localhost")
SIGNIN_PASSWORD = os.environ.get("WEBUI_ADMIN_PASSWORD", "admin")


def default_specs():
    """Legacy single-function env vars, used when FUNCTION_SPECS is not set."""
    return [
        {
            "id": os.environ.get("FUNCTION_ID", "comfyui_qwen21_t2i"),
            "name": os.environ.get("FUNCTION_NAME", "ComfyUI Qwen-Image-2.1 Text-to-Image"),
            "file": os.environ.get("FUNCTION_FILE", "/functions/comfyui_qwen21_t2i.py"),
        }
    ]


def load_specs():
    raw = os.environ.get("FUNCTION_SPECS", "").strip()
    if raw:
        specs = json.loads(raw)
        if isinstance(specs, dict):
            specs = [specs]
        return specs
    return default_specs()


def http_json(method, path, payload=None, token=None, timeout=30):
    url = f"{BASE_URL}{path}"
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            if not body:
                return None
            return json.loads(body)
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
    payload = {"email": SIGNIN_EMAIL, "password": SIGNIN_PASSWORD}
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}/api/v1/auths/signin",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))
    token = body.get("token")
    if not token:
        raise RuntimeError(f"No token in signin response: {body}")
    return token


def get_tool(token, function_id):
    result = http_json("GET", "/api/v1/tools/", token=token)
    if not isinstance(result, list):
        return None
    for item in result:
        if item.get("id") == function_id:
            return item
    return None


def install_one(token, spec):
    function_id = spec["id"]
    name = spec["name"]
    with open(spec["file"], "r", encoding="utf-8") as f:
        content = f.read()

    tool = get_tool(token, function_id)
    if tool is None:
        print(f"Creating tool {function_id}", flush=True)
        http_json(
            "POST",
            "/api/v1/tools/create",
            payload={
                "id": function_id,
                "name": name,
                "content": content,
                "meta": {},
                "access_grants": [],
            },
            token=token,
        )
        if get_tool(token, function_id) is None:
            raise RuntimeError(f"Tool {function_id} was not created")
    elif tool.get("content") != content:
        print(f"Updating tool {function_id}", flush=True)
        http_json(
            "POST",
            f"/api/v1/tools/id/{function_id}/update",
            payload={
                "id": function_id,
                "name": tool.get("name") or name,
                "content": content,
                "meta": tool.get("meta") or {},
            },
            token=token,
        )
    else:
        print(f"Tool {function_id} already up to date", flush=True)


def main():
    wait_for_webui()
    token = signin()
    print("Signed in to Open WebUI", flush=True)

    for spec in load_specs():
        install_one(token, spec)
        print(f"Tool {spec['id']} is installed", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"webui-init failed: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
