#!/bin/bash
# Vast 的 /.launch 每次开机都会执行 /root/onstart.sh（输出在 /var/log/onstart.log）。
# 这是唯一可靠的启动钩子 —— 基础镜像的 entrypoint / boot_custom.sh 在 Vast 上并不会被调用。
# 这里带保活把整个栈拉起来：下载权重 → ComfyUI → OpenAI 网关 → 反向隧道。
mkdir -p /var/log/moss

setsid bash -c '
  while true; do
    /opt/moss/scripts/start-all.sh >>/var/log/moss/boot.log 2>&1
    echo "[onstart $(date -u +%T)] start-all 退出，5 秒后重启" >>/var/log/moss/boot.log
    sleep 5
  done
' </dev/null >/dev/null 2>&1 &

echo "[onstart $(date -u +%T)] 已拉起业务栈，日志 /var/log/moss/"
