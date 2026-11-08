#!/usr/bin/env bash
# 02_get_models.sh —— 取 Qwen3.5 2B/4B 的 GGUF 量化模型
# ⚠️ 模型仓库 ID 会变，先确认再跑：https://www.modelscope.cn/models?search=Qwen3.5
# 用法: bash 02_get_models.sh
set -euo pipefail
DEST="${DEST:-$HOME/models}"
mkdir -p "$DEST"; cd "$DEST"

# —— 按实际情况改这两行（去 ModelScope 搜 "Qwen3.5-2B-Instruct-GGUF" / "Qwen3.5-4B-Instruct-GGUF"）——
REPO_2B="${REPO_2B:-Qwen/Qwen3.5-2B-Instruct-GGUF}"
FILE_2B="${FILE_2B:-qwen3.5-2b-instruct-q8_0.gguf}"      # 2B 用 Q8_0（X60 支持）
REPO_4B="${REPO_4B:-Qwen/Qwen3.5-4B-Instruct-GGUF}"
FILE_4B="${FILE_4B:-qwen3.5-4b-instruct-q4_k_m.gguf}"    # 4B 用 Q4_K_M（X60 支持，约 2.5G）

# X60 核官方支持: Q4_0 / Q4_1 / Q4_K / Q8_0   不支持: Q5_0 Q5_1 Q5_K Q6_K
fetch(){ # $1=repo $2=file
  local repo="$1" file="$2" url
  if [ -f "$file" ] && [ -s "$file" ]; then echo "✔ 已存在 $file ($(du -h "$file" | cut -f1))"; return; fi
  url="https://www.modelscope.cn/models/${repo}/resolve/master/${file}"
  echo "↓ $url"
  if command -v modelscope >/dev/null 2>&1; then
    modelscope download --model "$repo" "$file" --local_dir "$DEST" || wget -c "$url" -O "$file"
  else
    wget -c "$url" -O "$file"
  fi
  [ -s "$file" ] || { echo "❌ 下载失败：仓库/文件名不对。去 ModelScope 复制真实文件名后改上面 4 个变量"; exit 1; }
  echo "✔ $file ($(du -h "$file" | cut -f1))"
}

fetch "$REPO_2B" "$FILE_2B"
fetch "$REPO_4B" "$FILE_4B"

echo
echo "== 校验（GGUF magic + 报告里要写的量化类型）"
for f in "$FILE_2B" "$FILE_4B"; do
  printf "%-45s %s  magic=%s\n" "$f" "$(du -h "$f" | cut -f1)" "$(head -c4 "$f" | tr -d '\0')"
done

echo
echo "下一步:"
echo "  llama-cli -m $DEST/$FILE_2B -p '你好，用一句话介绍 RISC-V' -n 64 -t 8"
echo "  bash 03_bench.sh $DEST/$FILE_2B 2b_baseline"
