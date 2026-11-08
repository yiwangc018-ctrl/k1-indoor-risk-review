"""Minimal VLM HTTP smoke: OpenAI-compatible chat with a base64 image."""
import base64
import json
import sys
import time
import urllib.request

URL = "http://127.0.0.1:8093/v1/chat/completions"


def main(image_path, prompt, max_tokens=48, runs=3):
    b64 = base64.b64encode(open(image_path, "rb").read()).decode()
    payload = {
        "model": "qwen3.5-0.8b",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                ],
            }
        ],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": False,
    }
    body = json.dumps(payload).encode()
    times = []
    for i in range(runs):
        t0 = time.perf_counter()
        req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
        raw = urllib.request.urlopen(req, timeout=300).read()
        dt = time.perf_counter() - t0
        times.append(dt)
        data = json.loads(raw)
        msg = data["choices"][0]["message"]
        usage = data.get("usage", {})
        print(f"run {i+1}: {dt:.2f}s  usage={usage}")
        print("  content:", (msg.get("content") or "").strip()[:400])
        if msg.get("reasoning_content"):
            print("  reasoning:", msg["reasoning_content"].strip()[:150])
    times.sort()
    print(f"median {times[len(times)//2]:.2f}s  min {times[0]:.2f}s  max {times[-1]:.2f}s")


if __name__ == "__main__":
    main(
        sys.argv[1],
        sys.argv[2] if len(sys.argv) > 2 else "用一句中文描述这张图片里有什么。",
        int(sys.argv[3]) if len(sys.argv) > 3 else 48,
    )
