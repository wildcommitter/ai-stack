"""
title: ComfyUI Image Description (VLM)
description: Inspect/describe a generated image with the Qwen3.5-9B vision-language model running inside ComfyUI (shares the GPU with the image models). Unloads the chat LLM from the GPU first and frees ComfyUI afterwards, so the two never collide on VRAM.
author: humanai
version: 1.0.0
required_open_webui_version: 0.9.1
license: MIT
"""

import asyncio
import json
import logging
import time
import uuid
import aiohttp

from typing import Any, Dict, Optional, Callable, Awaitable
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

PE_CLIP_NAME = "qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors"

SYSTEM_PROMPT = (
    "You are a precise image captioning assistant. Describe only what is visible in the "
    "image: the subject(s), their appearance (build, hair, skin tone, expression, gaze), "
    "clothing (each garment with colour and material), pose, setting/background, lighting, "
    "colours, photographic style and mood. Be specific and concrete. Answer in one or two "
    "short paragraphs, in the language of the question."
)


def build_workflow(image_name: str, question: str) -> Dict[str, Any]:
    """Build the flat ComfyUI API workflow that runs the VLM over an image."""
    return {
        "1": {
            "inputs": {
                "clip_name": PE_CLIP_NAME,
                "type": "stable_diffusion",
                "device": "default",
            },
            "class_type": "CLIPLoader",
            "_meta": {"title": "Load VLM"},
        },
        "2": {
            "inputs": {"image": image_name},
            "class_type": "LoadImage",
            "_meta": {"title": "Load Image"},
        },
        "3": {
            "inputs": {
                "clip": ["1", 0],
                "image": ["2", 0],
                "prompt": question,
                "max_length": 512,
                # ComfyUI 0.38 V3 node: DynamicCombo value is the option key.
                "sampling_mode": "off",
                "thinking": False,
                "use_default_template": True,
                "system_prompt": SYSTEM_PROMPT,
            },
            "class_type": "TextGenerate",
            "_meta": {"title": "Describe Image"},
        },
    }


async def wait_for_completion_ws(
    comfyui_ws_url: str,
    comfyui_http_url: str,
    prompt_id: str,
    client_id: str,
    max_wait_time: int,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Wait for ComfyUI job completion via WebSocket, then fetch its history."""
    start_time = time.monotonic()
    async with aiohttp.ClientSession(headers=headers) as ws_session:
        async with ws_session.ws_connect(f"{comfyui_ws_url}?clientId={client_id}") as ws:
            async for msg in ws:
                if time.monotonic() - start_time > max_wait_time:
                    raise TimeoutError(f"Job timed out after {max_wait_time}s")
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        break
                    continue
                try:
                    message = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                msg_type = message.get("type")
                data = message.get("data", {})
                if msg_type == "executed" and data.get("prompt_id") == prompt_id:
                    async with ws_session.get(f"{comfyui_http_url}/history/{prompt_id}") as resp:
                        if resp.status == 200:
                            history = await resp.json()
                            if prompt_id in history:
                                return history[prompt_id]
                    raise Exception("Job completed but couldn't retrieve history")
                if msg_type == "execution_error" and data.get("prompt_id") == prompt_id:
                    raise Exception(
                        "ComfyUI job failed on node "
                        f"{data.get('node_id', 'N/A')}: {data.get('exception_message', 'Unknown error')}"
                    )
    raise TimeoutError("WebSocket connection closed before job completion.")


def extract_text_output(job_data: Dict[str, Any], node_id: str) -> str:
    """Pull the generated text out of a ComfyUI job's history entry."""
    outputs = job_data.get("outputs", {}).get(node_id, {})
    for key in ("generated_text", "text", "output"):
        if key in outputs:
            value = outputs[key]
            if isinstance(value, list):
                joined = " ".join(str(v) for v in value if v)
                if joined.strip():
                    return joined
            elif value:
                return str(value)
    for value in outputs.values():
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value and isinstance(value[0], str):
            return " ".join(str(v) for v in value if v)
    return ""


async def unload_llama_swap(
    session: aiohttp.ClientSession,
    base_url: str,
    timeout_s: int,
    emitter: Optional[Callable[[Any], Awaitable[None]]] = None,
) -> bool:
    """Ask llama-swap to unload the chat LLM, then wait until the GPU is free."""
    base_url = base_url.rstrip("/")
    if emitter:
        await emitter(
            {"type": "status", "data": {"description": "Unloading chat LLM from GPU before image analysis...", "done": False}}
        )
    try:
        async with session.post(
            f"{base_url}/api/models/unload",
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            await resp.text()
    except Exception as e:
        logger.warning("llama-swap unload request failed: %s", e)
        return False

    deadline = time.monotonic() + max(10, timeout_s)
    while time.monotonic() < deadline:
        try:
            async with session.get(
                f"{base_url}/running",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if not data.get("running"):
                        if emitter:
                            await emitter(
                                {"type": "status", "data": {"description": "GPU is free for ComfyUI.", "done": False}}
                            )
                        return True
        except Exception:
            pass
        await asyncio.sleep(2)
    logger.warning("Timed out waiting for llama-swap to unload the LLM")
    return False


async def free_comfyui_gpu(
    session: aiohttp.ClientSession,
    base_url: str,
    emitter: Optional[Callable[[Any], Awaitable[None]]] = None,
) -> None:
    """Tell ComfyUI to unload its models so the chat LLM can use the GPU again."""
    base_url = base_url.rstrip("/")
    if emitter:
        await emitter(
            {"type": "status", "data": {"description": "Freeing ComfyUI GPU memory for the chat LLM...", "done": False}}
        )
    try:
        async with session.post(
            f"{base_url}/free",
            json={"unload_models": True, "free_memory": True},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            await resp.text()
        await asyncio.sleep(12)
    except Exception as e:
        logger.warning("ComfyUI /free request failed: %s", e)


class Tools:
    class Valves(BaseModel):
        comfyui_api_url: str = Field(
            default="http://comfyui:8188",
            description="ComfyUI HTTP API endpoint (sibling container in this stack).",
        )
        comfyui_api_key: str = Field(
            default="",
            description="Optional Bearer token for ComfyUI auth. Leave empty if not required.",
            json_schema_extra={"input": {"type": "password"}},
        )
        llama_swap_url: str = Field(
            default="http://llm:8080",
            description="llama-swap endpoint used to unload the chat LLM before analysis.",
        )
        unload_llm_before: bool = Field(
            default=True,
            description="Unload the chat LLM from the GPU before ComfyUI loads the VLM.",
        )
        free_gpu_after: bool = Field(
            default=True,
            description="Ask ComfyUI to unload its model afterwards so chat can resume.",
        )
        gpu_handoff_timeout: int = Field(
            default=180,
            description="Max seconds to wait for the GPU handoff between LLM and ComfyUI.",
        )
        max_wait_time: int = Field(
            default=300, description="Max seconds to wait for the analysis job."
        )

    def __init__(self):
        self.valves = self.Valves()

    async def describe_image(
        self,
        filename: str,
        question: str = "Describe this image in detail: the subject, appearance, clothing, pose, setting, lighting and style.",
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> str:
        """
        Look at a previously generated image and describe what is actually in it, using a
        vision-language model. Use this to verify that a generated image matches the request,
        or to answer questions about it ("what is she wearing?", "is there text in the image?").

        :param filename: The ComfyUI output filename of the image to inspect (e.g. "qwen21_00004_.png"), as reported by the image generation tool.
        :param question: What to look for or report about the image.
        :return: The VLM's description/answer about the image.
        """
        comfyui_http_url = self.valves.comfyui_api_url.rstrip("/")
        comfyui_headers = {}
        if self.valves.comfyui_api_key:
            comfyui_headers["Authorization"] = f"Bearer {self.valves.comfyui_api_key}"
        comfyui_submitted = False
        gpu_freed = False
        try:
            # 1. Hand the GPU over to ComfyUI.
            if self.valves.unload_llm_before and self.valves.llama_swap_url:
                async with aiohttp.ClientSession() as control_session:
                    await unload_llama_swap(
                        control_session,
                        self.valves.llama_swap_url,
                        self.valves.gpu_handoff_timeout,
                        __event_emitter__,
                    )

            # 2. Fetch the image from ComfyUI's output directory.
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": f"Fetching {filename} from ComfyUI...", "done": False}}
                )
            view_url = f"{comfyui_http_url}/view?filename={filename}&type=output"
            async with aiohttp.ClientSession(headers=comfyui_headers) as session:
                async with session.get(view_url) as resp:
                    if resp.status != 200:
                        raise Exception(f"Could not fetch {filename} from ComfyUI (HTTP {resp.status})")
                    image_bytes = await resp.read()

            # 3. Upload it into ComfyUI's input directory for the LoadImage node.
            form = aiohttp.FormData()
            form.add_field("image", image_bytes, filename=filename, content_type="image/png")
            form.add_field("overwrite", "true")
            async with aiohttp.ClientSession(headers=comfyui_headers) as session:
                async with session.post(f"{comfyui_http_url}/upload/image", data=form) as resp:
                    if resp.status != 200:
                        raise Exception(f"ComfyUI image upload failed (HTTP {resp.status}): {(await resp.text())[:300]}")
                    upload_result = await resp.json()
            uploaded_name = upload_result.get("name") or filename

            # 4. Run the VLM.
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": "Analysing image with the VLM (Qwen3.5-9B)...", "done": False}}
                )
            workflow = build_workflow(uploaded_name, question)
            client_id = str(uuid.uuid4())
            comfyui_ws_url = (
                comfyui_http_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
            )
            payload = {"prompt": workflow, "client_id": client_id}
            async with aiohttp.ClientSession(headers=comfyui_headers) as session:
                async with session.post(f"{comfyui_http_url}/prompt", json=payload) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        raise Exception(f"ComfyUI rejected the prompt ({resp.status}): {body[:500]}")
                    result = json.loads(body)
                    prompt_id = result.get("prompt_id")
                    if not prompt_id:
                        raise Exception(f"No prompt_id from ComfyUI: {body[:500]}")
                    comfyui_submitted = True

            job_data = await wait_for_completion_ws(
                comfyui_ws_url,
                comfyui_http_url,
                prompt_id,
                client_id,
                self.valves.max_wait_time,
                comfyui_headers,
            )

            description = extract_text_output(job_data, "3")
            if not description:
                raise Exception(f"No text output from the VLM job. History outputs: {json.dumps(job_data.get('outputs', {}))[:500]}")

            # 5. Give the GPU back.
            if self.valves.free_gpu_after:
                async with aiohttp.ClientSession(headers=comfyui_headers) as control_session:
                    await free_comfyui_gpu(control_session, comfyui_http_url, __event_emitter__)
                gpu_freed = True

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": "Image analysis complete.", "done": True}}
                )
            return description
        except Exception as e:
            logger.error("Error describing image: %s", e)
            if comfyui_submitted and self.valves.free_gpu_after and not gpu_freed:
                try:
                    async with aiohttp.ClientSession(headers=comfyui_headers) as control_session:
                        await free_comfyui_gpu(control_session, comfyui_http_url, __event_emitter__)
                except Exception:
                    pass
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": f"Error: {str(e)}", "done": True}}
                )
            return f"Error describing image: {str(e)}"
