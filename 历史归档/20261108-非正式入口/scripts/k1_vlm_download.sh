#!/bin/bash
set -e
MODEL="Qwen3.5-0.8B.tar.gz"
BASE="http://archive.spacemit.com/spacemit-ai/model_zoo/vlm"
mkdir -p "$HOME/.cache/models/vlm"
cd "$HOME/.cache/models/vlm"
echo "[$(date +%H:%M:%S)] download start"
curl -sSL -O "$BASE/$MODEL"
curl -sSL -O "$BASE/$MODEL.md5"
ls -lh "$MODEL" "$MODEL.md5"
echo "[$(date +%H:%M:%S)] md5 check"
md5sum -c "$MODEL.md5" || echo "MD5_MISMATCH (continue)"
echo "[$(date +%H:%M:%S)] extract"
tar -xzf "$MODEL"
echo "[$(date +%H:%M:%S)] contents"
find . -maxdepth 3 \( -name 'config.json' -o -name '*.gguf' -o -name '*.onnx' \) -printf '%p %s\n'
echo "[$(date +%H:%M:%S)] done"
