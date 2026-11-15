# syntax=docker/dockerfile:1.7

# 必须基于 Vast 官方基础镜像：SSH 注入、Supervisor、Instance Portal、日志都在它的启动层里。
# 换成纯净镜像（python:slim）虽然容器能跑，但 Vast 无法登入实例、日志也看不到。
FROM vastai/base-image:cuda-12.8.1-cudnn-devel-ubuntu22.04-py312

ARG COMFY_COMMIT=fd71054
ARG TORCH_VERSION=2.9.1
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu128

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    COMFY_DIR=/opt/ComfyUI \
    MOSS_MODEL_DIR=/models

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# torch 锁 cu128，单独成层
RUN . /venv/main/bin/activate \
    && pip install --index-url "${TORCH_INDEX}" "torch==${TORCH_VERSION}" torchvision

# ComfyUI 固定在验证过的提交
RUN git clone https://github.com/comfyanonymous/ComfyUI.git "${COMFY_DIR}" \
    && cd "${COMFY_DIR}" \
    && git checkout "${COMFY_COMMIT}" \
    && git submodule update --init --recursive

# ComfyUI 的 requirements 里也写着 torch，剔掉免得覆盖刚才锁好的 cu128
RUN . /venv/main/bin/activate \
    && grep -viE '^(torch|torchvision|torchaudio)([=<>! ]|$)' "${COMFY_DIR}/requirements.txt" \
        > /tmp/requirements-comfyui.txt \
    && pip install -r /tmp/requirements-comfyui.txt

# 模型放 /models（不在 workspace 里，避免首次启动被复制一遍）
RUN mkdir -p "${MOSS_MODEL_DIR}"/{diffusion_models,text_encoders,vae,loras} \
    && cat > "${COMFY_DIR}/extra_model_paths.yaml" <<'YAML'
moss:
  base_path: /models
  diffusion_models: diffusion_models
  text_encoders: text_encoders
  vae: vae
  loras: loras
YAML

# 应用
COPY app/ /opt/moss/app/
COPY scripts/ /opt/moss/scripts/
COPY config/ /opt/moss/config/
RUN . /venv/main/bin/activate \
    && pip install -r /opt/moss/app/requirements.txt \
    && chmod +x /opt/moss/scripts/*.sh

# Vast 的 entrypoint 优先执行 boot_custom.sh（存在就跳过默认启动），
# 我们借它在 SSH 模式下也能把业务进程拉起来，然后把控制权交回默认启动流程。
COPY scripts/boot_custom.sh /opt/instance-tools/bin/boot_custom.sh
RUN chmod +x /opt/instance-tools/bin/boot_custom.sh

# 面板里能看到我们的服务（端口没 publish 时只是不显示，不影响运行）
ENV PORTAL_CONFIG="localhost:1111:11111:/:Instance Portal|localhost:8000:18000:/:MOSS Image API"

HEALTHCHECK --interval=30s --timeout=10s --start-period=900s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1
