"""
title: ComfyUI Qwen-Image-2.1 Text-to-Image
description: Generate photorealistic images (people, scenes, portraits) using ComfyUI with the uncensored Qwen-Image-2.1 model (Comfy-Org native int8 format). Optionally refines the prompt with the built-in prompt-enhancer before sampling. Talks to ComfyUI over HTTP+WebSocket and returns the finished image inline in the chat.
author: humanai
version: 1.0.0
required_open_webui_version: 0.9.1
license: MIT
"""

import asyncio
import io
import json
import logging
import random
import time
import uuid
import aiohttp

from typing import Any, Dict, Optional, Callable, Awaitable, List, Tuple, Union
from urllib.parse import quote
from pydantic import BaseModel, Field
from fastapi import UploadFile
from fastapi.responses import HTMLResponse
from open_webui.routers.files import upload_file_handler
from open_webui.models.users import Users

logger = logging.getLogger(__name__)

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

# Model filenames (must match the files under ComfyUI's models/ directory)
UNET_NAME = "qwen_image_2.1_int8_convrot.safetensors"
CLIP_NAME = "qwen3vl_8b_int8_convrot.safetensors"
VAE_NAME = "qwen_image_2.1_vae_bf16.safetensors"
PE_CLIP_NAME = "qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors"

# Prompt-enhancer system prompt (from the official Qwen-Image-2.1 T2I template).
# Turns a short brief into one dense, observer-style English paragraph.
SYSTEM_PROMPT = """# Image Prompt Rewriting Expert

You turn a user's image request into one long English paragraph that describes the
finished image as if you were looking at it. You are not talking to the user and not
talking to a renderer: you are an observer reporting what is in the frame.

Work through the steps below in order. Each step commits one decision; later steps
never revise an earlier one.

## Step 1 - Read the brief and split it in two
Fixed, and it must survive unchanged: every string of text they want shown, every named
object, every count, every stated colour, every stated position. Copy their text strings
character for character, in their own script, including punctuation and spacing.
Open, and you must decide it: everything they did not mention. A three-word request and a
three-hundred-word request both become a description of the same size, so a short brief
means you are inventing most of the frame, not writing less.

## Step 2 - Fix the frame
Decide the orientation from the subject. Default to a vertical 2:3 framing for portraits
and single standing subjects, horizontal 3:2 for wide scenes. Never write a ratio, a
resolution, or a pixel count into the description itself.

## Step 3 - Write the opening sentence
One sentence, around twenty words. Name the medium, the style, the subject, and the
background or palette; usually name the orientation too:
"The image is a <vertical / wide / square> <style> <photograph / portrait / scene / close-up> of <subject>, <the background and its palette>."
The medium noun is the one part that is never omitted. Name the style once here
(realistic, photorealistic, cinematic, editorial, watercolour, 3D-rendered, ...).

## Step 4 - Inventory before you write
Settle two lists. Every element that will appear, each with a place in the frame (upper-left,
across the top, on the far right, in the lower-third, in the centre, in front of, behind,
tucked into the corner) - eight to fourteen positional phrases, reaching the corners, the
edges and the centre. And every piece of legible text, in reading order.

## Step 5 - Walk the frame
If one subject fills the frame (a portrait, a close-up), walk the subject: the background
and how far it falls off, then the subject's pose and where they are placed, then head and
face, then body and each garment or surface, then what is held or touching them, then what
is left at the edges. Keep using positional phrases so the frame stays locatable. Roughly
a third of sentences should open on the positional phrase itself.

## Step 6 - Set every piece of text
Skip if nothing is meant to be read. Otherwise, for each string in reading order, name where
it sits, what it looks like, and what it says, in straight double quotes, in its own script.

## Step 7 - Give the lighting its own sentence
Every image has light in it: the source, its direction, its quality, and the shadows and
highlights it leaves. State it explicitly, e.g. "The lighting is soft diffused daylight from
a window on the left, ...".

## Step 8 - Close with the whole frame
End on a single sentence that steps back: "The overall composition is ...". Cover balance,
the palette, the style, and the mood in that one sentence.

## Throughout
The description runs about twenty sentences and four to five hundred words. Observe, don't
instruct: present tense, third person, declarative. No "you", no "create", no "make sure".
No quality boosters - no "masterpiece", "8K", "highly detailed", "award-winning". Hedge what
you cannot be certain of ("appears to be", "likely"). Name colours with a modifier, almost
never bare. Give the material, not just the noun. People get their observable surface: build,
posture, where they are looking, expression, hair, skin tone, and each garment with its colour
and material. Age is a life stage or a decade, never a number of years.

## Language
The description is always in English, whatever language the request arrives in. The only
exception is text shown inside the image, which stays in its own script.

## Output format
Return the description and nothing else: one continuous paragraph of English, in a single
line starting on the very first character of your reply. No JSON, no braces, no key names,
no labels, no headings, no markdown, no code fences, and no quotation marks around the
paragraph. No preamble, no acknowledgement, no explanation, no summary, no notes, no closing
remarks, no questions. No ratio, no resolution, or pixel count. Your whole reply is used as
the image prompt, so every extra word you add becomes part of the image."""


def build_workflow(
    prompt: str,
    width: int,
    height: int,
    steps: int,
    cfg: float,
    seed: int,
    refine: bool,
    negative_prompt: str,
) -> Dict[str, Any]:
    """Build the flat ComfyUI API workflow for Qwen-Image-2.1 text-to-image."""
    workflow: Dict[str, Any] = {
        "1": {
            "inputs": {"unet_name": UNET_NAME, "weight_dtype": "default"},
            "class_type": "UNETLoader",
            "_meta": {"title": "Load Diffusion Model"},
        },
        "2": {
            "inputs": {"model": ["1", 0], "device": "auto", "dtype": "default"},
            "class_type": "QwenImage21Cache",
            "_meta": {"title": "Qwen Image 2.1 Cache"},
        },
        "3": {
            "inputs": {
                "clip_name": CLIP_NAME,
                "type": "qwen_image",
                "device": "default",
            },
            "class_type": "CLIPLoader",
            "_meta": {"title": "Load CLIP (main)"},
        },
        "5": {
            "inputs": {"vae_name": VAE_NAME},
            "class_type": "VAELoader",
            "_meta": {"title": "Load VAE"},
        },
        "7": {
            "inputs": {"width": width, "height": height, "batch_size": 1},
            "class_type": "EmptyLatentImage",
            "_meta": {"title": "Empty Latent"},
        },
        "9": {
            "inputs": {
                "model": ["2", 0],
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": "euler",
                "scheduler": "simple",
                "positive": ["8", 0],
                "negative": ["8", 1],
                "latent_image": ["7", 0],
                "denoise": 1.0,
            },
            "class_type": "KSampler",
            "_meta": {"title": "KSampler"},
        },
        "10": {
            "inputs": {"samples": ["9", 0], "vae": ["5", 0]},
            "class_type": "VAEDecode",
            "_meta": {"title": "VAE Decode"},
        },
        "11": {
            "inputs": {"filename_prefix": "qwen21", "images": ["10", 0]},
            "class_type": "SaveImage",
            "_meta": {"title": "Save Image"},
        },
    }

    if refine:
        workflow["4"] = {
            "inputs": {
                "clip_name": PE_CLIP_NAME,
                "type": "stable_diffusion",
                "device": "default",
            },
            "class_type": "CLIPLoader",
            "_meta": {"title": "Load CLIP (prompt enhancer)"},
        }
        # ComfyUI 0.38 V3 nodes: sampling_mode is a DynamicCombo. The API value of
        # `sampling_mode` is the option key ("on"/"off"); the option's own inputs are
        # addressed as flat dotted keys (sampling_mode.temperature, ...). The framework
        # re-nests them into a dict before calling execute().
        workflow["6"] = {
            "inputs": {
                "clip": ["4", 0],
                "prompt": prompt,
                "max_length": 4096,
                "sampling_mode": "on",
                "sampling_mode.temperature": 1.0,
                "sampling_mode.top_k": 20,
                "sampling_mode.top_p": 0.95,
                "sampling_mode.min_p": 0.0,
                "sampling_mode.seed": seed,
                "sampling_mode.repetition_penalty": 1.05,
                "sampling_mode.presence_penalty": 0.0,
                "thinking": True,
                "use_default_template": True,
                "mtp": "auto",
                "system_prompt": SYSTEM_PROMPT,
            },
            "class_type": "TextGenerate",
            "_meta": {"title": "Refine Prompt"},
        }
        refined_prompt: Union[str, List] = ["6", 0]
    else:
        refined_prompt = prompt

    workflow["8"] = {
        "inputs": {
            "clip": ["3", 0],
            "prompt": refined_prompt,
            "negative_prompt": negative_prompt,
            "resolution": 1024,
        },
        "class_type": "TextEncodeQwenImage21",
        "_meta": {"title": "Text Encode Qwen Image 2.1"},
    }
    return workflow


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
                if msg_type == "executing" and data.get("prompt_id") == prompt_id:
                    node = data.get("node")
                    if node is None and data.get("done"):
                        if __event_emitter_ref__ is not None:
                            await __event_emitter_ref__(
                                {"type": "status", "data": {"description": "Sampling complete, decoding...", "done": False}}
                            )
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


def extract_image_files(job_data: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract image file info from a ComfyUI job's outputs."""
    image_files: List[Dict[str, str]] = []
    for node_output in job_data.get("outputs", {}).values():
        if not isinstance(node_output, dict):
            continue
        for key in ("images", "files", "output"):
            if key in node_output and isinstance(node_output[key], list):
                for file_info in node_output[key]:
                    if isinstance(file_info, dict):
                        filename = file_info.get("filename")
                        subfolder = file_info.get("subfolder", "")
                        if filename and filename.lower().endswith(IMAGE_EXTS):
                            image_files.append({"filename": filename, "subfolder": subfolder})
    return image_files


async def download_and_upload_to_owui(
    comfyui_http_url: str,
    filename: str,
    subfolder: str,
    request: Any,
    user: Any,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[str, bool]:
    """Download the image from ComfyUI and upload it into Open WebUI."""
    subfolder_param = f"&subfolder={quote(subfolder)}" if subfolder else ""
    comfyui_view_url = (
        f"{comfyui_http_url}/view?filename={quote(filename)}&type=output{subfolder_param}"
    )
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(comfyui_view_url) as response:
                if response.status != 200:
                    return comfyui_view_url, False
                content = await response.read()
        if request and user:
            file = UploadFile(file=io.BytesIO(content), filename=filename)
            file_item = await upload_file_handler(
                request=request, file=file, metadata={}, process=False, user=user
            )
            file_id = getattr(file_item, "id", None)
            if file_id:
                return f"/api/v1/files/{file_id}/content", True
    except Exception as e:
        logger.debug("Failed to download or upload image: %s", e)
    return comfyui_view_url, False


# Module-level reference so the WS waiter can emit progress without threading the
# emitter through every helper. Set on each tool invocation.
__event_emitter_ref__: Optional[Callable[[Any], Awaitable[None]]] = None


async def unload_llama_swap(
    session: aiohttp.ClientSession,
    base_url: str,
    timeout_s: int,
    emitter: Optional[Callable[[Any], Awaitable[None]]] = None,
    settle_seconds: int = 30,
) -> bool:
    """Ask llama-swap to unload the chat LLM, then wait until the GPU is free.

    llama-swap's /running bookkeeping clears before the llama-server process has
    actually released its VRAM. If ComfyUI loads into still-occupied memory the
    GPU faults, so after the model reports unloaded we also wait a settle period.
    """
    base_url = base_url.rstrip("/")
    if emitter:
        await emitter(
            {
                "type": "status",
                "data": {
                    "description": "Unloading chat LLM from GPU before image generation...",
                    "done": False,
                },
            }
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
                                {
                                    "type": "status",
                                    "data": {
                                        "description": "LLM unloaded; waiting for GPU memory to be released...",
                                        "done": False,
                                    },
                                }
                            )
                        await asyncio.sleep(max(0, settle_seconds))
                        if emitter:
                            await emitter(
                                {
                                    "type": "status",
                                    "data": {
                                        "description": "GPU is free for ComfyUI.",
                                        "done": False,
                                    },
                                }
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
            {
                "type": "status",
                "data": {
                    "description": "Freeing ComfyUI GPU memory for the chat LLM...",
                    "done": False,
                },
            }
        )
    try:
        async with session.post(
            f"{base_url}/free",
            json={"unload_models": True, "free_memory": True},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            await resp.text()
        # ComfyUI's prompt worker checks the flag on its next loop iteration.
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
            description="llama-swap endpoint used to unload the chat LLM before generation.",
        )
        unload_llm_before_generate: bool = Field(
            default=True,
            description="Unload the chat LLM from the GPU before ComfyUI loads its model.",
        )
        free_gpu_after_generate: bool = Field(
            default=True,
            description="Ask ComfyUI to unload its model after generation so chat can resume.",
        )
        gpu_handoff_timeout: int = Field(
            default=180,
            description="Max seconds to wait for the GPU handoff between LLM and ComfyUI.",
        )
        gpu_settle_seconds: int = Field(
            default=30,
            description="Extra seconds to wait after the LLM reports unloaded, for its VRAM to actually be released before ComfyUI loads.",
        )
        max_wait_time: int = Field(
            default=900, description="Max seconds to wait for a generation job."
        )

    def __init__(self):
        self.valves = self.Valves()

    async def generate_image(
        self,
        prompt: str,
        width: int = 1024,
        height: int = 1024,
        steps: int = 25,
        seed: int = -1,
        refine: bool = True,
        negative_prompt: str = "",
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __request__: Optional[Any] = None,
    ) -> Union[str, Tuple[HTMLResponse, str]]:
        """
        Generate a photorealistic image from a text description using ComfyUI + Qwen-Image-2.1.

        Use this whenever the user asks to create, draw, render, or imagine a picture,
        portrait, person, character, or scene. Describe the subject richly in `prompt`:
        appearance (age as a life stage, build, hair, skin tone, expression, gaze),
        clothing (each garment with colour and material), pose, setting/background,
        lighting, and photographic style (lens, framing, mood).

        :param prompt: Detailed description of the person or scene to generate.
        :param width: Image width in pixels (multiple of 32). Default 1024.
        :param height: Image height in pixels (multiple of 32). Default 1024. Use height>width for portraits.
        :param steps: Sampling steps. Higher is slower but can be cleaner. Default 25.
        :param seed: Random seed. Use -1 for a random seed (default). Fix a value to reproduce a result.
        :param refine: Enhance the prompt with the built-in prompt-rewriter before sampling. Default true.
        :param negative_prompt: Optional things to avoid in the image.
        :return: The generated image, shown inline in the chat.
        """
        global __event_emitter_ref__
        __event_emitter_ref__ = __event_emitter__
        comfyui_http_url = self.valves.comfyui_api_url.rstrip("/")
        comfyui_headers = {}
        if self.valves.comfyui_api_key:
            comfyui_headers["Authorization"] = f"Bearer {self.valves.comfyui_api_key}"
        comfyui_submitted = False
        gpu_freed = False
        try:
            if seed is None or seed < 0:
                seed = random.randint(0, 2**32 - 1)
            # Keep dimensions sane and aligned to the VAE's 32-pixel grid.
            width = max(256, min(2048, int(round(width / 32)) * 32))
            height = max(256, min(2048, int(round(height / 32)) * 32))

            workflow = build_workflow(
                prompt=prompt,
                width=width,
                height=height,
                steps=steps,
                cfg=1.0,
                seed=seed,
                refine=refine,
                negative_prompt=negative_prompt,
            )

            client_id = str(uuid.uuid4())
            comfyui_ws_url = (
                comfyui_http_url.replace("http://", "ws://").replace("https://", "wss://")
                + "/ws"
            )

            if self.valves.unload_llm_before_generate and self.valves.llama_swap_url:
                async with aiohttp.ClientSession() as control_session:
                    await unload_llama_swap(
                        control_session,
                        self.valves.llama_swap_url,
                        self.valves.gpu_handoff_timeout,
                        __event_emitter__,
                        self.valves.gpu_settle_seconds,
                    )

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "Submitting to ComfyUI (Qwen-Image-2.1)...",
                            "done": False,
                        },
                    }
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

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {"description": "Generating image (this can take a minute or two)...", "done": False},
                    }
                )

            job_data = await wait_for_completion_ws(
                comfyui_ws_url,
                comfyui_http_url,
                prompt_id,
                client_id,
                self.valves.max_wait_time,
                comfyui_headers,
            )

            image_files = extract_image_files(job_data)
            if not image_files:
                raise Exception("No images found in ComfyUI output")

            user = await Users.get_user_by_id(__user__["id"]) if __user__ else None
            image_url, _uploaded = await download_and_upload_to_owui(
                comfyui_http_url,
                image_files[0]["filename"],
                image_files[0]["subfolder"],
                __request__,
                user,
                comfyui_headers,
            )

            if self.valves.free_gpu_after_generate:
                async with aiohttp.ClientSession(headers=comfyui_headers) as control_session:
                    await free_comfyui_gpu(
                        control_session,
                        comfyui_http_url,
                        __event_emitter__,
                    )
                gpu_freed = True

            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": "Image generation complete!", "done": True}}
                )

            html_content = f"""<!DOCTYPE html>
<html style="margin:0; padding:0; overflow:hidden;">
<head>
<meta charset="UTF-8">
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ margin:0; padding:0; overflow:hidden; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; }}
.image-wrapper {{ margin:0 0 8px 0; border-radius:12px; overflow:hidden; line-height:0; }}
.image-wrapper img {{ max-width:100%; height:auto; display:block; }}
.meta {{ background:rgba(0,0,0,0.05); border:1px solid rgba(0,0,0,0.1); border-radius:12px; padding:10px 14px; margin-top:8px; font-size:12px; line-height:1.5; color:#444; word-wrap:break-word; }}
.meta b {{ color:#666; font-size:11px; text-transform:uppercase; letter-spacing:0.5px; }}
</style>
</head>
<body>
<div class="image-wrapper"><img src="{image_url}" alt="Generated image" /></div>
<div class="meta"><b>Qwen-Image-2.1</b> &middot; {width}&times;{height} &middot; {steps} steps &middot; seed {seed} &middot; refine {str(refine).lower()}</div>
</body>
</html>"""
            return (
                HTMLResponse(
                    content=html_content,
                    media_type="text/html",
                    headers={"content-disposition": "inline"},
                ),
                f"Image generated and shown above. {width}x{height}, seed {seed}. "
                f"ComfyUI output file: {image_files[0]['filename']} (use it as the filename argument of describe_image to inspect the result).",
            )
        except Exception as e:
            logger.error("Error generating image: %s", e)
            if comfyui_submitted and self.valves.free_gpu_after_generate and not gpu_freed:
                try:
                    async with aiohttp.ClientSession(headers=comfyui_headers) as control_session:
                        await free_comfyui_gpu(
                            control_session,
                            comfyui_http_url,
                            __event_emitter__,
                        )
                except Exception:
                    pass
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": f"Error: {str(e)}", "done": True}}
                )
            return f"Error generating image: {str(e)}"
