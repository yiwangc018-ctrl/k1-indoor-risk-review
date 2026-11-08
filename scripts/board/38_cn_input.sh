#!/usr/bin/env bash
# 中文输入验收：中文数字指令 + 纯中文句子 + 编码检查
set -u
cd /home/biandu/rv || exit 1

echo '=== 编码环境 ==='
echo "  LANG=$LANG  LC_ALL=${LC_ALL:-未设置}"
python3 -c 'import sys; print("  stdin/stdout 编码:", sys.stdin.encoding, sys.stdout.encoding)'

echo
echo '=== 中文数字指令 ==='
for q in "输出数字一百二十三" "输出数字三十五" "输出数字两千零二十六"; do
  echo "--- $q"
  python3 app/ask.py "$q" 2>&1 | grep -E '判定|模型|契约|显示'
done

echo
echo '=== 纯中文句子（应当拒绝，不调用模型）==='
echo "--- 你好"
python3 app/ask.py "你好" 2>&1 | grep -E '判定|契约'

echo
echo '=== 模拟终端逐行喂入（验证交互模式能处理中文 stdin）==='
printf '输出数字四十二\n' | python3 app/ask.py 2>&1 | grep -E '输入|模型|契约'
