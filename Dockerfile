# syntax=docker/dockerfile:1.7

# 用 python:slim 而不是 CUDA 官方镜像：torch 的 cu128 wheel 自带 CUDA 运行库，
# 不用赌某个 nvidia/cuda 标签还在不在，镜像也小一圈。
FROM python:3.12-slim

# 本地已验证可跑 MOSS 生图 + 编辑节点的提交（2026-11-07 master）
ARG COMFY_COMMIT=fd71054
ARG TORCH_VERSION=2.9.1
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu128

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    COMFY_DIR=/opt/ComfyUI \
    MOSS_MODEL_DIR=/models

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates libgl1 libglib2.0-0 tini \
    && rm -rf /var/lib/apt/lists/*

# torch 先装，锁版本并单独成层，改应用代码不必重装
RUN pip install --index-url "${TORCH_INDEX}" \
        "torch==${TORCH_VERSION}" torchvision

# ComfyUI 固定在验证过的提交上，不跟 master 漂移
RUN git clone https://github.com/comfyanonymous/ComfyUI.git "${COMFY_DIR}" \
    && cd "${COMFY_DIR}" \
    && git checkout "${COMFY_COMMIT}" \
    && git submodule update --init --recursive

COPY config/requirements-comfyui-extra.txt /tmp/requirements-comfyui-extra.txt
# ComfyUI 的 requirements.txt 里也写着 torch/torchvision，直接装会把上面锁好的
# cu128 版本覆盖成 PyPI 的另一个 CUDA 构建，这里把它们剔掉再装其余依赖。
RUN grep -viE '^(torch|torchvision|torchaudio)([=<>! ]|$)' "${COMFY_DIR}/requirements.txt" \
        > /tmp/requirements-comfyui.txt \
    && pip install -r /tmp/requirements-comfyui.txt \
    && pip install -r /tmp/requirements-comfyui-extra.txt

# 模型目录外挂到 /models，容器重建不丢权重
RUN mkdir -p "${MOSS_MODEL_DIR}"/{diffusion_models,text_encoders,vae,loras} \
    && cat > "${COMFY_DIR}/extra_model_paths.yaml" <<'YAML'
moss:
  base_path: /models
  diffusion_models: diffusion_models
  text_encoders: text_encoders
  vae: vae
  loras: loras
YAML

WORKDIR /app
COPY app/ /app/
RUN pip install -r /app/requirements.txt

COPY config/models.json /app/config/models.json
COPY scripts/ /app/scripts/
RUN chmod +x /app/scripts/*.sh

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=600s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/scripts/entrypoint.sh"]
