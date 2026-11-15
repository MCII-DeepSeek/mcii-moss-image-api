#!/usr/bin/env bash
# Supervisor 托管的主进程：权重检查 → ComfyUI → OpenAI 网关 →（可选）隧道。
# 任何一环挂掉就整体退出，让 Supervisor 重启，避免半死不活。
set -euo pipefail

. /venv/main/bin/activate 2>/dev/null || true

COMFY_DIR="${COMFY_DIR:-/opt/ComfyUI}"
MODEL_DIR="${MOSS_MODEL_DIR:-/models}"
COMFY_PORT="${COMFY_PORT:-8188}"
API_PORT="${MOSS_PORT:-8000}"
LOG_DIR="${MOSS_LOG_DIR:-/var/log/moss}"
mkdir -p "$LOG_DIR"

# 只允许一个 start-all 在跑：旧的没退出就等下一轮，避免两套 ComfyUI/网关/隧道互相抢端口
exec 9>"$LOG_DIR/start-all.lock"
if ! flock -n 9; then
  log "已有 start-all 在跑，退出本次"
  exit 0
fi

log() { printf '[moss %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

if [[ "${MOSS_SKIP_DOWNLOAD:-0}" != "1" ]]; then
  log "检查权重（缺失才下载）"
  python3 /opt/moss/scripts/download_models.py || log "权重检查未完全成功，继续启动"
else
  log "MOSS_SKIP_DOWNLOAD=1，跳过权重检查"
fi

log "启动 ComfyUI :${COMFY_PORT}"
python3 "$COMFY_DIR/main.py" \
  --listen 127.0.0.1 --port "$COMFY_PORT" \
  --disable-auto-launch --preview-method none \
  >"$LOG_DIR/comfyui.log" 2>&1 &
COMFY_PID=$!

trap 'kill $COMFY_PID ${API_PID:-} 2>/dev/null || true' TERM INT

log "等 ComfyUI 就绪（首次加载权重可能要几分钟）"
READY=0
for _ in $(seq 1 180); do
  if curl -fsS "http://127.0.0.1:${COMFY_PORT}/system_stats" >/dev/null 2>&1; then
    READY=1; break
  fi
  if ! kill -0 "$COMFY_PID" 2>/dev/null; then
    log "ComfyUI 退出，日志尾部："; tail -n 60 "$LOG_DIR/comfyui.log" || true; exit 1
  fi
  sleep 5
done
if [[ "$READY" != "1" ]]; then
  log "ComfyUI 15 分钟未就绪"; tail -n 80 "$LOG_DIR/comfyui.log" || true; exit 1
fi
log "ComfyUI 就绪"

log "启动 OpenAI 网关 :${API_PORT}"
python3 /opt/moss/app/server.py \
  --host "${MOSS_HOST:-127.0.0.1}" --port "$API_PORT" \
  --comfy "http://127.0.0.1:${COMFY_PORT}" \
  --comfy-input "${COMFY_DIR}/input" \
  >"$LOG_DIR/api.log" 2>&1 &
API_PID=$!

# 可选：反向隧道（MOSS_TUNNEL_HOST 给了才建）
if [[ -n "${MOSS_TUNNEL_HOST:-}" ]]; then
  KEY_PATH="${MOSS_SSH_KEY:-/root/.ssh/moss_tunnel_key}"
  if [[ -n "${MOSS_TUNNEL_KEY_B64:-}" ]]; then
    mkdir -p "$(dirname "$KEY_PATH")"
    printf '%s' "$MOSS_TUNNEL_KEY_B64" | base64 -d > "$KEY_PATH"
    chmod 600 "$KEY_PATH"
    log "已写入隧道私钥"
  fi
  pkill -f 'moss/scripts/tunnel[.]sh' 2>/dev/null || true
  if [[ -f "$KEY_PATH" ]]; then
    MOSS_ORANGE_HOST="$MOSS_TUNNEL_HOST" \
    MOSS_ORANGE_SSH_PORT="${MOSS_TUNNEL_PORT:-22}" \
    MOSS_ORANGE_USER="${MOSS_TUNNEL_USER:-root}" \
    MOSS_REMOTE_PORT="${MOSS_TUNNEL_REMOTE_PORT:-18010}" \
    MOSS_BIND="${MOSS_TUNNEL_BIND:-127.0.0.1}" \
    MOSS_LOCAL_PORT="$API_PORT" \
    MOSS_SSH_KEY="$KEY_PATH" \
      bash /opt/moss/scripts/tunnel.sh >>"$LOG_DIR/tunnel.log" 2>&1 &
    log "隧道守护已启动 → ${MOSS_TUNNEL_HOST}"
  else
    log "MOSS_TUNNEL_HOST 已设但没有私钥，跳过隧道"
  fi
fi

log "服务就绪：http://127.0.0.1:${API_PORT}/healthz"
wait -n "$COMFY_PID" "$API_PID"
log "有子进程退出，整体重启"
exit 1
