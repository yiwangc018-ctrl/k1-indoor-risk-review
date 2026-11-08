#!/bin/bash
# Reuse a healthy server; never kill existing model services.
set -eu
if curl -fsS --max-time 3 http://127.0.0.1:8093/health >/dev/null 2>&1; then
    echo 'Reusing VLM on 127.0.0.1:8093'
    exit 0
fi
if ss -ltnH 'sport = :8093' | grep -q .; then
    echo 'Port 8093 is occupied but unhealthy; inspect the existing service.' >&2
    exit 1
fi
mkdir -p "$HOME/k1demo/logs"
cd "$HOME/.cache/models/vlm/Qwen3.5-0.8B"
export OMP_NUM_THREADS=4
nohup llama-server -m ./qwen3_5vl_0.8b-text-q41.gguf \
    --media-backend smt --smt-config-dir . -t 4 -tb 4 -c 2048 -ngl 0 \
    --no-warmup --reasoning off --host 127.0.0.1 --port 8093 \
    > "$HOME/k1demo/logs/photo_vlm_server.log" 2>&1 < /dev/null &
server_pid=$!
if [ -n "${1:-}" ]; then printf '%s\n' "$server_pid" > "$1"; fi
for attempt in $(seq 1 45); do
    if curl -fsS --max-time 1 http://127.0.0.1:8093/health >/dev/null 2>&1; then
        echo "VLM ready, pid=$server_pid"
        exit 0
    fi
    kill -0 "$server_pid" 2>/dev/null || break
    sleep 1
done
tail -n 20 "$HOME/k1demo/logs/photo_vlm_server.log"
exit 1
