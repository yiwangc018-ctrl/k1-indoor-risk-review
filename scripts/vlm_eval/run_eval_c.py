#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词对照实验 C：自然语言两行回答（不给 JSON、不给枚举模板）。"""
import base64, glob, json, os, re, sys, time, urllib.request

PROMPT = (
    "这张图片的检查结果，请只用两行回答，不要输出其他内容：\n"
    "马桶：有/没有（一句依据）\n"
    "桌子：有/没有（一句依据）\n"
    "看不清、被挡住、或这不是室内房间，就写“不确定”。"
)


def encode(path):
    with open(path, "rb") as fh:
        return "data:image/jpeg;base64," + base64.b64encode(fh.read()).decode("ascii")


def parse(text):
    out = {}
    for key in ("马桶", "桌子"):
        m = re.search(key + r"\s*[:：]\s*(有|没有|不确定)", text)
        out[key] = m.group(1) if m else ""
    return out


def main():
    d, port = sys.argv[1], int(sys.argv[2])
    rows = []
    for path in sorted(glob.glob(os.path.join(d, "q*.jpg"))):
        payload = {"model": "vlm", "temperature": 0, "max_tokens": 120,
                   "messages": [{"role": "user", "content": [
                       {"type": "image_url", "image_url": {"url": encode(path)}},
                       {"type": "text", "text": PROMPT}]}]}
        req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % port,
                                     data=json.dumps(payload, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        dt = time.perf_counter() - t0
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
        row = {"qid": os.path.splitext(os.path.basename(path))[0],
               "seconds": round(dt, 2), "text": text, "parsed": parse(text)}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    json.dump(rows, open(os.path.join(d, "results_c.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
