# 接口

## 鉴权

设置 `MOSS_API_KEY` 后，所有 `/v1/*` 需要：

```
Authorization: Bearer <key>
```

`/healthz` 与 `/v1/models` 不需要（便于探测）。

## POST /v1/images/generations

请求体 JSON：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `prompt` | string | 必填 | 提示词 |
| `size` | string | `1024x1024` | `宽x高`，16 的倍数，256–2048 |
| `n` | int | 1 | 1–4，串行出图 |
| `quality` | string | `auto` | `low` 24 / `medium` 30 / `high` 36 / `auto` 30 步 |
| `num_inference_steps` | int | – | 直接指定步数，优先于 quality |
| `guidance_scale` | float | 3.0 | CFG；≥20 步时生效 |
| `negative_prompt` | string | 内置 | 负向提示词 |
| `seed` | int | 随机 | 固定可复现 |
| `output_format` | string | `jpeg` | `jpeg` / `png` |
| `output_compression` | int | 88 | JPEG 质量 0–100 |
| `timeout` | float | 600 | 单张超时秒 |

响应：

```json
{
  "created": 1763000000,
  "data": [{"b64_json": "..."}],
  "num_inference_steps": 30,
  "elapsed_seconds": 22.4
}
```

## POST /v1/images/edits

两种 body 都接受。

**multipart/form-data**

| 字段 | 说明 |
|---|---|
| `image` | 参考图文件，可重复传（1–4 张），也可写 `image[]` |
| `prompt` | 编辑指令，必填 |
| `size` | 可选，不传按第一张参考图比例 |
| `quality` / `num_inference_steps` / `seed` / `negative_prompt` / `guidance_scale` | 同上 |
| `edit_variant` | `auto` / `armv5` / `moss` |
| `n` / `output_format` / `output_compression` / `timeout` | 同上 |

**application/json**

```json
{
  "prompt": "...",
  "image": ["data:image/png;base64,..."],
  "size": "1024x1024"
}
```

`image` 每项可以是裸 base64、data URL 或 http(s) 链接。

响应在生图基础上多两个字段：

```json
{
  "size": "1024x1024",
  "edit_lora": "moss_image_edit_lora_bf16.safetensors"
}
```

## GET /v1/models

```json
{"object":"list","data":[{"id":"gpt-image-moss","object":"model","owned_by":"local"},
                         {"id":"moss-image","object":"model","owned_by":"local"}]}
```

模型名只做标识，切换模型请换部署或改环境变量。

## GET /healthz

```json
{
  "ok": true,
  "comfyui": "up",
  "models": ["gpt-image-moss", "moss-image"],
  "edits": true,
  "edit_loras": {"armv5": false, "moss": true},
  "steps": {"low": 24, "medium": 30, "high": 36, "auto": 30},
  "concurrency": {"max": 1, "queue_limit": 500, "inflight": 0, "peak_seen": 2}
}
```

## 错误

| 状态码 | 含义 |
|---|---|
| 400 | 参数不合法（尺寸、图片格式、没有可用编辑 LoRA） |
| 401 | API Key 不对 |
| 429 | 排队已满（`Retry-After: 10`） |
| 499 | 客户端断开，ComfyUI 任务已撤销 |
| 500 | ComfyUI 执行失败（body 里有节点报错原文） |
| 502 | ComfyUI 拒绝请求 |
| 504 | 超时 |

错误体沿用 OpenAI 形状，方便客户端按 `error.message` 处理。
