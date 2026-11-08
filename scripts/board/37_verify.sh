#!/usr/bin/env bash
# 热态复核：连问三次，看延迟是否稳定在 1~2 秒
set -u
cd /home/biandu/rv || exit 1
for n in 1 42 2026; do
  echo "=== 输出数字$n"
  python3 app/ask.py "输出数字$n" 2>&1 | grep -E '模型|契约|显示'
done
