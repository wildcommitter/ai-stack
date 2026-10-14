# ComfyUI on AMD ROCm (RDNA4 / gfx1201)
# PyTorch ROCm 7.2 wheels bundle the HIP runtime + hipBLAS/MIOpen, so a slim
# base is enough. Matches the host's proven ROCm 7.2.1 stack (see llamacpp-rocm).
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # RDNA4 (gfx1201) is natively supported by ROCm 7.x; no override needed.
    HSA_ENABLE_SDMA=1

# System libs required by the ROCm torch wheel + ComfyUI deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
        libgomp1 libnuma1 libelf1 \
        libgl1 libglib2.0-0 \
        libgfortran5 \
    && rm -rf /var/lib/apt/lists/*

# PyTorch with ROCm 7.2 (cp312). Install first so `pip install -r requirements.txt`
# sees torch/torchvision as already satisfied and does not pull the CUDA build.
RUN pip install \
        torch==2.14.0 \
        torchvision \
    --index-url https://download.pytorch.org/whl/rocm7.2

# Runtime dependency of the ROCm torch wheel; installed after torch so the
# large torch layer remains cacheable.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libatomic1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ComfyUI (canonical repo moved to Comfy-Org). Pin main for Qwen-Image-2.1 node support.
RUN git clone --depth 1 https://github.com/Comfy-Org/ComfyUI.git .

# torch/torchvision already present; everything else from PyPI.
RUN pip install -r requirements.txt

EXPOSE 8188

# --lowvram is intentionally NOT set: 32GB VRAM keeps the int8 DiT resident.
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188"]
