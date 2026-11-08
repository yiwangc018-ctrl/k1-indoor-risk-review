#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词对照实验 B：开放式提问，不给枚举、不要求 JSON。"""
import base64, glob, json, os, sys, time, urllib.request

PROMPT = (
    "看一眼这张图片。图片里有没有马桶？有没有桌子？\n"
    "有就回答“有”，没有就回答“没有”，并用一句话说明你看到了什么。"
)


def encode(path):
    with open(path, "rb") as fh:
        return "data:image/jpeg;base64," + base64.b64encode(fh.read()).decode("ascii")


def main():
    d, port = sys.argv[1], int(sys.argv[2])
    rows = []
    for path in sorted(glob.glob(os.path.join(d, "q*.jpg"))):
        payload = {"model": "vlm", "temperature": 0, "max_tokens": 96,
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
        rows.append({"qid": os.path.splitext(os.path.basename(path))[0],
                     "seconds": round(dt, 2), "text": text})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    json.dump(rows, open(os.path.join(d, "results_b.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
