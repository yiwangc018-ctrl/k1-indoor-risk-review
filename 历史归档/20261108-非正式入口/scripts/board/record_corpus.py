#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在板上录真人语料：每条提示一句，按回车才开始录，避免"程序在录、人还没开口"。

用法：
    python3 scripts/board/record_corpus.py --out ~/rv/asr/corpus/human
    python3 scripts/board/record_corpus.py --out ~/rv/asr/corpus/human --only p01 n01
    python3 scripts/board/record_corpus.py --out ... --clips ~/rv/asr/corpus/clips.json

语料定义默认取 ~/rv/asr/corpus/clips.json（id/text/expect）。录完写 manifest.json，
之后用 app/asr_sensevoice.py 或常驻服务逐条识别，即可给出真人语料的正确率与耗时。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

RECORD_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RECORD_ROOT / "app"))
import room_review  # noqa: E402


def record(device, path, seconds):
    subprocess.run(
        ["arecord", "-D", device, "-f", "S16_LE", "-r", "16000", "-c", "1",
         "-d", str(seconds), str(path)],
        check=True, capture_output=True, timeout=seconds + 10,
    )
    return path.stat().st_size


def main(argv=None):
    parser = argparse.ArgumentParser(description="录制真人语音语料（16k 单声道 16bit）")
    parser.add_argument("--out", type=Path, default=Path.home() / "rv/asr/corpus/human")
    parser.add_argument("--clips", type=Path,
                        default=Path.home() / "rv/asr/corpus/clips.json")
    parser.add_argument("--config", type=Path,
                        default=RECORD_ROOT / "configs/room_review_k1.json")
    parser.add_argument("--seconds", type=int, default=5)
    parser.add_argument("--only", nargs="*", help="只录这些 id")
    args = parser.parse_args(argv)

    config = room_review.load(args.config)
    device = room_review.microphone_device(config["asr"])
    clips = json.loads(args.clips.read_text(encoding="utf-8"))
    if args.only:
        wanted = set(args.only)
        clips = [clip for clip in clips if clip["id"] in wanted]
    args.out.mkdir(parents=True, exist_ok=True)

    print(f"麦克风 {device}，每条录 {args.seconds} 秒；按回车开始，Ctrl-C 中止。")
    recorded = []
    for clip in clips:
        print(f"\n[{clip['id']}] 请说：{clip['text']}")
        try:
            input("准备好后按回车开始录音：")
        except EOFError:
            pass
        path = args.out / f"{clip['id']}.wav"
        backoff = record(device, path, args.seconds)
        print(f"  已保存 {path}（{backoff} 字节）")
        recorded.append({**clip, "file": str(path), "bytes": backoff})

    manifest = {"recorded_at": datetime.now(timezone.utc).isoformat(),
                "device": device, "seconds": args.seconds, "clips": recorded}
    (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    print(f"\n共 {len(recorded)} 条，清单：{args.out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
