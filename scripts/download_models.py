#!/usr/bin/env python3
"""按清单下载权重：先测速挑最快源，再多线程分块拉，缺失才下、支持断点。

设计要点
  * 源：ModelScope / HuggingFace / hf-mirror（可通过 MOSS_SOURCES 覆盖）
  * 每个源先做 3 秒限时测速，取吞吐最高的那个
  * 单文件 8 线程按 Range 分块（ModelScope/HF 都支持 206）
  * 下载失败自动换下一个源，全部失败才算失败
  * 已存在且大小正确的文件直接跳过，重启不重复下载

环境变量
  MOSS_MODEL_DIR        权重根目录，默认 /models
  MOSS_MODELS_MANIFEST  清单路径，默认 <仓库>/config/models.json
  MOSS_OPTIONAL_MODELS  设 1 才下载 optional=true 的条目
  MOSS_DL_THREADS       并行线程数，默认 8
  MOSS_SOURCES          逗号分隔的源顺序，默认 modelscope,huggingface,hf-mirror
  HF_TOKEN              私有仓库需要
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent
MANIFEST = Path(os.environ.get("MOSS_MODELS_MANIFEST", str(_HERE.parent / "config" / "models.json")))
MODEL_DIR = Path(os.environ.get("MOSS_MODEL_DIR", "/models"))
OPTIONAL = os.environ.get("MOSS_OPTIONAL_MODELS", "0") == "1"
THREADS = int(os.environ.get("MOSS_DL_THREADS", "8"))
CHUNK = 16 * 1024 * 1024
PROBE_BYTES = 8 * 1024 * 1024
PROBE_SECONDS = 3.0

SOURCES = [
    name.strip()
    for name in os.environ.get("MOSS_SOURCES", "modelscope,huggingface,hf-mirror").split(",")
    if name.strip()
]

URLS = {
    "modelscope": "https://www.modelscope.cn/api/v1/models/{repo}/repo?Revision=master&FilePath={path}",
    "huggingface": "https://huggingface.co/{repo}/resolve/main/{path}",
    "hf-mirror": "https://hf-mirror.com/{repo}/resolve/main/{path}",
}

LOG_LOCK = threading.Lock()


def log(message: str) -> None:
    with LOG_LOCK:
        print(f"[models {time.strftime('%H:%M:%S')}] {message}", flush=True)


def build_url(source: str, repo: str, path: str) -> str:
    return URLS[source].format(repo=repo, path=urllib.parse.quote(path))


def http_head(url: str) -> int | None:
    """取文件大小：先 HEAD，部分 CDN 不给就退回 Range 探针。"""
    for headers in (None, {"Range": "bytes=0-0"}):
        try:
            req = urllib.request.Request(url, method="HEAD", headers=headers or {})
            with urllib.request.urlopen(req, timeout=20) as resp:
                length = resp.headers.get("Content-Length")
                if headers and length in (None, "1"):
                    cr = resp.headers.get("Content-Range") or ""
                    if "/" in cr:
                        return int(cr.split("/")[-1])
                if length and int(length) > 1024:
                    return int(length)
        except Exception:
            continue
    return None


def probe(url: str) -> float:
    """限时读一小段，返回 B/s；不支持 Range 或失败返回 0。"""
    start = time.time()
    got = 0
    try:
        req = urllib.request.Request(url, headers={"Range": f"bytes=0-{PROBE_BYTES - 1}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            while time.time() - start < PROBE_SECONDS:
                chunk = resp.read(512 * 1024)
                if not chunk:
                    break
                got += len(chunk)
    except Exception:
        return 0.0
    elapsed = max(time.time() - start, 0.001)
    return got / elapsed if got > 0 else 0.0


def fetch_range(url: str, start: int, end: int, dest: Path, attempt: int = 0) -> None:
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = resp.read()
        want = end - start + 1
        if len(data) != want:
            raise IOError(f"短读 {len(data)}/{want}")
        with open(dest, "r+b") as fh:
            fh.seek(start)
            fh.write(data)
    except Exception:
        if attempt >= 2:
            raise
        time.sleep(1 + attempt * 2)
        fetch_range(url, start, end, dest, attempt + 1)


def download_from(url: str, size: int, dest: Path) -> bool:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with open(tmp, "wb") as fh:
        fh.truncate(size)

    ranges = []
    pos = 0
    while pos < size:
        ranges.append((pos, min(pos + CHUNK - 1, size - 1)))
        pos += CHUNK

    done = {"n": 0}
    failed = {"n": 0}
    lock = threading.Lock()
    total = len(ranges)

    def worker(bounds):
        s, e = bounds
        try:
            fetch_range(url, s, e, tmp)
        except Exception as exc:
            with lock:
                failed["n"] += 1
            log(f"  块 {s}-{e} 失败：{str(exc)[:60]}")
            return
        with lock:
            done["n"] += 1
            if done["n"] % max(total // 10, 1) == 0 or done["n"] == total:
                log(f"  {done['n']}/{total} 块")

    idx = 0
    while idx < total:
        batch = ranges[idx: idx + THREADS]
        threads = [threading.Thread(target=worker, args=(b,)) for b in batch]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        idx += THREADS
        if failed["n"]:
            break

    if not failed["n"] and tmp.stat().st_size == size:
        tmp.rename(dest)
        return True
    tmp.unlink(missing_ok=True)
    return False


def already_ok(dest: Path, size: int | None) -> bool:
    if not dest.exists():
        return False
    if size is None:
        return dest.stat().st_size > 1024
    return dest.stat().st_size == size


def handle(repo: str, remote: str, dest_rel: str) -> bool:
    dest = MODEL_DIR / dest_rel
    dest.parent.mkdir(parents=True, exist_ok=True)

    candidates = []
    for source in SOURCES:
        url = build_url(source, repo, remote)
        size = http_head(url)
        candidates.append({"source": source, "url": url, "size": size})

    known = next((c["size"] for c in candidates if c["size"]), None)
    if already_ok(dest, known):
        log(f"跳过 {dest_rel}（已存在 {dest.stat().st_size / 1e9:.2f}GB）")
        return True

    for cand in candidates:
        if not cand["size"]:
            log(f"  {cand['source']} 拿不到大小，跳过")
            continue
        cand["speed"] = probe(cand["url"])
        log(f"  测速 {cand['source']}: {cand['speed'] / 1e6:.1f} MB/s")

    usable = [c for c in candidates if c["size"] and c["speed"] > 0]
    if not usable:
        log(f"✗ {dest_rel} 所有源都不可用")
        return False
    usable.sort(key=lambda c: c["speed"], reverse=True)

    for cand in usable:
        log(f"下载 {dest_rel} {cand['size'] / 1e9:.2f}GB ← {cand['source']}"
            f"（{cand['speed'] / 1e6:.1f}MB/s × {THREADS} 线程）")
        if download_from(cand["url"], cand["size"], dest):
            log(f"✓ {dest_rel} 完成")
            return True
        log(f"  {cand['source']} 失败，换下一个源")
    log(f"✗ {dest_rel} 全部源失败")
    return False


def main() -> int:
    if not MANIFEST.exists():
        log(f"找不到清单 {MANIFEST}")
        return 1
    spec = json.loads(MANIFEST.read_text(encoding="utf-8"))
    files = spec.get("files", spec.get("models", []))
    todo = [f for f in files if OPTIONAL or not f.get("optional")]

    log(f"清单 {len(todo)} 个文件，线程 {THREADS}，源顺序 {','.join(SOURCES)}")
    ok = 0
    for entry in todo:
        dest_rel = entry.get("dest") or entry.get("target")
        repo = entry["repo"]
        remote = entry.get("path") or entry.get("file")
        try:
            if handle(repo, remote, dest_rel):
                ok += 1
        except Exception as exc:
            log(f"✗ {dest_rel} 异常：{str(exc)[:80]}")

    log(f"完成 {ok}/{len(todo)}")
    return 0 if ok == len(todo) else 2


if __name__ == "__main__":
    sys.exit(main())
