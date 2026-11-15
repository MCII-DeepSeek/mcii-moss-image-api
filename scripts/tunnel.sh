#!/usr/bin/env bash
# 在算力机（Vast 实例）上跑：把本机 8000 反向隧道到橙子，橙子那边再给 sub2api 用。
# 不对外开任何公网端口，也不必给算力机配域名和证书。
set -uo pipefail

ORANGE_HOST="${MOSS_ORANGE_HOST:-92.113.124.35}"
ORANGE_PORT="${MOSS_ORANGE_SSH_PORT:-20010}"
ORANGE_USER="${MOSS_ORANGE_USER:-root}"
SSH_KEY="${MOSS_SSH_KEY:-/root/.ssh/tunnel_key}"
REMOTE_PORT="${MOSS_REMOTE_PORT:-18010}"
LOCAL_PORT="${MOSS_LOCAL_PORT:-8000}"
BIND_ADDR="${MOSS_BIND:-127.0.0.1}"
LOG="${MOSS_TUNNEL_LOG:-/var/log/moss/tunnel.log}"

mkdir -p "$(dirname "$LOG")"
log() { printf '[%s] %s\n' "$(date -u '+%F %T')" "$*" | tee -a "$LOG"; }

SSH_OPTS=(
  -N -T -C
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o ConnectTimeout=15
  -o BatchMode=yes
  -o ServerAliveInterval=20
  -o ServerAliveCountMax=3
  -o ExitOnForwardFailure=yes
  -i "$SSH_KEY"
  -p "$ORANGE_PORT"
)

# 重连前先清掉对端残留的监听，否则 -R 会一直报 failed for listen port
clean_remote() {
  [[ -n "${MOSS_TUNNEL_CLEAN_CMD:-}" ]] || return 0
  timeout 20 ssh -T -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o BatchMode=yes -i "$SSH_KEY" -p "$ORANGE_PORT" \
    "${ORANGE_USER}@${ORANGE_HOST}" "${MOSS_TUNNEL_CLEAN_CMD}" >/dev/null 2>&1 || true
}

log "隧道启动：${BIND_ADDR}:${REMOTE_PORT}（橙子）→ 127.0.0.1:${LOCAL_PORT}（本机）"
while true; do
  clean_remote
  ssh "${SSH_OPTS[@]}" -R "${BIND_ADDR}:${REMOTE_PORT}:127.0.0.1:${LOCAL_PORT}" \
    "${ORANGE_USER}@${ORANGE_HOST}" >>"$LOG" 2>&1
  log "ssh 退出（码 $?），5 秒后重连"
  sleep 5
done
