"""Standalone format test: POST the Qwen-Image-2.1 workflow to ComfyUI /prompt and
report whether validation accepts it (200) or rejects it (400). Self-contained so it
runs on the host without open_webui."""
import json
import sys
import urllib.request

COMFYUI = "http://localhost:8188"

UNET_NAME = "qwen_image_2.1_int8_convrot.safetensors"
CLIP_NAME = "qwen3vl_8b_int8_convrot.safetensors"
VAE_NAME = "qwen_image_2.1_vae_bf16.safetensors"
PE_CLIP_NAME = "qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors"

PROMPT = "a red cube on a table"
SEED = 12345
WIDTH, HEIGHT, STEPS = 512, 512, 4  # tiny/fast for the format test


def build_workflow():
    wf = {
        "1": {"inputs": {"unet_name": UNET_NAME, "weight_dtype": "default"}, "class_type": "UNETLoader"},
        "2": {"inputs": {"model": ["1", 0], "device": "auto", "dtype": "default"}, "class_type": "QwenImage21Cache"},
        "3": {"inputs": {"clip_name": CLIP_NAME, "type": "qwen_image", "device": "default"}, "class_type": "CLIPLoader"},
        "4": {"inputs": {"clip_name": PE_CLIP_NAME, "type": "stable_diffusion", "device": "default"}, "class_type": "CLIPLoader"},
        "5": {"inputs": {"vae_name": VAE_NAME}, "class_type": "VAELoader"},
        "6": {
            "inputs": {
                "clip": ["4", 0],
                "prompt": PROMPT,
                "max_length": 4096,
                "sampling_mode": "on",
                "sampling_mode.temperature": 1.0,
                "sampling_mode.top_k": 20,
                "sampling_mode.top_p": 0.95,
                "sampling_mode.min_p": 0.0,
                "sampling_mode.seed": SEED,
                "sampling_mode.repetition_penalty": 1.05,
                "sampling_mode.presence_penalty": 0.0,
                "thinking": True,
                "use_default_template": True,
                "mtp": "auto",
            },
            "class_type": "TextGenerate",
        },
        "7": {"inputs": {"width": WIDTH, "height": HEIGHT, "batch_size": 1}, "class_type": "EmptyLatentImage"},
        "8": {
            "inputs": {"clip": ["3", 0], "prompt": ["6", 0], "negative_prompt": "", "resolution": 1024},
            "class_type": "TextEncodeQwenImage21",
        },
        "9": {
            "inputs": {
                "model": ["2", 0], "seed": SEED, "steps": STEPS, "cfg": 1.0,
                "sampler_name": "euler", "scheduler": "simple",
                "positive": ["8", 0], "negative": ["8", 1], "latent_image": ["7", 0], "denoise": 1.0,
            },
            "class_type": "KSampler",
        },
        "10": {"inputs": {"samples": ["9", 0], "vae": ["5", 0]}, "class_type": "VAEDecode"},
        "11": {"inputs": {"filename_prefix": "fmt_test", "images": ["10", 0]}, "class_type": "SaveImage"},
    }
    return wf


def main():
    payload = json.dumps({"prompt": build_workflow(), "client_id": "format_test"}).encode()
    req = urllib.request.Request(COMFYUI + "/prompt", data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            print("STATUS:", resp.status)
            print("BODY:", body[:800])
            sys.exit(0 if resp.status == 200 else 1)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print("STATUS:", e.code)
        print("BODY:", body[:1500])
        sys.exit(1)


if __name__ == "__main__":
    main()
