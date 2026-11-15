#!/bin/bash
# Vast 的 entrypoint 只在 Jupyter 模式下起 supervisord，SSH 模式不会。
# 这里自己拉起业务进程（带保活），再把控制权交回 Vast 的默认启动流程（sshd/portal）。
mkdir -p /var/log/moss
nohup bash -c '
  while true; do
    /opt/moss/scripts/start-all.sh
    echo "[boot $(date -u +%T)] start-all 退出，5 秒后重启"
    sleep 5
  done
' >>/var/log/moss/boot.log 2>&1 &
exec /opt/instance-tools/bin/boot_default.sh "$@"
