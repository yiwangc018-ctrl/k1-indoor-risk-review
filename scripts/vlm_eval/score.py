#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汇总 VLM 验证结果：评分、对照表、对照图。

用法（仓库根目录）：
    python scripts/vlm_eval/score.py --root results/vlm_eval

输入（--root 下）：
    labels.json            14 张图的真值与来源
    board_results.json     主实验（JSON 提示词）
    board_results_b.json   开放问句变体（可选）
    board_results_c.json   两行自然语言变体（可选）
    frontier_answers.json  前沿模型盲答
    blind_map.json         盲测编号 s01.. 与 q01.. 的映射

输出：
    results_table.csv      逐图对照
    figure.jpg             三张例图 + 真值/0.8B/前沿模型
    终端摘要
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SAFE_ABSENT = {"absent", "unknown"}  # 无目标时，判 absent 或 unknown 都算安全


def load(path: Path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def font(size: int):
    for name in (r"C:\Windows\Fonts\msyh.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="results/vlm_eval")
    args = ap.parse_args()
    root = Path(args.root)

    labels = {r["qid"]: r for r in load(root / "labels.json", [])}
    if not labels:
        print("缺少 labels.json")
        return 2
    rows_a = {r["qid"]: r for r in load(root / "board_results.json", [])}
    frontier = (load(root / "frontier_answers.json", {}) or {}).get("answers", {})
    blind = load(root / "blind_map.json", {}) or {}
    q2s = {q: s for s, q in blind.items()}

    facts = ("toilet", "table")
    total = correct = 0
    present_n = present_hit = absent_n = absent_safe = 0
    frontier_correct = frontier_total = 0
    latencies = []
    strict = 0

    with (root / "results_table.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["qid", "bucket", "GT_toilet", "GT_table", "A_toilet", "A_table", "A_correct",
                    "frontier_toilet", "frontier_table", "frontier_correct", "A_seconds", "A_evidence"])
        for q in sorted(labels):
            gt, a = labels[q], rows_a.get(q, {})
            fr = frontier.get(q2s.get(q, ""), {})
            ac = fc = 0
            for fact in facts:
                total += 1
                ok = a.get(fact) == gt[fact]
                ac += ok
                correct += ok
                if gt[fact] == "present":
                    present_n += 1
                    present_hit += a.get(fact) == "present"
                else:
                    absent_n += 1
                    absent_safe += a.get(fact) in SAFE_ABSENT
                if fr:
                    frontier_total += 1
                    fc += fr.get(fact) == gt[fact]
                    frontier_correct += fr.get(fact) == gt[fact]
            if a.get("strict_json"):
                strict += 1
            if isinstance(a.get("seconds"), (int, float)):
                latencies.append(a["seconds"])
            w.writerow([q, gt["bucket"], gt["toilet"], gt["table"], a.get("toilet"), a.get("table"), ac,
                        fr.get("toilet"), fr.get("table"), fc, a.get("seconds", ""), a.get("evidence", "")])

    print("=" * 60)
    print(f"主实验 A（JSON 提示词）  n={len(labels)} 图 / {total} 个事实")
    print(f"  事实准确率      {correct}/{total} = {correct / total:.1%}")
    print(f"  敏感度(有→present) {present_hit}/{present_n}"
          + (f" = {present_hit / present_n:.0%}" if present_n else ""))
    print(f"  特异度(无→absent/unknown) {absent_safe}/{absent_n}"
          + (f" = {absent_safe / absent_n:.0%}" if absent_n else ""))
    print(f"  JSON 严格合法   {strict}/{len(labels)}")
    if latencies:
        print(f"  延迟 中位 {statistics.median(latencies):.1f}s "
              f"范围 {min(latencies):.1f}–{max(latencies):.1f}s")
    if frontier_total:
        print(f"  前沿模型对照    {frontier_correct}/{frontier_total} = {frontier_correct / frontier_total:.1%}")

    rows_b = load(root / "board_results_b.json", [])
    if rows_b:
        absent_cases = [r for r in rows_b if labels[r["qid"]]["toilet"] == "absent"
                        and labels[r["qid"]]["table"] == "absent"]
        said_no = sum("没有" in r["text"] for r in absent_cases)
        print(f"变体 B（开放问句） 无目标图正确说“没有”：{said_no}/{len(absent_cases)}")

    rows_c = load(root / "board_results_c.json", [])
    if rows_c:
        ok = sum(1 for r in rows_c if r.get("parsed", {}).get("马桶") in ("有", "没有", "不确定")
                 and r.get("parsed", {}).get("桌子") in ("有", "没有", "不确定"))
        print(f"变体 C（两行自然语言） 格式合规：{ok}/{len(rows_c)}")

    # 三张例图
    panels = [q for q in ("q01", "q09", "q13") if (root / "images" / f"{q}.jpg").is_file()]
    if panels and rows_a:
        W, H, bar = 640, 480, 118
        sheet = Image.new("RGB", (W * len(panels), H + bar), "white")
        draw = ImageDraw.Draw(sheet)
        f_small = font(17)
        for i, q in enumerate(panels):
            im = Image.open(root / "images" / f"{q}.jpg").convert("RGB").resize((W, H))
            sheet.paste(im, (i * W, 0))
            gt, a = labels[q], rows_a[q]
            fr = frontier.get(q2s.get(q, ""), {})
            lines = [
                (f"{q}  GT: toilet={gt['toilet']}, table={gt['table']}", "black"),
                (f"0.8B: toilet={a.get('toilet')}, table={a.get('table')}", "darkred"),
                (f"前沿模型: {fr.get('toilet', '?')}/{fr.get('table', '?')}", "darkgreen"),
            ]
            y = H + 8
            for text, color in lines:
                draw.text((i * W + 10, y), text, fill=color, font=f_small)
                y += 24
        sheet.save(root / "figure.jpg", quality=88)
        print(f"已写 {root / 'results_table.csv'} 与 {root / 'figure.jpg'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
