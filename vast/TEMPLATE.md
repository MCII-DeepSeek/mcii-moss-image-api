# Vast.ai 模板填写说明

建模板时对着这张表填。**镜像要先发布到 GHCR**，模板本身不装环境。

## 一、基本信息

| 字段 | 填什么 |
|---|---|
| 模板名 | `mcii-moss-image-api` |
| 描述 | OpenAI 兼容 MOSS 文生图 / 指令改图（单卡 24GB） |
| Docker 镜像 | `ghcr.io/mcii-deepseek/mcii-moss-image-api:latest` |
| 镜像类型 | 公共（GHCR 公共包匿名可拉） |
| 启动模式 | **Docker 入口点**（三张卡片里最右边那张） |

> 不要选 Jupyter。镜像的 ENTRYPOINT 会自己起 ComfyUI 和网关，选 Jupyter 只会多一层没用的界面。

## 二、端口

| 字段 | 填什么 |
|---|---|
| 对外端口 | **不加**（走 SSH 反向隧道，不需要公网口） |
| 内部端口 | 8000 |

调试期想临时从浏览器点开，可以加一条 `8000 → 8000`，验证完删掉。Vast 会为开放端口建
Cloudflare 隧道，地址每次开机都变，**不要拿它当正式入口**。

## 三、Docker 选项

在「Docker create/run options」里填：

```
--shm-size=32gb
```

## 四、环境变量

批量粘贴用「批量粘贴」，一行一个：

```
MOSS_API_KEY=<自己生成一串 sk-moss- 开头的>
MOSS_MODEL_DIR=/models
MOSS_MAX_CONCURRENCY=1
MOSS_PYTHONUNBUFFERED=1
```

可选：

| 变量 | 什么时候加 |
|---|---|
| `MOSS_STEPS_LOW=24` `MOSS_STEPS_MEDIUM=30` `MOSS_STEPS_HIGH=36` `MOSS_STEPS_AUTO=30` | 想改档位时（默认就是这四个值，不填一样） |
| `HF_ENDPOINT=https://hf-mirror.com` | 宿主机连 HuggingFace 慢时 |
| `HF_TOKEN=hf_xxx` | 权重仓库需要登录时（一般不必要） |
| `MOSS_OPTIONAL_MODELS=1` | 想顺带下 armV5 和 8 步 DMD LoRA |
| `MOSS_SKIP_DOWNLOAD=1` | 已经用卷挂好了完整权重，跳过启动检查 |
| `MOSS_COMPRESSION=88` | 改 JPEG 默认质量 |

## 五、磁盘

| 字段 | 填什么 |
|---|---|
| 磁盘大小 | **80GB**（镜像约 9GB + 权重 17GB + 余量） |
| 挂载 | 不用挂，`/models` 就在实例盘上 |

权重放在实例盘上：**停机保留、销毁即失**。想跨实例复用，要在同一台物理机上建 Vast 卷挂到 `/models`；卷不能跨机器。

## 六、口令与访问

| 字段 | 填什么 |
|---|---|
| SSH | 开启，用 Vast 给的公钥/密码方式 |
| 启动脚本 | **留空** |

启动脚本留空是因为镜像 ENTRYPOINT 已经做了：检查权重 → 起 ComfyUI → 等就绪 → 起网关。
写启动脚本反而会覆盖掉这套流程。

## 七、开机后要做的

实例起来后（首次约 10–20 分钟，主要在下载 17GB 权重）：

```bash
# 1) 看服务是否就绪
curl -s http://127.0.0.1:8000/healthz

# 2) 建反向隧道回橙子（后台跑）
nohup env MOSS_ORANGE_HOST=92.113.124.35 \
          MOSS_ORANGE_SSH_PORT=20010 \
          MOSS_REMOTE_PORT=18010 \
          bash /app/scripts/tunnel.sh &

# 3) 在橙子上验证
curl -s http://127.0.0.1:18010/healthz
```

sub2api 那边把地址指到橙子的 `18010`（或容器网络里对应地址），模型名沿用
`gpt-image-moss` / `moss-image`。

## 八、验证清单

| 检查 | 期望 |
|---|---|
| `/healthz` | `comfyui: up`，`edits: true` |
| 文生图 512×512 `quality=low` | 200，24 步 |
| 文生图 `quality=high` | 200，36 步 |
| 改图单图 | 200，返回 `edit_lora` |
| 连续 文生图 → 改图 → 文生图 | 都成功，记录 `elapsed_seconds` |
| `nvidia-smi` 峰值 | 记下来，判断还能开多大尺寸 |
