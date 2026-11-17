"""MOSS-Image OpenAI 兼容服务。

一个进程同时提供：
  POST /v1/images/generations   文生图
  POST /v1/images/edits         指令改图（单图或多参考图）
  GET  /v1/models
  GET  /healthz

后端是本地 ComfyUI（HTTP 接口）。为了在 24GB 卡上稳定跑，
GPU 一次只做一个任务，其余在进程内排队；客户端断开时把 ComfyUI 里的任务撤掉。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import traceback
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ------------------------------------------------------------------ 配置

API_KEY = os.environ.get("MOSS_API_KEY", "").strip()
COMFY_URL = os.environ.get("MOSS_COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
COMFY_INPUT = Path(os.environ.get("MOSS_COMFY_INPUT", "/opt/ComfyUI/input"))
LORA_DIR = Path(os.environ.get("MOSS_LORA_DIR", "/models/loras"))

MODEL_ID = os.environ.get("MOSS_MODEL_ID", "gpt-image-moss")
MODEL_ALIASES = [
    item.strip()
    for item in os.environ.get("MOSS_MODEL_ALIASES", "moss-image").split(",")
    if item.strip()
]

MODEL_UNET = os.environ.get("MOSS_UNET", "moss_image_sft_int8_convrot.safetensors")
MODEL_TE = os.environ.get("MOSS_TE", "qwen3.5_9b_int8_convrot.safetensors")
MODEL_VAE = os.environ.get("MOSS_VAE", "flux2-vae_small.safetensors")
MODEL_DMD = os.environ.get("MOSS_DMD", "moss_8step_dmd.safetensors")

# 改图 LoRA：key -> (文件名, ComfyUI 的 edit_format)
EDIT_VARIANTS = {
    "armv5": (
        os.environ.get("MOSS_EDIT_ARM", "moss_image_edit_hq_armV5.safetensors"),
        "ai-toolkit",
    ),
    "moss": (os.environ.get("MOSS_EDIT", "moss_image_edit_lora_bf16.safetensors"), "Moss-Image-Edit"),
}
DEFAULT_EDIT_VARIANT = os.environ.get("MOSS_EDIT_DEFAULT", "auto")

# quality 档位 -> 采样步数。四个档位都 >= DMD_STEP_LIMIT，一律走基底模型 + 真 CFG。
QUALITY_STEPS = {
    "low": int(os.environ.get("MOSS_STEPS_LOW", "30")),
    "medium": int(os.environ.get("MOSS_STEPS_MEDIUM", "30")),
    "high": int(os.environ.get("MOSS_STEPS_HIGH", "30")),
    "auto": int(os.environ.get("MOSS_STEPS_AUTO", "30")),
}
DEFAULT_STEPS = QUALITY_STEPS["auto"]

# 低于这个步数才叠 DMD 加速 LoRA（cfg 固定 1，负向词失效）。默认四档都不触发。
DMD_STEP_LIMIT = int(os.environ.get("MOSS_DMD_STEP_LIMIT", "20"))

DEFAULT_NEGATIVE = os.environ.get(
    "MOSS_NEGATIVE",
    "worst quality, bad anatomy, watermark, jpeg artifact",
)
# 图生图的步数定死，不跟 quality 档位和 num_inference_steps 走
EDIT_STEPS = int(os.environ.get("MOSS_EDIT_STEPS", "30"))
DEFAULT_COMPRESSION = int(os.environ.get("MOSS_COMPRESSION", "88"))

# ComfyUI 的 executor 一次只跑一个 prompt，>1 只是让第二个先在里面等着。
MAX_CONCURRENCY = max(1, int(os.environ.get("MOSS_MAX_CONCURRENCY", "1")))
QUEUE_LIMIT = max(0, int(os.environ.get("MOSS_QUEUE_LIMIT", "500")))
DEFAULT_TIMEOUT = float(os.environ.get("MOSS_TIMEOUT", "600"))

NODE_UNET, NODE_CLIP, NODE_VAE = "1", "2", "3"
NODE_EDIT_LORA, NODE_DMD_LORA = "4", "5"
NODE_POS, NODE_NEG = "10", "11"
NODE_IMG_BASE = 20
NODE_LATENT, NODE_SAMPLER = "40", "41"
NODE_DECODE, NODE_SAVE = "42", "44"

_gpu_sema: asyncio.Semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
_inflight = 0
_queue_peak = 0

# ------------------------------------------------------------------ 工具


class ClientGone(Exception):
    """客户端中途断开，ComfyUI 任务已撤销。"""


def parse_size(size: str) -> tuple[int, int]:
    try:
        width_str, height_str = size.lower().replace(" ", "").split("x", 1)
        width, height = int(width_str), int(height_str)
    except (ValueError, AttributeError):
        raise HTTPException(400, f"invalid size {size!r}, expected e.g. 1024x1024")
    if width % 16 or height % 16:
        raise HTTPException(400, "width and height must be multiples of 16")
    if width < 256 or height < 256 or width > 2048 or height > 2048:
        raise HTTPException(400, "size must be between 256 and 2048 per side")
    return width, height


def quality_to_steps(quality: Any, default: int, floor: int = 1) -> int:
    text = str(quality if quality is not None else "auto").strip().lower()
    if text.replace(".", "", 1).isdigit():
        return max(floor, default)
    return max(floor, QUALITY_STEPS.get(text, default))


def normalize_compression(value: Any, fallback: int = DEFAULT_COMPRESSION) -> int:
    if value is None or value == "" or isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return max(0, min(100, value))
    text = str(value).strip().lower()
    if text.isdigit():
        return max(0, min(100, int(text)))
    return {"high": 95, "hd": 95, "medium": 85, "standard": 85, "low": 70}.get(text, fallback)


def encode_image(payload: bytes, output_format: str, compression: Any) -> bytes:
    fmt = (output_format or "jpeg").lower().strip()
    if fmt == "png":
        return payload
    if fmt not in ("jpeg", "jpg"):
        raise HTTPException(400, f"unsupported output_format {output_format!r}")
    level = normalize_compression(compression)
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(500, f"jpeg output needs Pillow: {exc}")
    buffer = io.BytesIO(payload)
    with Image.open(buffer) as image:
        out = io.BytesIO()
        image.convert("RGB").save(out, format="JPEG", quality=level, optimize=True)
    return out.getvalue()


# ------------------------------------------------------------ ComfyUI 通信


def post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise HTTPException(502, f"comfyui rejected the request: {detail[:2000]}")
    except OSError as exc:
        raise HTTPException(502, f"comfyui unreachable at {COMFY_URL}: {exc}")


def get_json(url: str, timeout: float = 30) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.load(response)
    except OSError as exc:
        raise HTTPException(502, f"comfyui unreachable at {COMFY_URL}: {exc}")


async def _watch_disconnect(request: Request, cancel: threading.Event) -> None:
    """等 http.disconnect。不要用 is_disconnected() 轮询，uvicorn 下常探不到。"""
    try:
        while not cancel.is_set():
            message = await request.receive()
            if message.get("type") == "http.disconnect":
                print("[cancel] 收到 http.disconnect", flush=True)
                cancel.set()
                return
    except asyncio.CancelledError:
        return
    except Exception as exc:  # noqa: BLE001
        print(f"[cancel] watcher 退出: {type(exc).__name__}: {exc}", flush=True)


def cancel_prompt(prompt_id: str) -> None:
    """撤掉还没跑完的任务：排队中的从 /queue 删，正在跑的删完再 /interrupt。"""
    try:
        queue = get_json(f"{COMFY_URL}/queue")
    except (OSError, ValueError):
        return
    running = [item[1] for item in queue.get("queue_running", [])]
    pending = [item[1] for item in queue.get("queue_pending", [])]
    if prompt_id in pending or prompt_id in running:
        try:
            post_json(f"{COMFY_URL}/queue", {"delete": [prompt_id]})
        except (OSError, ValueError, HTTPException):
            pass
    if prompt_id in running:
        try:
            post_json(f"{COMFY_URL}/interrupt", {})
        except (OSError, ValueError, HTTPException):
            pass


def wait_for_result(prompt_id: str, timeout: float, should_cancel: Any = None) -> dict[str, Any]:
    deadline = time.time() + timeout
    history_url = f"{COMFY_URL}/history/{prompt_id}"
    while time.time() < deadline:
        if should_cancel is not None and should_cancel():
            raise ClientGone(prompt_id)
        try:
            history = get_json(history_url)
        except (OSError, ValueError):
            history = {}
        entry = history.get(prompt_id)
        if entry:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                detail = json.dumps(status.get("messages", []), ensure_ascii=False)[:2000]
                raise HTTPException(500, f"comfyui execution failed: {detail}")
            return entry
        time.sleep(0.5)
    raise HTTPException(504, "comfyui did not finish in time")


def fetch_image(entry: dict[str, Any]) -> bytes:
    images = entry.get("outputs", {}).get(NODE_SAVE, {}).get("images", [])
    if not images:
        raise HTTPException(500, "comfyui produced no image")
    image = images[0]
    query = urllib.parse.urlencode(
        {
            "filename": image.get("filename", ""),
            "subfolder": image.get("subfolder", ""),
            "type": image.get("type", "output"),
        }
    )
    with urllib.request.urlopen(f"{COMFY_URL}/view?{query}", timeout=180) as response:
        return response.read()


def edit_image_keys(count: int) -> list[str]:
    """TextEncodeMOSSImageEdit 的参考图键名：images.image_0, images.image_1 ..."""
    return [f"images.image_{index}" for index in range(count)]


def stage_image(payload: bytes, stem: str) -> str:
    """参考图落到 ComfyUI 的 input 目录，返回 LoadImage 需要的文件名。"""
    from PIL import Image

    name = f"moss_ref_{stem}.png"
    COMFY_INPUT.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(payload)) as image:
        image.convert("RGB").save(COMFY_INPUT / name, format="PNG")
    return name


def drop_staged(name: str) -> None:
    try:
        (COMFY_INPUT / name).unlink(missing_ok=True)
    except OSError:
        pass


# ------------------------------------------------------------------ 工作流


def base_graph(steps: int) -> dict[str, Any]:
    graph: dict[str, Any] = {
        NODE_UNET: {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": MODEL_UNET, "weight_dtype": "default"},
        },
        NODE_CLIP: {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": MODEL_TE, "type": "moss_image", "device": "default"},
        },
        NODE_VAE: {"class_type": "VAELoader", "inputs": {"vae_name": MODEL_VAE}},
        NODE_SAVE: {
            "class_type": "SaveImage",
            "inputs": {"images": [NODE_DECODE, 0], "filename_prefix": "moss_api"},
        },
    }
    if steps < DMD_STEP_LIMIT:
        graph[NODE_DMD_LORA] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"model": [NODE_UNET, 0], "lora_name": MODEL_DMD, "strength_model": 1.0},
        }
    return graph


def build_generation_graph(
    prompt: str, negative: str, width: int, height: int, steps: int, cfg: float, seed: int
) -> dict[str, Any]:
    graph = base_graph(steps)
    model_src = [NODE_DMD_LORA, 0] if steps < DMD_STEP_LIMIT else [NODE_UNET, 0]
    graph[NODE_POS] = {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": prompt, "clip": [NODE_CLIP, 0]},
    }
    graph[NODE_NEG] = {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": negative, "clip": [NODE_CLIP, 0]},
    }
    graph[NODE_LATENT] = {
        "class_type": "EmptyFlux2LatentImage",
        "inputs": {"width": width, "height": height, "batch_size": 1},
    }
    graph[NODE_SAMPLER] = {
        "class_type": "KSampler",
        "inputs": {
            "model": model_src,
            "positive": [NODE_POS, 0],
            "negative": [NODE_NEG, 0],
            "latent_image": [NODE_LATENT, 0],
            "seed": seed,
            "steps": steps,
            "cfg": 1.0 if steps < DMD_STEP_LIMIT else (cfg if cfg > 1.0 else 3.0),
            "sampler_name": "euler",
            "scheduler": "simple",
            "denoise": 1.0,
        },
    }
    graph[NODE_DECODE] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": [NODE_SAMPLER, 0], "vae": [NODE_VAE, 0]},
    }
    return graph


def build_edit_graph(
    prompt: str,
    negative: str,
    width: int,
    height: int,
    steps: int,
    cfg: float,
    seed: int,
    ref_names: list[str],
    lora_name: str,
    edit_format: str,
) -> dict[str, Any]:
    graph = base_graph(steps)
    graph[NODE_EDIT_LORA] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {"model": [NODE_UNET, 0], "lora_name": lora_name, "strength_model": 1.0},
    }
    if steps < DMD_STEP_LIMIT:
        graph[NODE_DMD_LORA]["inputs"]["model"] = [NODE_EDIT_LORA, 0]
        model_src = [NODE_DMD_LORA, 0]
    else:
        model_src = [NODE_EDIT_LORA, 0]

    keys = edit_image_keys(len(ref_names))
    images: dict[str, Any] = {}
    for index, (key, name) in enumerate(zip(keys, ref_names)):
        node_id = f"{NODE_IMG_BASE + index}"
        graph[node_id] = {"class_type": "LoadImage", "inputs": {"image": name}}
        images[key] = [node_id, 0]

    for node_id, text in ((NODE_POS, prompt), (NODE_NEG, negative)):
        graph[node_id] = {
            "class_type": "TextEncodeMOSSImageEdit",
            "inputs": {
                "clip": [NODE_CLIP, 0],
                "prompt": text,
                "vae": [NODE_VAE, 0],
                "edit_format": edit_format,
                "image_strength": 1.0,
                **images,
            },
        }

    graph[NODE_LATENT] = {
        "class_type": "EmptyFlux2LatentImage",
        "inputs": {"width": width, "height": height, "batch_size": 1},
    }
    graph[NODE_SAMPLER] = {
        "class_type": "KSampler",
        "inputs": {
            "model": model_src,
            "positive": [NODE_POS, 0],
            "negative": [NODE_NEG, 0],
            "latent_image": [NODE_LATENT, 0],
            "seed": seed,
            "steps": steps,
            "cfg": 1.0 if steps < DMD_STEP_LIMIT else (cfg if cfg > 1.0 else 3.0),
            "sampler_name": "euler",
            "scheduler": "simple",
            "denoise": 1.0,
        },
    }
    graph[NODE_DECODE] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": [NODE_SAMPLER, 0], "vae": [NODE_VAE, 0]},
    }
    return graph


def resolve_edit_variant(requested: str) -> tuple[str, str, str]:
    if requested in ("", "auto", None):
        order = (
            ["armv5", "moss"] if DEFAULT_EDIT_VARIANT in ("", "auto") else [DEFAULT_EDIT_VARIANT]
        )
    else:
        order = [requested]
    for variant in order:
        entry = EDIT_VARIANTS.get(variant)
        if entry and (LORA_DIR / entry[0]).is_file():
            return variant, entry[0], entry[1]
    raise HTTPException(
        400,
        "no edit LoRA installed: expected "
        + " or ".join(v[0] for v in EDIT_VARIANTS.values())
        + f" in {LORA_DIR}",
    )


# --------------------------------------------------------------- 执行队列


def run_graph(graph: dict[str, Any], timeout: float, should_cancel: Any = None) -> bytes:
    graph[NODE_SAVE]["inputs"]["filename_prefix"] = f"moss_api_{uuid.uuid4().hex[:8]}"
    accepted = post_json(f"{COMFY_URL}/prompt", {"prompt": graph, "client_id": "moss-openai"})
    prompt_id = accepted.get("prompt_id")
    if not prompt_id:
        raise HTTPException(502, f"comfyui rejected the job: {accepted}")
    try:
        entry = wait_for_result(prompt_id, timeout, should_cancel)
    except ClientGone:
        cancel_prompt(prompt_id)
        raise
    return fetch_image(entry)


async def run_graph_slotted(graph: dict[str, Any], timeout: float, should_cancel: Any = None) -> bytes:
    global _inflight, _queue_peak
    if _inflight >= MAX_CONCURRENCY + QUEUE_LIMIT:
        raise HTTPException(
            429,
            f"queue is full ({_inflight} waiting); retry later",
            headers={"Retry-After": "10"},
        )
    _inflight += 1
    _queue_peak = max(_queue_peak, _inflight)
    try:
        async with _gpu_sema:
            return await asyncio.to_thread(run_graph, graph, timeout, should_cancel)
    finally:
        _inflight -= 1


# ------------------------------------------------------------------ 接口

app = FastAPI(title="MOSS-Image OpenAI API", version="1.0.0")

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "invalid_api_key",
    429: "rate_limit_error",
    499: "client_closed_request",
    500: "server_error",
    502: "upstream_error",
    504: "timeout",
}


LOGGER = logging.getLogger("moss.api")


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
    """把 FastAPI 的 {"detail": ...} 转成 OpenAI 的 {"error": {...}}，客户端不用写两套解析。"""
    if exc.status_code >= 500:
        LOGGER.error("%s %s -> %s: %s", request.method, request.url.path,
                     exc.status_code, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None) or None,
        content={
            "error": {
                "message": str(exc.detail),
                "type": _ERROR_TYPES.get(exc.status_code, "api_error"),
                "code": exc.status_code,
            }
        },
    )


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """未捕获异常：记完整栈到日志，客户端拿 OpenAI 形状的 500。"""
    LOGGER.error("未捕获异常 %s %s\n%s", request.method, request.url.path,
                 traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"error": {"message": f"{type(exc).__name__}: {exc}",
                           "type": "server_error", "code": 500}},
    )


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "message": "invalid request: " + json.dumps(exc.errors(), ensure_ascii=False)[:1500],
                "type": "invalid_request_error",
                "code": 400,
            }
        },
    )


def check_auth(authorization: str = Header(default="")) -> None:
    if not API_KEY:
        return
    token = authorization.removeprefix("Bearer ").strip()
    if token != API_KEY:
        raise HTTPException(401, "invalid api key")


class ImageRequest(BaseModel):
    prompt: str
    model: str | None = None
    n: int = Field(default=1, ge=1, le=4)
    size: str = "1024x1024"
    seed: int | None = None
    num_inference_steps: int | None = Field(default=None, ge=1, le=100)
    guidance_scale: float = Field(default=3.0, ge=0.0, le=20.0)
    negative_prompt: str = ""
    response_format: str = "b64_json"
    output_format: str = "jpeg"
    quality: str | int = "auto"
    output_compression: int = Field(default=DEFAULT_COMPRESSION, ge=0, le=100)
    timeout: float = DEFAULT_TIMEOUT



# ---------------------------------------------------------------- GPU 体检
# 租来的机器良莠不齐：同样标着 3090，实际算力能差一倍（电源/散热被限流、
# 主板只给 PCIe 1.0 的矿板翻新机）。开机后跑一次 1 秒的矩阵乘，把实测
# 算力写进 /healthz —— 低于 45 TFLOPS 的机器建议直接销毁重租。
_GPU_PROBE: dict[str, Any] = {}


def gpu_probe(force: bool = False) -> dict[str, Any]:
    global _GPU_PROBE
    if _GPU_PROBE and not force:
        return _GPU_PROBE
    try:
        import torch

        if not torch.cuda.is_available():
            _GPU_PROBE = {"available": False}
            return _GPU_PROBE
        size = 4096
        a = torch.randn(size, size, device="cuda", dtype=torch.float16)
        b = torch.randn(size, size, device="cuda", dtype=torch.float16)
        for _ in range(3):
            a @ b
        torch.cuda.synchronize()
        start = time.time()
        rounds = 20
        for _ in range(rounds):
            a @ b
        torch.cuda.synchronize()
        elapsed = max(time.time() - start, 1e-6)
        tflops = 2 * size**3 * rounds / elapsed / 1e12
        name = torch.cuda.get_device_name(0)
        total = torch.cuda.get_device_properties(0).total_memory / 1e9
        verdict = "ok" if tflops >= 45 else ("slow" if tflops >= 30 else "bad")
        _GPU_PROBE = {
            "available": True,
            "name": name,
            "vram_gb": round(total, 1),
            "tflops_fp16": round(tflops, 1),
            "verdict": verdict,
        }
        del a, b
        torch.cuda.empty_cache()
    except Exception as exc:  # 体检失败不影响出图
        _GPU_PROBE = {"available": False, "error": str(exc)[:120]}
    return _GPU_PROBE


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    comfy = "down"
    try:
        get_json(f"{COMFY_URL}/system_stats", timeout=5)
        comfy = "up"
    except (OSError, ValueError):
        pass
    installed = {k: (LORA_DIR / v[0]).is_file() for k, v in EDIT_VARIANTS.items()}
    return {
        "ok": True,
        "comfyui": comfy,
        "models": [MODEL_ID, *MODEL_ALIASES],
        "edits": any(installed.values()),
        "edit_loras": installed,
        "steps": QUALITY_STEPS,
        "gpu": gpu_probe(),
        "concurrency": {
            "max": MAX_CONCURRENCY,
            "queue_limit": QUEUE_LIMIT,
            "inflight": _inflight,
            "peak_seen": _queue_peak,
        },
    }


@app.get("/v1/models")
def list_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": int(time.time()), "owned_by": "local"}
            for name in [MODEL_ID, *MODEL_ALIASES]
        ],
    }


@app.post("/v1/images/generations", dependencies=[])
async def generate_images(request: ImageRequest, raw: Request, authorization: str = Header(default="")) -> JSONResponse:
    check_auth(authorization)
    if request.response_format != "b64_json":
        raise HTTPException(400, "only response_format=b64_json is supported")

    width, height = parse_size(request.size)
    steps = request.num_inference_steps or quality_to_steps(request.quality, DEFAULT_STEPS)
    base_seed = request.seed if request.seed is not None else int(time.time() * 1000) % 2**31
    started = time.time()
    images: list[dict[str, str]] = []

    cancel = threading.Event()
    watcher = asyncio.create_task(_watch_disconnect(raw, cancel))
    try:
        for index in range(request.n):
            seed = (base_seed + index) % 2**32
            graph = build_generation_graph(
                prompt=request.prompt,
                negative=request.negative_prompt or DEFAULT_NEGATIVE,
                width=width,
                height=height,
                steps=steps,
                cfg=request.guidance_scale,
                seed=seed,
            )
            payload = await run_graph_slotted(graph, request.timeout, cancel.is_set)
            payload = encode_image(payload, request.output_format, request.output_compression)
            images.append({"b64_json": base64.b64encode(payload).decode()})
    except ClientGone:
        raise HTTPException(499, "client disconnected; comfyui job cancelled")
    finally:
        watcher.cancel()

    return JSONResponse(
        {
            "created": int(time.time()),
            "data": images,
            "num_inference_steps": steps,
            "elapsed_seconds": round(time.time() - started, 2),
        }
    )


async def _read_image_bytes(value: Any) -> bytes:
    """接受 data:base64、裸 base64、http(s) URL。"""
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(400, "image must be a base64 string, data URL, or http(s) url")
    text = value.strip()
    if text.startswith("http://") or text.startswith("https://"):
        try:
            with urllib.request.urlopen(text, timeout=60) as response:
                return response.read()
        except OSError as exc:
            raise HTTPException(400, f"cannot download image: {exc}")
    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    try:
        return base64.b64decode(text, validate=False)
    except (ValueError, TypeError):
        raise HTTPException(400, "image is not valid base64")


def _auto_size(payload: bytes) -> tuple[int, int]:
    """没给 size 时按参考图比例取一个 16 的倍数，边长钳到 256..1536。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(payload)) as image:
            width, height = image.size
    except Exception:  # noqa: BLE001
        return 1024, 1024
    scale = min(1536 / max(width, height), 1.0)
    width = max(256, min(1536, int(width * scale) // 16 * 16))
    height = max(256, min(1536, int(height * scale) // 16 * 16))
    return width, height


def _is_oom(exc: "HTTPException") -> bool:
    """判断 ComfyUI 报的是不是显存不足（多参考图在 24G 卡上很容易撞到）。"""
    detail = str(getattr(exc, "detail", "")).lower()
    return ("out of memory" in detail) or ("would exceed allowed memory" in detail)


def free_comfy() -> None:
    """让 ComfyUI 释放缓存，给降级重试腾显存（不动已驻留的权重）。"""
    try:
        body = json.dumps({"unload_models": False, "free_memory": True}).encode()
        req = urllib.request.Request(
            f"{COMFY_URL}/free", data=body, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:  # noqa: BLE001 - 释放失败不影响主流程
        pass


@app.post("/v1/images/edits")
async def edit_images(raw: Request, authorization: str = Header(default="")) -> JSONResponse:
    """OpenAI /v1/images/edits：同时接受 multipart/form-data 和 JSON。

    multipart 字段：image（可多个）、prompt、size、n、quality、seed、
    negative_prompt、output_format、output_compression、edit_variant。
    JSON 字段同上，image 用 base64 / data URL / http(s) 链接。
    """
    check_auth(authorization)
    content_type = raw.headers.get("content-type", "")

    fields: dict[str, Any] = {}
    blobs: list[bytes] = []

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await raw.form()
        for key, value in form.multi_items():
            if key in ("image", "image[]", "images", "images[]"):
                if hasattr(value, "read"):
                    blobs.append(await value.read())
            else:
                fields[key] = value
    else:
        try:
            body = await raw.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "body must be multipart/form-data or JSON")
        fields = dict(body)
        raw_images = body.get("image") or body.get("images") or []
        if isinstance(raw_images, (str, dict)):
            raw_images = [raw_images]
        for item in raw_images:
            blobs.append(await _read_image_bytes(item))

    prompt = str(fields.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    if not blobs:
        raise HTTPException(400, "image is required")
    # 24G 卡上多参考图 + 大尺寸必然 OOM（实测 2 图 1536x1024 单次分配就要 7.81G），
    # 直接限制成单参考图，宁可明确报错也不让请求撞显存。
    MAX_REF_IMAGES = 1
    if len(blobs) > MAX_REF_IMAGES:
        raise HTTPException(
            400,
            f"only 1 reference image is supported (got {len(blobs)}); "
            "send a single 'image' field, merge multiple refs into one image if needed",
        )

    size = str(fields.get("size") or "").strip()
    width, height = parse_size(size) if size else _auto_size(blobs[0])

    # 改图固定步数：quality 和 num_inference_steps 都不影响，保证改图延迟稳定
    steps = EDIT_STEPS
    n = max(1, min(4, int(fields.get("n") or 1)))
    seed_value = fields.get("seed")
    base_seed = int(seed_value) if seed_value not in (None, "") else int(time.time() * 1000) % 2**31
    negative = str(fields.get("negative_prompt") or DEFAULT_NEGATIVE)
    cfg = float(fields.get("guidance_scale") or 3.0)
    output_format = str(fields.get("output_format") or "jpeg")
    compression = fields.get("output_compression", DEFAULT_COMPRESSION)
    variant = str(fields.get("edit_variant") or "auto")
    timeout = float(fields.get("timeout") or DEFAULT_TIMEOUT)

    _, lora_name, edit_format = resolve_edit_variant(variant)

    started = time.time()
    staged: list[str] = []
    images: list[dict[str, str]] = []
    degraded: dict[str, Any] = {}
    cancel = threading.Event()
    watcher = asyncio.create_task(_watch_disconnect(raw, cancel))
    try:
        stem = uuid.uuid4().hex[:10]
        for index, blob in enumerate(blobs):
            staged.append(stage_image(blob, f"{stem}_{index}"))
        for index in range(n):
            seed = (base_seed + index) % 2**32
            # 显存不足时逐级降级：先缩尺寸，再砍到单参考图。24G 卡上多参考图 + 1024x1536 会 OOM。
            plan = [(1.0, 1), (0.75, 1), (0.55, 1)]
            last_error: HTTPException | None = None
            for scale, ref_count in plan:
                used_w = max(256, int(round(width * scale / 16.0)) * 16)
                used_h = max(256, int(round(height * scale / 16.0)) * 16)
                refs = staged[:ref_count]
                graph = build_edit_graph(
                    prompt=prompt,
                    negative=negative,
                    width=used_w,
                    height=used_h,
                    steps=steps,
                    cfg=cfg,
                    seed=seed,
                    ref_names=refs,
                    lora_name=lora_name,
                    edit_format=edit_format,
                )
                try:
                    payload = await run_graph_slotted(graph, timeout, cancel.is_set)
                except HTTPException as exc:
                    if not _is_oom(exc):
                        raise
                    last_error = exc
                    LOGGER.warning(
                        "改图显存不足，降级重试：scale=%s refs=%d size=%dx%d（原始 %dx%d，%d 张参考图）",
                        scale, ref_count, used_w, used_h, width, height, len(staged),
                    )
                    free_comfy()
                    continue
                if scale != 1.0 or ref_count != len(staged):
                    degraded["size"] = f"{used_w}x{used_h}"
                    degraded["refs"] = ref_count
                width, height = used_w, used_h
                payload = encode_image(payload, output_format, compression)
                images.append({"b64_json": base64.b64encode(payload).decode()})
                break
            else:
                raise last_error or HTTPException(500, "comfyui execution failed: out of memory")
    except ClientGone:
        raise HTTPException(499, "client disconnected; comfyui job cancelled")
    finally:
        watcher.cancel()
        for name in staged:
            drop_staged(name)

    return JSONResponse(
        {
            "created": int(time.time()),
            "data": images,
            "num_inference_steps": steps,
            "size": f"{width}x{height}",
            "edit_lora": lora_name,
            "elapsed_seconds": round(time.time() - started, 2),
            **({"degraded": degraded} if degraded else {}),
        }
    )


def main() -> None:
    global COMFY_URL, COMFY_INPUT
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("MOSS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MOSS_PORT", "8000")))
    parser.add_argument("--comfy", default=COMFY_URL)
    parser.add_argument("--comfy-input", default=str(COMFY_INPUT))
    args = parser.parse_args()

    COMFY_URL = args.comfy.rstrip("/")
    COMFY_INPUT = Path(args.comfy_input)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
