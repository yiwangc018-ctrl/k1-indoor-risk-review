#!/bin/bash
# 常驻 0.8B VLM（端口 8099），让分析不再每次付 13–16 秒冷启动。
# 已健康且模型身份一致时直接复用；端口被别的服务占用则报错，不动它。
set -eu

PORT=8099
MODEL=/home/biandu/.cache/models/vlm/Qwen3.5-0.8B/qwen3_5vl_0.8b-text-q41.gguf
BIN=/home/biandu/rv/validation_20261103/runtime_archives/spacemit-llama.cpp.riscv64.0.1.7/bin/llama-server
SMTDIR=/home/biandu/rv/room_review/configs/vlm_smt_0.8b_224
LOG=/home/biandu/rv/room_review/logs/vlm_server.log
export LD_LIBRARY_PATH=/home/biandu/rv/validation_20261103/runtime_archives/spacemit-llama.cpp.riscv64.0.1.7/lib:/home/biandu/rv/validation_20261103/runtime_archives/spacemit-ort.riscv64.2.0.6/lib

mkdir -p "$SMTDIR" "$(dirname "$LOG")"

python3 - "$SMTDIR" <<'PY'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1]) / "config.json"
src = Path("/home/biandu/.cache/models/vlm/Qwen3.5-0.8B/config.json")
cfg = json.loads(src.read_text())
for key in ("vision_model", "text_model"):
    path = Path(cfg[key]["model_path"])
    cfg[key]["model_path"] = str(path if path.is_absolute() else (src.parent / path).resolve())
vision = Path(cfg["vision_model"]["model_path"])
cfg["vision_model"]["model_path"] = str(vision.with_name(vision.name.replace("-384-", "-224-")))
cfg["vision_model"]["input_size"] = 224
out.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
PY

healthy() {
    curl -fsS --max-time 1 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 || return 1
    curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/v1/models" 2>/dev/null \
        | grep -q "qwen3_5vl_0.8b-text-q41.gguf" || return 1
}

healthy && exit 0

if ss -ltnH "sport = :${PORT}" | grep -q .; then
    echo "端口 ${PORT} 被其它服务占用，未启动常驻 VLM" >&2
    exit 1
fi

nohup "$BIN" -m "$MODEL" --media-backend smt --smt-config-dir "$SMTDIR" \
    -t 4 -tb 4 -c 2048 -np 1 --no-warmup --reasoning off \
    --host 127.0.0.1 --port "$PORT" -ngl 0 > "$LOG" 2>&1 < /dev/null &
pid=$!
for _ in $(seq 1 60); do
    if healthy; then
        echo "VLM 常驻服务就绪 pid=${pid}"
        exit 0
    fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
done
tail -n 20 "$LOG" >&2
exit 1
