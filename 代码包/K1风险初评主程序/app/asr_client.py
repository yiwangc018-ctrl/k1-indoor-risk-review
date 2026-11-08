#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻 ASR 服务的客户端：需要时拉起服务，之后每条命令只等推理。

服务端见 scripts/board/asr_service.py。这里只管三件事：探活、按需拉起、发一条 wav 换文本。
所有失败都抛 AsrUnavailable，由调用方写成 command.json 的 failed，绝不静默通过。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

DEFAULT_SOCKET = "/tmp/room_asr.sock"


class AsrUnavailable(RuntimeError):
    pass


def _request(socket_path, payload, timeout):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(socket_path)
        client.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
    if not data.strip():
        raise AsrUnavailable("ASR 服务没有返回内容")
    return json.loads(data.split(b"\n", 1)[0].decode("utf-8"))


def ping(socket_path, timeout=1.0):
    try:
        return _request(socket_path, {"op": "ping"}, timeout)
    except (OSError, ValueError, AsrUnavailable):
        return None


def service_script():
    return Path(__file__).resolve().parents[1] / "scripts" / "board" / "asr_service.py"


def ensure_service(config, wait_seconds=None):
    """保证服务在跑：已在跑直接返回；否则拉起并等到 ping 通。"""
    asr = config["asr"]
    socket_path = asr.get("service_socket", DEFAULT_SOCKET)
    if ping(socket_path):
        return socket_path

    model_dir = Path(asr["model_dir"])
    model_file = model_dir / asr.get("model_name", "model_quant_optimized.onnx")
    if not model_file.is_file():
        raise AsrUnavailable(f"语音识别模型未安装：{model_file}")

    log_dir = Path(asr.get("log_dir") or Path(config.get("inbox", "/tmp")).parent / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "asr_service.log"
    wait_seconds = wait_seconds or float(asr.get("startup_timeout_seconds", 90))

    lock_path = Path(socket_path + ".spawn.lock")
    with _spawn_lock(lock_path):
        if ping(socket_path):
            return socket_path
        with log_path.open("ab") as log:
            subprocess.Popen(
                [sys.executable, str(service_script()),
                 "--socket", socket_path,
                 "--model-dir", str(model_dir),
                 "--model-name", asr.get("model_name", "model_quant_optimized.onnx"),
                 "--threads", str(asr.get("threads", 4))],
                stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if ping(socket_path):
                return socket_path
            time.sleep(0.5)
    raise AsrUnavailable(f"{wait_seconds:.0f}s 内没有等到 ASR 服务就绪；日志：{log_path}")


def _spawn_lock(path):
    from contextlib import contextmanager

    @contextmanager
    def holder():
        import fcntl

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    return holder()


def transcribe(config, wav_path, timeout=None):
    """把 wav 交给常驻服务，返回 {text, infer_seconds, model, ...}。"""
    asr = config["asr"]
    socket_path = ensure_service(config)
    timeout = timeout or float(asr.get("request_timeout_seconds", 120))
    try:
        reply = _request(socket_path, {"op": "transcribe", "wav": str(wav_path)}, timeout)
    except (OSError, ValueError) as exc:
        raise AsrUnavailable(f"{type(exc).__name__}: {exc}") from exc
    if not reply.get("ok"):
        raise AsrUnavailable(reply.get("error") or "ASR 服务返回失败")
    return reply
