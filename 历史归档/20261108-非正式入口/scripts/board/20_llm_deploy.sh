#!/usr/bin/env bash
# =============================================================================
# 20_llm_deploy.sh —— K1 板端本地大模型（Ollama）部署 + 冒烟 + 测速
#
# 依据：
#   官方《大模型部署指南》(竞赛教程 02 / 03)
#   官方案例 voice-control-servo/spacemit_audio/ollama.sh（模型都放在 archive.spacemit.com）
#
# 用法：
#   bash 20_llm_deploy.sh --check   # 只看现状，不装、不下载
#   bash 20_llm_deploy.sh           # 装 toolkit → 取 0.5B 与 0.5B-FC → create → 起服务 → 冒烟
#   bash 20_llm_deploy.sh --agv     # 额外创建 AGV 版 FC 模型 qwen2.5-0.5b-agv-fc
#   bash 20_llm_deploy.sh --no-install  # 已装好 toolkit，只取模型/建模型
#
# 环境变量：
#   MODEL_DIR   模型与 Modelfile 存放目录，默认 ~/models
#   MODEL_BASE  模型源站，默认 https://archive.spacemit.com/spacemit-ai
#
# 说明：
#   * 0.5B-Q4 约 400MB，8GB 内存跑起来无压力，不需要开 swap
#   * Ollama 只用于赛题 1 的"LLM 决策"；赛题 2 的性能测试必须用 spacemit-com/llama.cpp
#   * Windows 拷过去的脚本先转行尾：sed -i 's/\r$//' 20_llm_deploy.sh
# =============================================================================
set -u

MODE_CHECK=0
DO_INSTALL=1
DO_AGV=0
for a in "$@"; do
  if [ "$a" = "--check" ] || [ "$a" = "check" ]; then
    MODE_CHECK=1
  elif [ "$a" = "--no-install" ]; then
    DO_INSTALL=0
  elif [ "$a" = "--agv" ]; then
    DO_AGV=1
  elif [ "$a" = "-h" ] || [ "$a" = "--help" ]; then
    sed -n '2,25p' "$0"; exit 0
  else
    echo "未知参数: $a（可用: --check / --no-install / --agv）"; exit 2
  fi
done

MODEL_DIR="${MODEL_DIR:-$HOME/models}"
MODEL_BASE="${MODEL_BASE:-https://archive.spacemit.com/spacemit-ai}"
API="http://127.0.0.1:11434"

CHAT_GGUF="Qwen2.5-0.5B-Instruct-Q4_0.gguf"
CHAT_MF="qwen2.5:0.5b.modelfile"
CHAT_NAME="qwen2.5:0.5b"

FC_GGUF="qwen2.5-0.5b-f16-elephant-fc-Q4_0.gguf"
FC_MF="qwen2.5-0.5b-elephant-fc.modelfile"
FC_NAME="qwen2.5-0.5b-fc"

AGV_GGUF="qwen2.5-0.5b-f16-agv-fc-Q4_0.gguf"
AGV_MF="qwen2.5-0.5b-agv-fc.modelfile"
AGV_NAME="qwen2.5-0.5b-agv-fc"

ok(){   printf '  [ OK ] %s\n' "$*"; }
warn(){ printf '  [WARN] %s\n' "$*"; }
bad(){  printf '  [FAIL] %s\n' "$*"; }
hdr(){  printf '\n=== %s ===\n' "$*"; }

# -----------------------------------------------------------------------------
hdr "1. 板卡与资源"
printf '  arch        : %s\n' "$(uname -m)"
printf '  kernel      : %s\n' "$(uname -r)"
[ -r /proc/device-tree/model ] && printf '  model       : %s\n' "$(tr -d '\0' < /proc/device-tree/model)"
printf '  mem total   : %s\n' "$(free -h | awk '/^Mem:/{print $2}')"
printf '  mem avail   : %s\n' "$(free -h | awk '/^Mem:/{print $7}')"
printf '  disk free / : %s\n' "$(df -h / | awk 'NR==2{print $4}')"
printf '  cpus        : %s\n' "$(nproc)"

if [ "$(uname -m)" != "riscv64" ]; then
  bad "不是 riscv64：spacemit-ollama-toolkit 只有 RISC-V 版。请在板子上跑，或改用 Bianbu Cloud 实例。"
  exit 1
fi

MEM_MB=$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo)
if [ "${MEM_MB:-0}" -ge 7500 ]; then ok "内存 ${MEM_MB}MB：0.5B/1.5B 随便跑，3B 也能试"
elif [ "${MEM_MB:-0}" -ge 3500 ]; then warn "内存 ${MEM_MB}MB：只跑 0.5B，别碰 4B"
else bad "内存仅 ${MEM_MB}MB，先关掉占内存的进程"; fi

AVAIL_G=$(df -BG / | awk 'NR==2{gsub("G","",$4); print $4}')
if [ "${AVAIL_G:-0}" -ge 5 ]; then ok "根分区可用 ${AVAIL_G}G（两个 0.5B 模型约 1.2G）"
else warn "根分区仅 ${AVAIL_G}G，可能不够；可先清清 apt 缓存: sudo apt clean"; fi
command -v wget >/dev/null && ok "wget 就绪" || warn "缺 wget：sudo apt install -y wget"

# -----------------------------------------------------------------------------
hdr "2. Ollama 现状"
if command -v ollama >/dev/null 2>&1; then
  ok "ollama 已安装: $(command -v ollama)"
  ollama --version 2>&1 | head -1 | sed 's/^/         /'
  if curl -sf "$API/api/tags" >/dev/null 2>&1; then
    ok "服务在线（$API）"
    echo "         已有模型："
    ollama list 2>/dev/null | sed 's/^/         /'
  else
    warn "服务未响应（$API），稍后由本脚本拉起"
  fi
else
  warn "ollama 未安装（apt 包名: spacemit-ollama-toolkit）"
fi

if [ "$MODE_CHECK" = "1" ]; then
  hdr "自检完成（未做任何修改）"
  echo "  确认无误后运行： bash $0"
  echo "  预计：apt 装包 1~2 分钟；下载两个模型各约 400MB；create 每个 5~20 秒。"
  exit 0
fi

# -----------------------------------------------------------------------------
hdr "3. 安装 spacemit-ollama-toolkit"
if ! command -v ollama >/dev/null 2>&1; then
  if [ "$DO_INSTALL" = "0" ]; then
    bad "指定了 --no-install，但系统里没有 ollama，退出"
    exit 1
  fi
  if apt-cache show spacemit-ollama-toolkit >/dev/null 2>&1; then
    ok "apt 源里有 spacemit-ollama-toolkit"
  else
    warn "apt 源里暂时看不到它，先 apt update 再试（需要联网）"
  fi
  sudo apt-get update || warn "apt update 失败，继续尝试安装"
  if sudo apt-get install -y spacemit-ollama-toolkit; then
    ok "安装完成: $(command -v ollama 2>/dev/null || echo '未找到 ollama 命令')"
  else
    bad "安装失败。检查网络后重试，或手动：sudo apt install spacemit-ollama-toolkit"
    bad "官方文档：竞赛教程/02_AI_大模型开发文档及案例集/03_大模型部署指南.md"
    exit 1
  fi
else
  ok "已安装，跳过"
fi
command -v ollama >/dev/null 2>&1 || { bad "仍然没有 ollama 命令，退出"; exit 1; }

# 版本：0.0.8 起支持 q4_K_M/q4_1 直接 pull（本脚本走本地 create，不依赖它）
VER=$(ollama --version 2>/dev/null | head -1)
printf '  版本输出: %s\n' "${VER:-未知}"

# -----------------------------------------------------------------------------
hdr "4. 拉起 ollama 服务"
started=0
if curl -sf "$API/api/tags" >/dev/null 2>&1; then
  ok "服务已在运行"
else
  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files 2>/dev/null | grep -q '^ollama\.service'; then
    sudo systemctl enable --now ollama >/dev/null 2>&1 && started=1 || warn "systemctl 启动失败，改用前台后台进程"
  fi
  if [ "$started" = "0" ]; then
    ( nohup ollama serve >"$HOME/ollama-serve.log" 2>&1 & ) && started=1
    warn "已用 nohup 后台启动，日志: $HOME/ollama-serve.log"
  fi
  for i in $(seq 1 20); do
    curl -sf "$API/api/tags" >/dev/null 2>&1 && break
    sleep 1
  done
  if curl -sf "$API/api/tags" >/dev/null 2>&1; then ok "服务已就绪（$API）"
  else bad "服务起不来：看 $HOME/ollama-serve.log，或 systemctl status ollama"; exit 1; fi
fi

# -----------------------------------------------------------------------------
hdr "5. 下载模型文件（源站 archive.spacemit.com）"
mkdir -p "$MODEL_DIR" || exit 1
cd "$MODEL_DIR" || exit 1

fetch(){ # $1=文件名
  local f="$1" url="$MODEL_BASE/$1"
  if [ -s "$f" ]; then
    ok "已存在 $f ($(du -h "$f" | cut -f1))"
    return 0
  fi
  echo "  ↓ $url"
  wget -c --no-check-certificate "$url" -O "$f" 2>/dev/null \
    || curl -L --insecure -o "$f" "$url" \
    || { bad "下载失败: $url"; return 1; }
  if [ -s "$f" ]; then ok "下载完成 $f ($(du -h "$f" | cut -f1))"
  else bad "文件为空: $f"; return 1; fi
}

fetch "$CHAT_GGUF" && fetch "$CHAT_MF" || { bad "基础模型文件没下全，退出"; exit 1; }
fetch "$FC_GGUF" && fetch "$FC_MF" || warn "FC 模型文件没下全，后面可以重跑本脚本续传"

# GGUF 文件头校验（前 4 字节应为 GGUF）
for f in "$CHAT_GGUF" "$FC_GGUF"; do
  [ -s "$f" ] || continue
  magic=$(head -c 4 "$f" 2>/dev/null | tr -d '\0')
  if [ "$magic" = "GGUF" ]; then ok "$f magic=GGUF"
  else warn "$f 头部不是 GGUF（magic=$magic），文件可能坏了，删掉重下"; fi
done

# -----------------------------------------------------------------------------
hdr "6. ollama create 建模"
create(){
  local name="$1" mf="$2"
  [ -s "$mf" ] || { warn "缺 Modelfile: $mf"; return 1; }
  if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$name"; then
    ok "$name 已存在"
    return 0
  fi
  if ollama create "$name" -f "$mf"; then ok "创建 $name"; else bad "创建 $name 失败"; return 1; fi
}

create "$CHAT_NAME" "$CHAT_MF" || warn "chat 模型创建失败"
create "$FC_NAME"   "$FC_MF"   || warn "FC 模型创建失败（LLM 决策要用它）"
[ "$DO_AGV" = "1" ] && { fetch "$AGV_GGUF" && fetch "$AGV_MF" && create "$AGV_NAME" "$AGV_MF"; }

cd "$HOME" || true

# -----------------------------------------------------------------------------
hdr "7. 冒烟测试"
smoke(){
  local model="$1"
  local t0 t1 out
  t0=$(date +%s%N)
  out=$(curl -s --max-time 180 "$API/api/chat" -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"用一句话介绍你自己\"}],\"stream\":false,\"options\":{\"num_predict\":40}}")
  t1=$(date +%s%N)
  if [ -z "$out" ]; then bad "$model 无响应"; return 1; fi
  echo "$out" | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print("  [FAIL] 返回不是 JSON"); sys.exit(1)
msg=(d.get("message") or {}).get("content","").strip().replace("\n"," ")
print("  回复:", msg[:120])
ec=d.get("eval_count") or 0
ed=(d.get("eval_duration") or 0)/1e9
if ec and ed: print("  速度: %.2f tok/s（eval %d tokens）"%(ec/ed, ec))
' || return 1
  echo "  总耗时: $(( (t1-t0)/1000000 )) ms"
}

if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$CHAT_NAME"; then
  echo "  -- $CHAT_NAME"; smoke "$CHAT_NAME"
fi
if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$FC_NAME"; then
  echo "  -- $FC_NAME"; smoke "$FC_NAME"
fi

# -----------------------------------------------------------------------------
hdr "8. 完成，下一步"
MODELS_BUILT="$CHAT_NAME / $FC_NAME"
[ "$DO_AGV" = "1" ] && MODELS_BUILT="$MODELS_BUILT / $AGV_NAME"
cat <<EOF
  模型目录 : $MODEL_DIR
  服务地址 : $API   （把 127.0.0.1 换成本机 IP 就变成"局域网可调"的 OpenAI 兼容接口）
  已建模型 : $MODELS_BUILT

  1) 交互试玩
       ollama run $CHAT_NAME

  2) OpenAI 兼容接口冒烟
       curl -s $API/v1/chat/completions -H 'Content-Type: application/json' \\
         -d '{"model":"$CHAT_NAME","messages":[{"role":"user","content":"你好"}]}' | head -c 400

  3) 指令→JSON 的合法率/延迟测试（报告里那 10 句就靠它）
       python3 llm_decision_bench.py --model $FC_NAME

  4) 放进你的主链路时记住（参赛指南 §5.2）：
       LLM 只能做兜底，不能进 1 秒关键路径；规则命中先走，LLM 超时直接回退。
EOF
