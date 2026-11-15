# mcii-moss-image-api

把 **MOSS-Image**（5B DiT）包成一套 **OpenAI 兼容**的图片服务：同一个端口同时提供
文生图和指令改图，背后由 ComfyUI 执行，适合在单张 24GB 卡（RTX 3090 等）上部署。

```
OpenAI 客户端 / sub2api
        │  HTTP + Bearer
        ▼
  server.py（8000）          OpenAI 协议、排队、取消、压缩
        │  HTTP
        ▼
  ComfyUI（8188，仅回环）    MOSS 权重、编辑 LoRA、采样、显存管理
```

## 能做什么

| 能力 | 接口 |
|---|---|
| 文生图 | `POST /v1/images/generations` |
| 指令改图（1–4 张参考图） | `POST /v1/images/edits` |
| 模型列表 | `GET /v1/models` |
| 健康检查 | `GET /healthz` |

- **quality → 步数**：`low=24`、`medium=30`、`high=36`、`auto=30`（可用环境变量改）
- 输入：`prompt`、`size`、`n`、`seed`、`negative_prompt`、`guidance_scale`、`output_format`、`output_compression`
- 改图额外支持：多参考图、`edit_variant` 选择 LoRA、不传 `size` 时按参考图比例自动取尺寸
- 输出：`b64_json`（可转 JPEG，默认质量 88）
- **GPU 一次只跑一个任务**，其余在进程内排队；客户端断开会把 ComfyUI 里的任务撤销（返回 499）

## 硬件与依赖

| 项 | 建议 |
|---|---|
| GPU | 24GB（RTX 3090 / 4090 / A10G 等），INT8 权重常驻约 13.3GB |
| 系统内存 | ≥32GB，建议 64GB（加载与 CPU 卸载） |
| 磁盘 | ≥80GB（镜像 + 权重约 17GB + 运行空间） |
| 驱动 | 支持 CUDA 12.x 的驱动（镜像内自带运行库） |

## 开机自动完成的事

容器启动后由 `boot_custom.sh` 接管（Vast 的 entrypoint 在 SSH 模式下不起 supervisord，
所以不能用 Supervisor），依次完成：

1. **权重检查**：清单里缺失的文件才下载，已存在且大小正确直接跳过，重启不重复拉
2. **自动选源**：对 ModelScope / HuggingFace / hf-mirror 各做 3 秒限时测速，取吞吐最高的
3. **多线程分块下载**：默认 8 线程按 Range 分块，实测比单线程快 7 倍（5MB/s → 36MB/s）
4. **失败自动换源**：某源中途断了换下一个源重试，全部失败才算失败
5. **拉起 ComfyUI**（等 `/system_stats` 就绪，最长 15 分钟）
6. **拉起 OpenAI 网关**（8000）
7. **建立反向隧道**（配了 `MOSS_TUNNEL_HOST` 才建）
8. **保活**：任何一环挂掉，5 秒后整体重启

装完即可用，不需要 SSH 进去调任何东西。

> **驱动要求**：torch 用 `2.15.0+cu132`，宿主机驱动需支持 CUDA 13.x（≥580）。
> 实测同一 ComfyUI 提交下，cu128 的 torch 2.9.1 跑 1024×1536/30 步要 126 秒，
> cu132 的 2.15.0 只要 39.5 秒。

## 快速开始

镜像基于 **Vast.ai 官方基础镜像**（`vastai/base-image`），所以 SSH、Supervisor、
Instance Portal 和实例日志都能正常工作；服务由 Supervisor 托管，开机自启、挂了自重启。

```bash
docker run -d --gpus all -p 127.0.0.1:8000:8000 \
  -v /data/moss-models:/models \
  -e MOSS_API_KEY=sk-your-key \
  ghcr.io/mcii-deepseek/mcii-moss-image-api:latest
```

首次启动会检查 `/models`，缺失才下载（约 16GB，支持断点续传）。国内可加
`-e HF_ENDPOINT=https://hf-mirror.com`。

```bash
curl -s http://127.0.0.1:8000/healthz
curl -s http://127.0.0.1:8000/v1/images/generations \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"a red paper lantern, Chinese character 春","size":"1024x1024","quality":"high"}'
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MOSS_API_KEY` | 空 | 不设则不鉴权；生产必须设 |
| `MOSS_PORT` | `8000` | 网关端口 |
| `MOSS_HOST` | `127.0.0.1` | 监听地址 |
| `MOSS_MODEL_DIR` | `/models` | 权重根目录（挂卷） |
| `MOSS_COMFY_URL` | `http://127.0.0.1:8188` | ComfyUI 地址 |
| `MOSS_STEPS_LOW/MEDIUM/HIGH/AUTO` | 24/30/36/30 | quality 映射 |
| `MOSS_MAX_CONCURRENCY` | `1` | 同时在跑的任务数 |
| `MOSS_QUEUE_LIMIT` | `500` | 排队上限，超过返回 429 |
| `MOSS_TIMEOUT` | `600` | 单张图超时秒数 |
| `MOSS_UNET/TE/VAE` | INT8 四件套 | 权重文件名 |
| `MOSS_EDIT` / `MOSS_EDIT_ARM` | chinoll / armV5 | 编辑 LoRA 文件名 |
| `MOSS_EDIT_DEFAULT` | `auto` | `auto` 按 armV5 → chinoll 顺序取已装的 |
| `MOSS_SKIP_DOWNLOAD` | `0` | 设 1 跳过启动时权重检查 |
| `HF_ENDPOINT` | 官方 | 设为 `https://hf-mirror.com` 走国内镜像 |
| `MOSS_NEGATIVE` | 内置 | 默认负向词 |
| `MOSS_COMPRESSION` | `88` | 默认 JPEG 质量 |

## 改图

```bash
curl -s http://127.0.0.1:8000/v1/images/edits \
  -H "Authorization: Bearer sk-your-key" \
  -F image=@photo.jpg \
  -F prompt="把桌上的苹果换成一只梨，其他保持不动" \
  -F quality=high
```

也接受 JSON body，`image` 传 base64 / data URL / http 链接：

```json
{
  "prompt": "把背景换成黄昏的海边",
  "image": ["data:image/png;base64,..."],
  "size": "1024x1024",
  "quality": "medium"
}
```

不传 `size` 时按第一张参考图比例取 16 的倍数，边长钳到 256–1536。

## 私有部署（反向隧道）

不想给算力机开公网端口，也不要配域名证书时，从算力机反向连到自己的跳板机：

```bash
MOSS_ORANGE_HOST=1.2.3.4 MOSS_ORANGE_SSH_PORT=22 \
MOSS_REMOTE_PORT=18010 bash scripts/tunnel.sh
```

对端 18010 就指向本机 8000，全程只有 SSH 一条加密连接。

## 已知边界

- **一次一个任务**：ComfyUI 的 executor 串行，`MOSS_MAX_CONCURRENCY>1` 只让第二个先在里面等，不会更快。
- **改图峰值高于文生图**：多一张参考图的 latent / 条件缓存。大尺寸 + 多参考图需要单独验证。
- **步数不是显存开关**：24→36 步主要增加时间，不等于显存成倍上升。
- **尺寸上限** 2048/边（协议层校验）；实际可用尺寸以显存为准。
- **不支持** `response_format=url`、蒙版局部重绘、`/v1/images/variations`。

## 目录

```
app/server.py          OpenAI 兼容网关（生图 / 改图 / 队列 / 取消）
scripts/entrypoint.sh  启动 ComfyUI 与网关
scripts/download_models.py  按清单下载权重
scripts/tunnel.sh      反向 SSH 隧道
config/models.json     权重清单
config/requirements-comfyui-extra.txt  ComfyUI 额外依赖
vast/TEMPLATE.md       Vast.ai 模板填写说明
docs/API.md            接口细节
```

## 许可

本仓库代码 MIT。镜像里的 ComfyUI 是 GPL-3.0（构建时安装，不随仓库分发源码）。
MOSS-Image 权重遵循其官方许可；编辑 LoRA 的代码与权重许可见各自仓库（chinoll 项目代码 GPLv3，
权重未单独声明；MOSS-Image-Edit-HQ 为 Apache-2.0）。商用前请自行核对。
