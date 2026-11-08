#!/bin/bash
# Source v1.5.5 and its generic objects must already exist under room_review/asr.
# Keep the generic executable for comparison. This creates a separate main-rvv.
set -eu
cd /home/biandu/rv/room_review/asr/whisper.cpp-1.5.5
cp ../../scripts/board/room_asr_rvv.h ./room_asr_rvv.h
python3 ../../scripts/board/prepare_room_asr_rvv.py
cc -I. -O2 -fno-strict-aliasing -march=rv64gcv ../../scripts/board/test_room_asr_rvv.c -lm -o test-room-rvv
./test-room-rvv
cc -I. -O2 -fno-strict-aliasing -march=rv64gcv -DNDEBUG -std=c11 -fPIC \
  -D_XOPEN_SOURCE=600 -D_GNU_SOURCE -pthread -c ggml-room-rvv.c -o ggml-rvv.o
g++ -I. -I./examples -O0 -DNDEBUG -std=c++11 -fPIC -D_XOPEN_SOURCE=600 \
  -D_GNU_SOURCE -pthread examples/main/main.cpp examples/common.cpp \
  examples/common-ggml.cpp examples/grammar-parser.cpp ggml-rvv.o ggml-alloc.o \
  ggml-backend.o ggml-quants.o whisper.o -o main-rvv
echo 'RVV build finished'
