"""E2E test: GPU handoff + Qwen-Image-2.1 generation + VLM description.

Runs inside the Open WebUI container (has aiohttp and network access to
llm:8080 and comfyui:8188). Exercises the exact code paths the Open WebUI
tools use: unload LLM -> generate image -> describe image -> free ComfyUI.
"""
import asyncio
import importlib.util
import json
import time


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


t2i = load_module("t2i", "/tmp/tool_t2i.py")
vlm = load_module("vlm", "/tmp/tool_vlm.py")

COMFYUI = "http://comfyui:8188"
LLMASWAP = "http://llm:8080"

PROMPT = (
    "Photorealistic portrait photograph of a beautiful woman in her late twenties with long "
    "wavy auburn hair and fair skin with light freckles, wearing an emerald green silk blouse, "
    "standing at a sunlit Parisian cafe terrace, soft golden hour light from the left, "
    "shallow depth of field, 85mm lens, warm cinematic color grade"
)

result = {"prompt": PROMPT, "steps": {}}


async def main():
    import aiohttp

    async with aiohttp.ClientSession() as session:
        # 1. GPU handoff: unload the chat LLM.
        t0 = time.time()
        ok = await t2i.unload_llama_swap(session, LLMASWAP, 180, None, 45)
        result["steps"]["llm_unloaded"] = {"ok": ok, "seconds": round(time.time() - t0, 1)}
        if not ok:
            result["error"] = "LLM unload failed"
            return

        # 2. Generate the image (refine=True exercises the fixed TextGenerate node).
        t0 = time.time()
        seed = 20260930
        workflow = t2i.build_workflow(
            prompt=PROMPT, width=896, height=1152, steps=20, cfg=1.0,
            seed=seed, refine=True, negative_prompt="",
        )
        client_id = "e2e-test"
        async with session.post(f"{COMFYUI}/prompt", json={"prompt": workflow, "client_id": client_id}) as resp:
            body = await resp.text()
            if resp.status != 200:
                result["error"] = f"ComfyUI rejected prompt ({resp.status}): {body[:500]}"
                return
            prompt_id = json.loads(body)["prompt_id"]
        ws_url = COMFYUI.replace("http://", "ws://") + "/ws"
        job = await t2i.wait_for_completion_ws(ws_url, COMFYUI, prompt_id, client_id, 900)
        files = t2i.extract_image_files(job)
        result["steps"]["generated"] = {
            "seconds": round(time.time() - t0, 1),
            "filename": files[0]["filename"] if files else None,
            "refined_prompt": (job.get("outputs", {}).get("6", {}).get("generated_text") or [""])[0][:400] if isinstance(job.get("outputs", {}).get("6", {}).get("generated_text"), list) else "",
        }
        if not files:
            result["error"] = "no image in job output"
            return

        # 3. VLM description of the generated image.
        t0 = time.time()
        view = f"{COMFYUI}/view?filename={files[0]['filename']}&type=output"
        async with session.get(view) as resp:
            if resp.status != 200:
                result["error"] = f"could not fetch generated image (HTTP {resp.status})"
                return
            image_bytes = await resp.read()
        form = aiohttp.FormData()
        form.add_field("image", image_bytes, filename=files[0]["filename"], content_type="image/png")
        form.add_field("overwrite", "true")
        async with session.post(f"{COMFYUI}/upload/image", data=form) as resp:
            if resp.status != 200:
                result["error"] = f"ComfyUI image upload failed (HTTP {resp.status})"
                return
            uploaded = (await resp.json()).get("name") or files[0]["filename"]
        wf2 = vlm.build_workflow(
            uploaded,
            "Describe this image in detail: the subject, appearance, clothing, pose, setting, lighting and style.",
        )
        client_id2 = "e2e-test-vlm"
        async with session.post(f"{COMFYUI}/prompt", json={"prompt": wf2, "client_id": client_id2}) as resp:
            body = await resp.text()
            if resp.status != 200:
                result["error"] = f"VLM prompt rejected ({resp.status}): {body[:500]}"
                return
            pid2 = json.loads(body)["prompt_id"]
        job2 = await vlm.wait_for_completion_ws(ws_url, COMFYUI, pid2, client_id2, 600)
        desc = vlm.extract_text_output(job2, "3")
        result["steps"]["described"] = {"seconds": round(time.time() - t0, 1)}
        result["vlm_description"] = desc or "(no text output)"

        # 4. Give the GPU back.
        t0 = time.time()
        await vlm.free_comfyui_gpu(session, COMFYUI)
        result["steps"]["comfyui_freed"] = {"seconds": round(time.time() - t0, 1)}


asyncio.run(main())
print(json.dumps(result, indent=2))
