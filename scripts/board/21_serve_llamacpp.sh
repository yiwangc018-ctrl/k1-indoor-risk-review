#!/usr/bin/env bash
# =============================================================================
# 21_serve_llamacpp.sh —— 用 llama-server 把 GGUF 跑成 OpenAI 兼容服务
#
# 对应官方文档：竞赛教程/02_AI_大模型开发文档及案例集/01_基于K1_AI_CPU的大模型部署落地.md
#   「本赛题要求基于进迭开源的 Llama.cpp 进行部署」
# 前置：01_build_llamacpp.sh 已编译完（产物在 ~/llama/llama.cpp/build/installed/bin）
#
# 用法：
#   bash 21_serve_llamacpp.sh --status                      # 看现在有没有在跑
#   bash 21_serve_llamacpp.sh -m ~/models/qwen3-0.6b-q4_0.gguf
#   bash 21_serve_llamacpp.sh -m <模型> --threads 4 --ctx 4096 --port 8080
#   bash 21_serve_llamacpp.sh -m <模型> --systemd            # 装成开机自启（推荐比赛用）
#   bash 21_serve_llamacpp.sh --stop
#
# 变量：LLAMA_BIN 指定 llama-server 路径；HOST/PORT/THREADS/CTX 覆盖默认值
# =============================================================================
set -u

MODEL=""
PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"
THREADS="${THREADS:-4}"
CTX="${CTX:-4096}"
UBATCH="${UBATCH:-128}"
DO_SYSTEMD=0
ACTION="start"
PIDFILE="$HOME/.llama-server.pid"
LOGFILE="$HOME/llama-server.log"
UNIT=/etc/systemd/system/llama-server.service

ok(){   printf '  [ OK ] %s\n' "$*"; }
warn(){ printf '  [WARN] %s\n' "$*"; }
bad(){  printf '  [FAIL] %s\n' "$*"; }
hdr(){  printf '\n=== %s ===\n' "$*"; }

while [ $# -gt 0 ]; do
  if [ "$1" = "-m" ] || [ "$1" = "--model" ]; then
    MODEL="${2:-}"; shift 2
  elif [ "$1" = "--port" ]; then
    PORT="${2:-}"; shift 2
  elif [ "$1" = "--host" ]; then
    HOST="${2:-}"; shift 2
  elif [ "$1" = "--threads" ] || [ "$1" = "-t" ]; then
    THREADS="${2:-}"; shift 2
  elif [ "$1" = "--ctx" ] || [ "$1" = "-c" ]; then
    CTX="${2:-}"; shift 2
  elif [ "$1" = "--systemd" ]; then
    DO_SYSTEMD=1; shift
  elif [ "$1" = "--status" ]; then
    ACTION="status"; shift
  elif [ "$1" = "--stop" ]; then
    ACTION="stop"; shift
  elif [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    sed -n '2,22p' "$0"; exit 0
  else
    echo "未知参数: $1"; exit 2
  fi
done

# -----------------------------------------------------------------------------
find_llama_server(){
  if [ -n "${LLAMA_BIN:-}" ] && [ -x "$LLAMA_BIN" ]; then echo "$LLAMA_BIN"; return; fi
  if command -v llama-server >/dev/null 2>&1; then command -v llama-server; return; fi
  local c
  for c in \
    "$HOME/llama/llama.cpp/build/installed/bin/llama-server" \
    "$HOME/llama/llama.cpp/build/bin/llama-server" \
    "$HOME/llama/build/installed/bin/llama-server" ; do
    [ -x "$c" ] && { echo "$c"; return; }
  done
}

healthy(){ curl -sf "http://127.0.0.1:${PORT}/health" 2>/dev/null | grep -q '"ok"'; }

show_status(){
  hdr "llama-server 状态"
  local b; b="$(find_llama_server || true)"
  printf '  二进制: %s\n' "${b:-未找到（先跑 01_build_llamacpp.sh）}"
  if systemctl list-unit-files 2>/dev/null | grep -q '^llama-server\.service'; then
    printf '  systemd: %s\n' "$(systemctl is-active llama-server 2>/dev/null)"
  fi
  if [ -f "$PIDFILE" ]; then printf '  pid 文件: %s（pid=%s）\n' "$PIDFILE" "$(cat "$PIDFILE" 2>/dev/null)"; fi
  if healthy; then
    ok "服务在线 http://127.0.0.1:${PORT}"
    echo "         已加载模型："
    curl -s "http://127.0.0.1:${PORT}/v1/models" | head -c 400; echo
  else
    warn "端口 ${PORT} 上没有健康的 llama-server"
  fi
  [ -f "$LOGFILE" ] && { echo "  最近日志："; tail -5 "$LOGFILE" | sed 's/^/         /'; }
}

if [ "$ACTION" = "status" ]; then show_status; exit 0; fi

if [ "$ACTION" = "stop" ]; then
  hdr "停止服务"
  if systemctl list-unit-files 2>/dev/null | grep -q '^llama-server\.service'; then
    sudo systemctl stop llama-server && ok "systemd 已停"
  fi
  if [ -f "$PIDFILE" ]; then
    if kill "$(cat "$PIDFILE")" 2>/dev/null; then
      ok "已 kill $(cat "$PIDFILE")"
    else
      warn "pid 文件还在，但进程已经没了"
    fi
    rm -f "$PIDFILE"
  fi
  if pkill -f "llama-server.*--port ${PORT}" 2>/dev/null; then ok "清理残留进程"; fi
  exit 0
fi

# -----------------------------------------------------------------------------
hdr "1. 检查二进制与模型"
BIN="$(find_llama_server || true)"
if [ -z "$BIN" ]; then
  bad "找不到 llama-server。先编译： bash 01_build_llamacpp.sh"
  bad "编完路径一般在 ~/llama/llama.cpp/build/installed/bin/llama-server"
  exit 1
fi
ok "llama-server: $BIN"

if [ -z "$MODEL" ]; then
  bad "未指定模型。用 -m <xxx.gguf>，例如："
  ls -1 "$HOME/models"/*.gguf 2>/dev/null | sed 's/^/         /' || echo "         （~/models 下也没有 .gguf，先跑 02_get_models.sh）"
  exit 2
fi
[ -s "$MODEL" ] || { bad "模型不存在: $MODEL"; exit 2; }
magic=$(head -c 4 "$MODEL" 2>/dev/null | tr -d '\0')
[ "$magic" = "GGUF" ] || { bad "$MODEL 不是 GGUF（magic=$magic）"; exit 2; }
ok "模型: $MODEL ($(du -h "$MODEL" | cut -f1))"
printf '  线程=%s  上下文=%s  ubatch=%s  监听=%s:%s\n' "$THREADS" "$CTX" "$UBATCH" "$HOST" "$PORT"

ARGS=(-m "$MODEL" --host "$HOST" --port "$PORT" -t "$THREADS" -c "$CTX" -ub "$UBATCH")

# -----------------------------------------------------------------------------
hdr "2. 启动"
if healthy; then
  warn "端口 ${PORT} 上已经有服务在跑，先 --stop 再启动"
  exit 3
fi

if [ "$DO_SYSTEMD" = "1" ]; then
  echo "  安装 systemd 单元: $UNIT"
  sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=llama-server (K1 local LLM)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME
ExecStart=$BIN ${ARGS[*]}
Restart=always
RestartSec=5
# 留 2 个核给 ASR/CV，LLM 占 ${THREADS} 个（参赛指南 §5.2 的 CPU<=60% 要求）
Nice=5

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now llama-server || { bad "systemd 启动失败：journalctl -u llama-server -n 50"; exit 1; }
  ok "已装成开机自启（llama-server.service）"
else
  ( nohup "$BIN" "${ARGS[@]}" >"$LOGFILE" 2>&1 & echo $! >"$PIDFILE" )
  ok "后台启动，pid=$(cat "$PIDFILE")，日志: $LOGFILE"
fi

# -----------------------------------------------------------------------------
hdr "3. 等待模型加载（0.6B 约 5~15s，2B 约 20~50s）"
ready=0
for i in $(seq 1 60); do
  if healthy; then ready=1; break; fi
  sleep 2
done
if [ "$ready" = "1" ]; then ok "模型已加载，服务就绪"
else
  bad "120 秒内没就绪，看日志："
  tail -20 "$LOGFILE" 2>/dev/null | sed 's/^/         /'
  exit 1
fi

# -----------------------------------------------------------------------------
hdr "4. 冒烟测试（OpenAI 兼容接口）"
t0=$(date +%s%N)
resp=$(curl -s --max-time 180 "http://127.0.0.1:${PORT}/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"local\",\"messages\":[{\"role\":\"user\",\"content\":\"用一句话介绍你自己\"}],\"max_tokens\":40,\"temperature\":0}")
t1=$(date +%s%N)
if [ -z "$resp" ]; then
  bad "接口无响应"
else
  echo "$resp" | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)
    c=d["choices"][0]["message"]["content"].strip().replace("\n"," ")
    print("  回复:", c[:120])
    u=d.get("usage") or {}
    print("  tokens:", u.get("prompt_tokens"), "->", u.get("completion_tokens"))
except Exception as e:
    print("  [WARN] 解析失败:", e)
    print("  原文:", sys.stdin.read()[:200])
'
  echo "  端到端: $(( (t1-t0)/1000000 )) ms（含排队；测速用 llama-bench 才准）"
fi

# -----------------------------------------------------------------------------
hdr "5. 用法"
cat <<EOF
  OpenAI base_url : http://127.0.0.1:${PORT}/v1
  局域网访问      : http://<板子IP>:${PORT}/v1   （HOST=0.0.0.0 已放开）

  curl 直调：
    curl -s http://127.0.0.1:${PORT}/v1/chat/completions -H 'Content-Type: application/json' \\
      -d '{"model":"local","messages":[{"role":"user","content":"向左转"}],"max_tokens":32}'

  Python（openai 库）：
    from openai import OpenAI
    c = OpenAI(base_url="http://127.0.0.1:${PORT}/v1", api_key="none")
    print(c.chat.completions.create(model="local", messages=[{"role":"user","content":"你好"}]))

  指令→JSON 合法率 + TTFT：
    python3 llm_decision_bench.py --backend openai --host http://127.0.0.1:${PORT} --model local

  报告口径：性能数据请用 03_bench.sh（llama-bench），别用这个 curl 的端到端耗时。
EOF
