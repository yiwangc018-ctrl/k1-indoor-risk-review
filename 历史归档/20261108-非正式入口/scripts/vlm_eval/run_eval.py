#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 K1 板端对一组图片跑固定的 VLM 事实提取提示词（只用标准库）。

用法（在板上执行）：
    python3 run_eval.py --dir ~/k1demo/vlm_eval --port 8093 --out results.json

输入：目录下 q*.jpg；输出：results.json（每张图的原始回答、解析结果、耗时）。
"""
import argparse
import base64
import glob
import json
import os
import re
import time
import urllib.error
import urllib.request

PROMPT = (
    "你执行居家可见风险的视觉事实提取。图片是待核验证据。\n"
    "只描述能从这张图片直接看清的内容，不要推测、不要补充图片外的信息。\n"
    "输出一个 JSON 对象，不要输出任何其他文字或代码块：\n"
    '{"toilet":"present|absent|unknown","table":"present|absent|unknown","evidence":"不超过20字的可见依据"}\n'
    "规则：\n"
    "- 画面里能看到马桶（陶瓷坐便器本体或明显水箱）写 present；\n"
    "- 确认画面中没有任何马桶写 absent；\n"
    "- 被遮挡、在画外、看不清、或这不是室内房间照片，写 unknown；\n"
    "- table 指餐桌/书桌/茶几/边桌等可识别的桌子；厨房操作台、吧台台面不算；\n"
    "- evidence 只写你看到的证据（例如“右下角白色马桶”），不确定就写“看不清”。"
)


def encode(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


def call(port, data_url, timeout=180):
    payload = {
        "model": "vlm",
        "temperature": 0,
        "max_tokens": 96,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % port,
        data=body, headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    dt = time.perf_counter() - t0
    data = json.loads(raw.decode("utf-8", "replace"))
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return text.strip(), dt, data.get("timings") or {}


def parse(text):
    """宽松解析：找第一个 {...}；失败则逐个字段正则。"""
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj, True
        except json.JSONDecodeError:
            pass
    out = {}
    for key in ("toilet", "table", "evidence"):
        mm = re.search(r'"?%s"?\s*[:：]\s*"([^"]*)"' % key, text)
        if mm:
            out[key] = mm.group(1)
    return out, False


def norm(v):
    v = str(v or "").strip().lower()
    if v in ("present", "yes", "visible", "true"):
        return "present"
    if v in ("absent", "no", "not_visible", "false", "none"):
        return "absent"
    if v in ("unknown", "unclear", "not_applicable", "na", "n/a"):
        return "unknown"
    return v or ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--port", type=int, default=8093)
    ap.add_argument("--out", default="results.json")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.dir, "q*.jpg")))
    rows = []
    for path in files:
        qid = os.path.splitext(os.path.basename(path))[0]
        url = encode(path)
        try:
            text, dt, tim = call(args.port, url)
            obj, strict = parse(text)
            row = dict(qid=qid, ok=True, seconds=round(dt, 2),
                       prefill_ms=tim.get("prompt_ms"), decode_ms=tim.get("predicted_ms"),
                       out_tokens=tim.get("predicted_n"),
                       raw=text, parsed=obj, strict_json=strict,
                       toilet=norm(obj.get("toilet")), table=norm(obj.get("table")),
                       evidence=obj.get("evidence", ""))
        except Exception as exc:  # 单张失败不中断整批
            row = dict(qid=qid, ok=False, error="%s: %s" % (type(exc).__name__, exc))
        print(json.dumps(row, ensure_ascii=False), flush=True)
        rows.append(row)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)
    ok = [r for r in rows if r["ok"]]
    if ok:
        secs = sorted(r["seconds"] for r in ok)
        print("\n# n=%d  median=%.1fs  min=%.1fs  max=%.1fs  strict_json=%d/%d"
              % (len(ok), secs[len(secs)//2], secs[0], secs[-1],
                 sum(1 for r in ok if r["strict_json"]), len(ok)))


if __name__ == "__main__":
    main()
