#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻 ASR 服务：模型只加载一次，之后每句只做推理。

为什么需要常驻
--------------
SenseVoice 在 K1 上**加载模型约 30 秒**、单句推理 0.8–1.9 秒。如果每次语音命令都起一个新进程，
30 秒加载会把"识别快"这个唯一的优势吃光（比 whisper 的 25 秒还慢）。所以模型留在内存里。

协议：Unix socket，一行一个 JSON。
    {"op":"ping"}                        -> {"ready":true,"model":...,"load_seconds":...}
    {"op":"transcribe","wav":"/path.wav"} -> {"ok":true,"text":...,"infer_seconds":...}
    {"op":"shutdown"}                    -> {"ok":true}
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))

from asr_sensevoice import SenseVoiceRecognizer  # noqa: E402


def handle(conn, recognizer, state):
    buffer = b""
    conn.settimeout(120)
    while True:
        try:
            chunk = conn.recv(65536)
        except (socket.timeout, ConnectionResetError):
            return
        if not chunk:
            return
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            reply = dispatch(line, recognizer, state)
            conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
            if reply.get("shutdown"):
                state["stop"] = True
                return


def dispatch(line, recognizer, state):
    started = time.perf_counter()
    try:
        request = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return {"ok": False, "error": f"bad json: {exc}"}
    op = request.get("op")
    try:
        if op == "ping":
            return {"ready": True, "model": recognizer.model_name,
                    "load_seconds": round(recognizer.load_seconds, 3)}
        if op == "shutdown":
            return {"ok": True, "shutdown": True}
        if op == "transcribe":
            wav = request.get("wav")
            if not wav or not Path(wav).is_file():
                return {"ok": False, "error": f"wav 不存在：{wav}"}
            result = recognizer.transcribe_file(wav)
            result["ok"] = True
            result["handled_seconds"] = time.perf_counter() - started
            return result
        return {"ok": False, "error": f"未知 op：{op}"}
    except Exception as exc:  # 一条请求失败不能拖垮服务
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="常驻 SenseVoice ASR 服务")
    parser.add_argument("--socket", default="/tmp/room_asr.sock")
    parser.add_argument("--model-dir", type=Path, default=Path.home() / "rv/asr/sensevoice/sensevoice")
    parser.add_argument("--model-name", default="model_quant_optimized.onnx")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args(argv)

    recognizer = SenseVoiceRecognizer(args.model_dir, args.model_name, threads=args.threads)
    recognizer.warmup()
    socket_path = Path(args.socket)
    if socket_path.exists():
        socket_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    server.listen(4)
    state = {"stop": False}
    lock = threading.Lock()

    def stop(signum, frame):
        state["stop"] = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"ASR 服务就绪：{socket_path} 模型 {args.model_name} 加载 {recognizer.load_seconds:.1f}s", flush=True)

    server.settimeout(1.0)
    while not state["stop"]:
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        with conn, lock:
            handle(conn, recognizer, state)
    server.close()
    if socket_path.exists():
        socket_path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
