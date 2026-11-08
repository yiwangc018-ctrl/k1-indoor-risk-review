#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成“开始工作”的嘀嘀提示音；listen.wav 是单独的语音提示。

约定（2026-11-08 现场调整）：
    先说一句提示（listen.wav，部署时从
    外部提示语资源 listen.wav 复制到 audio/）
    → 说“可以开始了” → 识别通过 → “嘀嘀”（start.wav）→ 开始拍。
本脚本只生成 start.wav，不再覆盖 listen.wav；否则语音提示会被 0.1 秒单声替换。

用法：
    python3 scripts/board/make_cues.py           # 只写 start.wav 到 ~/rv/room_review/audio
    python3 scripts/board/make_cues.py --out /tmp/cues
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
import wave

import numpy as np

SAMPLE_RATE = 48000
# 0.9 是"数字满量程但不过载"的取值：之前 0.35 实测太轻（用户 2026-11-06 反馈）。
AMPLITUDE = 0.9
FADE_MS = 5

# 只保留 start：两声“嘀嘀”，表示识别通过、开始工作。
# listen.wav 是语音提示，不由本脚本生成。
CUE_SPECS = {
    "start": [(880, 110), (0, 60), (880, 110)],
}


def render(segments, rate=SAMPLE_RATE):
    """把 [(freq, ms)] 渲染成 float32 波形；freq<=0 表示静音。"""
    parts = []
    for frequency, milliseconds in segments:
        count = int(rate * milliseconds / 1000)
        if frequency <= 0:
            parts.append(np.zeros(count, dtype=np.float32))
            continue
        t = np.arange(count, dtype=np.float32) / rate
        tone = (AMPLITUDE * np.sin(2 * math.pi * frequency * t)).astype(np.float32)
        fade = min(int(rate * FADE_MS / 1000), count // 2)
        if fade > 0:
            ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
            tone[:fade] *= ramp
            tone[-fade:] *= ramp[::-1]
        parts.append(tone)
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def write_wav(path, waveform, rate=SAMPLE_RATE):
    pcm = np.clip(waveform, -1.0, 1.0)
    raw = (pcm * 32767.0).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(raw)
    return path


def build(out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, segments in CUE_SPECS.items():
        waveform = render(segments)
        path = write_wav(out_dir / (name + ".wav"), waveform)
        written.append((name, path, waveform.size / SAMPLE_RATE))
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成提示音（嘀嘀）")
    parser.add_argument("--out", type=Path, default=Path.home() / "rv/room_review/audio")
    args = parser.parse_args(argv)
    for name, path, seconds in build(args.out):
        print(f"{name:10} {seconds:.2f}s  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
