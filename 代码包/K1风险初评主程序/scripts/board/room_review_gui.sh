#!/bin/bash
# 桌面图标入口：板上没有 xterm，Terminal=true 的 .desktop 会直接报错，
# 所以由这个脚本在已安装的终端（qterminal）里启动菜单。
set -eu
cd /home/biandu/rv/room_review
exec bash scripts/board/start_room_review.sh
