#!/usr/bin/env bash
# 容器入口：先确保权重在位，再起 ComfyUI，等它就绪后起 OpenAI 网关。
set -euo pipefail

COMFY_DIR="${COMFY_DIR:-/opt/ComfyUI}"
MODEL_DIR="${MOSS_MODEL_DIR:-/models}"
COMFY_PORT="${COMFY_PORT:-8188}"
API_PORT="${MOSS_PORT:-8000}"
LOG_DIR="${MOSS_LOG_DIR:-/var/log/moss}"
mkdir -p "$LOG_DIR"

log() { printf '[entry %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

if [[ "${MOSS_SKIP_DOWNLOAD:-0}" != "1" ]]; then
  log "检查权重（缺失才下载，支持断点续传）"
  python3 /app/scripts/download_models.py || log "权重检查失败，继续启动（可能已有缓存）"
else
  log "MOSS_SKIP_DOWNLOAD=1，跳过权重检查"
fi

log "启动 ComfyUI :${COMFY_PORT}"
python3 "$COMFY_DIR/main.py" \
  --listen 127.0.0.1 --port "$COMFY_PORT" \
  --disable-auto-launch --preview-method none \
  >"$LOG_DIR/comfyui.log" 2>&1 &
COMFY_PID=$!

cleanup() {
  log "收到退出信号，停止子进程"
  kill "$COMFY_PID" 2>/dev/null || true
  [[ -n "${API_PID:-}" ]] && kill "$API_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup TERM INT

log "等 ComfyUI 就绪（最多 15 分钟，首次加载要读 15GB 权重）"
for _ in $(seq 1 180); do
  if curl -fsS "http://127.0.0.1:${COMFY_PORT}/system_stats" >/dev/null 2>&1; then
    log "ComfyUI 就绪"
    break
  fi
  if ! kill -0 "$COMFY_PID" 2>/dev/null; then
    log "ComfyUI 启动失败，日志尾部："
    tail -n 60 "$LOG_DIR/comfyui.log" || true
    exit 1
  fi
  sleep 5
done

if ! curl -fsS "http://127.0.0.1:${COMFY_PORT}/system_stats" >/dev/null 2>&1; then
  log "ComfyUI 15 分钟内没起来，退出"
  tail -n 80 "$LOG_DIR/comfyui.log" || true
  exit 1
fi

log "启动 OpenAI 网关 :${API_PORT}"
python3 /app/server.py \
  --host "${MOSS_HOST:-127.0.0.1}" --port "$API_PORT" \
  --comfy "http://127.0.0.1:${COMFY_PORT}" \
  --comfy-input "${COMFY_DIR}/input" \
  >"$LOG_DIR/api.log" 2>&1 &
API_PID=$!

# 可选：开机自动建反向隧道。MOSS_TUNNEL_HOST 给了才启动。
# 私钥用 base64 通过环境变量塞进来（MOSS_TUNNEL_KEY_B64），落盘后立刻 chmod 600。
if [[ -n "${MOSS_TUNNEL_HOST:-}" ]]; then
  KEY_PATH="${MOSS_SSH_KEY:-/root/.ssh/tunnel_key}"
  if [[ -n "${MOSS_TUNNEL_KEY_B64:-}" ]]; then
    mkdir -p "$(dirname "$KEY_PATH")"
    printf '%s' "$MOSS_TUNNEL_KEY_B64" | base64 -d > "$KEY_PATH"
    chmod 600 "$KEY_PATH"
    log "已写入隧道私钥 $KEY_PATH"
  fi
  if [[ -f "$KEY_PATH" ]]; then
    MOSS_ORANGE_HOST="$MOSS_TUNNEL_HOST" \
    MOSS_ORANGE_SSH_PORT="${MOSS_TUNNEL_PORT:-22}" \
    MOSS_ORANGE_USER="${MOSS_TUNNEL_USER:-root}" \
    MOSS_REMOTE_PORT="${MOSS_TUNNEL_REMOTE_PORT:-18010}" \
    MOSS_BIND="${MOSS_TUNNEL_BIND:-127.0.0.1}" \
    MOSS_LOCAL_PORT="$API_PORT" \
      nohup bash /app/scripts/tunnel.sh >>"$LOG_DIR/tunnel.log" 2>&1 &
    log "隧道守护已启动 → ${MOSS_TUNNEL_HOST}"
  else
    log "MOSS_TUNNEL_HOST 已设但没有私钥，跳过隧道"
  fi
fi

# 任意一个挂了就整体退出，交给 Docker 的 restart 策略
while true; do
  if ! kill -0 "$COMFY_PID" 2>/dev/null; then
    log "ComfyUI 退出，整体停止"
    tail -n 40 "$LOG_DIR/comfyui.log" || true
    exit 1
  fi
  if ! kill -0 "$API_PID" 2>/dev/null; then
    log "网关退出，整体停止"
    tail -n 40 "$LOG_DIR/api.log" || true
    exit 1
  fi
  sleep 10
done
