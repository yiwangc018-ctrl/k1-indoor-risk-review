#!/bin/bash
set -eu
cd /home/biandu/rv/room_review/asr/whisper.cpp-1.5.5
# Reuse existing vendor-board objects; do not rebuild the model backend.
g++ -I. -O1 -std=c++11 -pthread ../../scripts/board/room_asr_command.cpp \
  ggml-rvv.o ggml-alloc.o ggml-backend.o ggml-quants.o whisper.o -o room-asr
echo 'Short-command ASR runner built'
