#!/bin/bash
# 旧入口：Gimbal/Hardware 路径，会清零 PCA。
# 2026-11-08 交接包的 30 秒流程用 raw I2C（CH15/CH11），不要用本入口跑姿态流程；
# 见 docs/云台30秒流程与接入-20261108.md。
# 相机必须固定在云台上，否则四张是同一个画面（代码不检查安装方式）。
set -eu
cd /home/biandu/rv/room_review
exec python3 app/gimbal_scan.py "$@"
