#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SenseVoice-Small 本地 ASR（板端常驻），不依赖 kaldi_native_fbank / soundfile / scipy。

为什么要自己写前端
------------------
官方 spacemit 包（`archive.spacemit.com/.../sensevoice.tar.gz`）配的参考代码需要
`kaldi_native_fbank`（板上没有 riscv64 轮子，pip 无法构建）、`soundfile`+`scipy`（只用来读 wav
和重采样，用标准库 `wave` 即可），以及 `libortextensions.so.0`（板上不存在，导致
`sensevoice_decoder_model.onnx` 加载失败，报 `ai.onnx.contrib:SentencepieceDecoder` 未注册）。
所以这里把三件事都换成纯 numpy/标准库实现：

* 特征：kaldi 风格 80 维 fbank（16k、25ms 窗、10ms 移、hamming、去直流、预加重 0.97）
  → LFR(7,6) → 560 维，与模型输入 `speech[:, :, 560]` 对齐；不做 CMVN（config.yaml 的
  `cmvn_file` 为 null，参考实现里 CMVN 也被注释掉）。
* 分词：直接用模型目录里的 `tokens.txt`（行号即 token id），CTC 贪心解码后拼字符串。
* 解码：CTC 贪心（argmax → 去重 → 去 blank 0），特殊标记 `<|...|>` 全部剥掉。

边界：dither 固定 0（确定性，便于复现）；官方参考实现默认 dither=1.0。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import time
import wave

import numpy as np

SAMPLE_RATE = 16000
FRAME_LENGTH = 400        # 25 ms
FRAME_SHIFT = 160         # 10 ms
NUM_MELS = 80
LFR_M = 7
LFR_N = 6
FEAT_DIM = NUM_MELS * LFR_M  # 560
PREEMPH = 0.97
FFT_SIZE = 512
FLOAT_EPS = float(np.finfo(np.float32).eps)
BLANK_ID = 0
LANGUAGE_ZH = 3
TEXTNORM_WITHITN = 14
_SPECIAL = re.compile(r"<\|[^|]*\|>")


def read_wav(path):
    """读 16k 单声道 16bit wav，返回 float32 [-1, 1]。"""
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != SAMPLE_RATE:
            raise ValueError(f"采样率必须是 {SAMPLE_RATE}，实际 {handle.getframerate()}")
        if handle.getnchannels() != 1:
            raise ValueError(f"必须是单声道，实际 {handle.getnchannels()} 声道")
        if handle.getsampwidth() != 2:
            raise ValueError(f"必须是 16bit，实际 {handle.getsampwidth() * 8}bit")
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _mel_scale(freqs):
    return 1127.0 * np.log(1.0 + freqs / 700.0)


def mel_filterbank(num_bins=NUM_MELS, fft_size=FFT_SIZE, fs=SAMPLE_RATE, low_freq=20.0):
    """kaldi MelBanks 的等价实现（htk_mode=false：三角滤波在 mel 域等分）。"""
    num_fft_bins = fft_size // 2 + 1
    freqs = np.arange(num_fft_bins) * fs / fft_size
    mel = _mel_scale(freqs)
    mel_low = _mel_scale(np.array([low_freq]))[0]
    mel_high = _mel_scale(np.array([fs / 2.0]))[0]
    delta = (mel_high - mel_low) / (num_bins + 1)
    left = mel_low + np.arange(num_bins) * delta
    center = left + delta
    right = center + delta
    banks = np.zeros((num_bins, num_fft_bins), dtype=np.float64)
    for i in range(num_bins):
        up = (mel - left[i]) / (center[i] - left[i])
        down = (right[i] - mel) / (right[i] - center[i])
        banks[i] = np.maximum(0.0, np.minimum(up, down))
    return banks


_MEL_BANKS = mel_filterbank()
_WINDOW = 0.54 - 0.46 * np.cos(2.0 * math.pi * np.arange(FRAME_LENGTH) / (FRAME_LENGTH - 1))


def compute_fbank(waveform):
    """返回 (frames, 80) float32；帧数与 kaldi snip_edges=true 一致。"""
    samples = np.asarray(waveform, dtype=np.float32).reshape(-1)
    if samples.size < FRAME_LENGTH:
        return np.zeros((0, NUM_MELS), dtype=np.float32)
    num_frames = 1 + (samples.size - FRAME_LENGTH) // FRAME_SHIFT
    index = np.arange(FRAME_LENGTH)[None, :] + FRAME_SHIFT * np.arange(num_frames)[:, None]
    frames = samples[index].astype(np.float64)
    frames -= frames.mean(axis=1, keepdims=True)          # remove_dc_offset
    frames *= _WINDOW                                    # hamming
    frames[:, 1:] -= PREEMPH * frames[:, :-1]            # preemph（与 kaldi 同序）
    frames[:, 0] *= 1.0 - PREEMPH
    spectrum = np.fft.rfft(frames, n=FFT_SIZE, axis=1)
    power = spectrum.real ** 2 + spectrum.imag ** 2
    mel = power @ _MEL_BANKS.T
    return np.log(np.maximum(mel, FLOAT_EPS)).astype(np.float32)


def apply_lfr(feature, lfr_m=LFR_M, lfr_n=LFR_N):
    """FunASR 的 LFR：左补 (m-1)//2 帧，再按 n 步长拼 m 帧。"""
    if lfr_m <= 1 and lfr_n <= 1:
        return feature
    total = feature.shape[0]
    if total == 0:
        return np.zeros((0, NUM_MELS * lfr_m), dtype=np.float32)
    lfr_total = int(math.ceil(total / lfr_n))
    left = (lfr_m - 1) // 2
    padded = np.vstack((np.tile(feature[0], (left, 1)), feature))
    length = total + left
    rows = []
    for i in range(lfr_total):
        if lfr_m <= length - i * lfr_n:
            rows.append(padded[i * lfr_n:i * lfr_n + lfr_m].reshape(-1))
        else:
            row = padded[i * lfr_n:].reshape(-1)
            while row.size < lfr_m * feature.shape[1]:
                row = np.hstack((row, padded[-1]))
            rows.append(row)
    return np.vstack(rows).astype(np.float32)


class TokenTable:
    """tokens.txt：行号即 id，tab 前是第一列 token。"""

    def __init__(self, path):
        text = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        self.tokens = [line.split("\t")[0] for line in text]

    def __len__(self):
        return len(self.tokens)

    def decode(self, ids):
        pieces = []
        for token_id in ids:
            if 0 <= token_id < len(self.tokens):
                pieces.append(self.tokens[token_id])
        return _SPECIAL.sub("", "".join(pieces)).replace("▁", " ").strip()


def ctc_greedy(logits, length, blank_id=BLANK_ID):
    """argmax → 去连续重复 → 去 blank。"""
    if length <= 0:
        return []
    best = np.argmax(logits[:length], axis=-1)
    collapsed = []
    previous = -1
    for token_id in best:
        token_id = int(token_id)
        if token_id != previous and token_id != blank_id:
            collapsed.append(token_id)
        previous = token_id
    return collapsed


class SenseVoiceRecognizer:
    def __init__(self, model_dir, model_name="model_quant_optimized.onnx", threads=4):
        import onnxruntime as ort  # 延迟导入：本地无 ort 时仍可跑单测

        self.model_dir = Path(model_dir)
        self.tokens = TokenTable(self.model_dir / "tokens.txt")
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        started = time.perf_counter()
        self.session = ort.InferenceSession(
            str(self.model_dir / model_name), options, providers=["CPUExecutionProvider"]
        )
        self.load_seconds = time.perf_counter() - started
        self.model_name = model_name

    def warmup(self):
        silence = np.zeros(SAMPLE_RATE, dtype=np.float32)  # 1s 静音
        return self.transcribe_waveform(silence)

    def transcribe_waveform(self, waveform):
        started = time.perf_counter()
        feature = apply_lfr(compute_fbank(waveform))
        frames = feature.shape[0]
        speech = feature[None, :, :]
        lengths = np.array([frames], dtype=np.int32)
        language = np.array([LANGUAGE_ZH], dtype=np.int32)
        textnorm = np.array([TEXTNORM_WITHITN], dtype=np.int32)
        tick = time.perf_counter()
        logits, out_lens = self.session.run(
            None,
            {"speech": speech, "speech_lengths": lengths,
             "language": language, "textnorm": textnorm},
        )
        infer_seconds = time.perf_counter() - tick
        ids = ctc_greedy(logits[0], int(out_lens[0]))
        return {
            "text": self.tokens.decode(ids),
            "token_ids": ids,
            "frames": frames,
            "encoder_frames": int(out_lens[0]),
            "infer_seconds": infer_seconds,
            "total_seconds": time.perf_counter() - started,
        }

    def transcribe_file(self, path):
        result = self.transcribe_waveform(read_wav(path))
        result["wav"] = str(path)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="SenseVoice 板端单句识别（常驻进程内计时）")
    parser.add_argument("--model-dir", type=Path, default=Path.home() / "rv/asr/sensevoice/sensevoice")
    parser.add_argument("--wav", type=Path, nargs="+", action="extend", required=True)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    asr = SenseVoiceRecognizer(args.model_dir, threads=args.threads)
    warm = asr.warmup()
    results = []
    for wav in args.wav:
        for _ in range(args.repeat):
            results.append(asr.transcribe_file(wav))
    if args.json:
        print(json.dumps({"model": asr.model_name, "load_seconds": round(asr.load_seconds, 3),
                          "warmup_infer_seconds": round(warm["infer_seconds"], 3),
                          "results": results}, ensure_ascii=False, indent=1))
        return 0
    print(f"模型加载 {asr.load_seconds:.3f}s；预热推理 {warm['infer_seconds']:.3f}s")
    for item in results:
        print(f"{Path(item['wav']).name}: {item['text']!r}  推理 {item['infer_seconds']:.3f}s"
              f"（特征 {item['frames']} 帧，总 {item['total_seconds']:.3f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
