#!/usr/bin/env python3
"""按清单下载权重：已有就跳过，缺失才拉，单文件可断点续传。

环境变量：
  MOSS_MODEL_DIR       权重根目录，默认 /models
  HF_ENDPOINT          镜像站，国内可设 https://hf-mirror.com
  HF_TOKEN             私有仓库才需要
  MOSS_OPTIONAL_MODELS 设 1 才下载 optional=true 的条目
  MOSS_MODELS_MANIFEST 清单路径，默认 /app/config/models.json
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

MANIFEST = Path(os.environ.get("MOSS_MODELS_MANIFEST", "/app/config/models.json"))
MODEL_DIR = Path(os.environ.get("MOSS_MODEL_DIR", "/models"))
ENDPOINT = os.environ.get("HF_ENDPOINT", "").strip()
WITH_OPTIONAL = os.environ.get("MOSS_OPTIONAL_MODELS", "0") == "1"


def log(message: str) -> None:
    print(f"[models {time.strftime('%H:%M:%S')}] {message}", flush=True)


def main() -> int:
    if ENDPOINT:
        os.environ["HF_ENDPOINT"] = ENDPOINT

    from huggingface_hub import HfApi, hf_hub_download

    files = json.loads(MANIFEST.read_text(encoding="utf-8"))["files"]
    api = HfApi()
    missing = False

    for entry in files:
        dest = MODEL_DIR / entry["dest"]
        repo = entry["repo"]
        required = bool(entry.get("required"))
        if not required and not WITH_OPTIONAL:
            log(f"跳过可选：{dest.name}")
            continue

        if dest.is_file() and dest.stat().st_size > 1024 * 1024:
            log(f"已存在：{dest}（{dest.stat().st_size / 1e9:.2f} GB）")
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        path = entry.get("path") or dest.name
        log(f"下载 {repo}/{path} → {dest}")

        # 仓库目录结构和清单不一致时，按文件名在仓库里搜一遍
        try:
            files_in_repo = api.list_repo_files(repo)
        except Exception as exc:  # noqa: BLE001
            log(f"列仓库文件失败：{type(exc).__name__}: {exc}")
            files_in_repo = []

        if files_in_repo and path not in files_in_repo:
            matches = [f for f in files_in_repo if f.rsplit("/", 1)[-1] == dest.name]
            if not matches:
                matches = [f for f in files_in_repo if f.endswith(".safetensors")]
            if matches:
                path = sorted(matches, key=len)[0]
                log(f"清单路径不存在，改用：{path}")

        try:
            cached = hf_hub_download(repo_id=repo, filename=path, local_dir=str(dest.parent))
        except Exception as exc:  # noqa: BLE001
            log(f"下载失败 {repo}/{path}：{type(exc).__name__}: {exc}")
            if required:
                missing = True
            continue

        downloaded = Path(cached)
        if downloaded != dest and downloaded.is_file():
            downloaded.replace(dest)
        log(f"完成：{dest}（{dest.stat().st_size / 1e9:.2f} GB）")

    if missing:
        log("有必需权重没下成功；服务会启动但出图会失败，请检查网络或 HF_ENDPOINT")
        return 1
    log("权重就绪")
    return 0


if __name__ == "__main__":
    sys.exit(main())
