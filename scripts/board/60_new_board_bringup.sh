#!/bin/bash
# 新板一键恢复入口（K1 MUSE Pi Pro / Bianbu / 用户 biandu）
#
# 常用：
#   bash scripts/board/60_new_board_bringup.sh plan
#   bash scripts/board/60_new_board_bringup.sh install --bundle ~/rv/offline/bundle
#   bash scripts/board/60_new_board_bringup.sh verify
#
# 说明见 docs/换板恢复-20261108.md。
set -eu
cd "$(dirname "$0")/../.."
exec python3 scripts/board/new_board_bringup.py "$@"
