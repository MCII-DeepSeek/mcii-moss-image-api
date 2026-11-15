#!/bin/bash
# Vast 的 entrypoint 只在 Jupyter 模式下起 supervisord，SSH 模式不会。
# 所以我们在这里自己拉起业务进程，再把控制权交回 Vast 的默认启动流程（sshd/portal）。
mkdir -p /var/log/moss
nohup /opt/moss/scripts/start-all.sh >>/var/log/moss/boot.log 2>&1 &
exec /opt/instance-tools/bin/boot_default.sh "$@"
