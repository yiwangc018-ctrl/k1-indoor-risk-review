#!/bin/bash
# Restart the K1 VLM server with reasoning disabled and wait until healthy.
set -e
pkill -f llama-server 2>/dev/null || true
sleep 2
cd "$HOME/.cache/models/vlm/Qwen3.5-0.8B"
export OMP_NUM_THREADS=4
nohup llama-server \
  -m ./qwen3_5vl_0.8b-text-q41.gguf \
  --media-backend smt --smt-config-dir . \
  -t 4 -tb 4 -c 2048 -ngl 0 --no-warmup --reasoning off \
  --host 127.0.0.1 --port 8093 \
  > "$HOME/k1demo/logs/vlm_server.log" 2>&1 < /dev/null &
for i in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8093/health || true)
  if [ "$code" = "200" ]; then
    echo "ready after ${i}s"
    exit 0
  fi
  sleep 1
done
echo "NOT READY"
tail -n 20 "$HOME/k1demo/logs/vlm_server.log"
exit 1
