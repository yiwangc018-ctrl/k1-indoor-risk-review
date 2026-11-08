#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llm_decision_bench.py —— 板端大模型「指令 → JSON」合法率 / TTFT 测试

用途：
    参赛指南 §8 里 10/30 的验收标准是「固定 10 句，JSON 合法率 ≥9/10」。
    这个脚本就是那把尺子：逐句请求、校验 JSON、记录 TTFT 与总耗时，
    最后出 CSV（results/ 下），可以直接贴进技术文档的测试报告。

为什么用流式：
    主指标是「说完最后一个字 → 第一个可感知响应」，真正要看的是 TTFT
    （首 token 时间），不是总耗时。这里按 SSE 行流式读取，单独记 TTFT。

支持两种后端（--backend auto 会自动探测）：
    openai  llama-server / 任何 OpenAI 兼容服务      POST /v1/chat/completions
    ollama  spacemit-ollama-toolkit                 POST /api/chat

用法：
    # llama.cpp 路线（赛题 2 部署口径）
    python3 llm_decision_bench.py --backend openai --host http://127.0.0.1:8080 --model local
    # Ollama 路线
    python3 llm_decision_bench.py --model qwen2.5-0.5b-fc
    # 每句 3 次看抖动
    python3 llm_decision_bench.py --runs 3

只依赖标准库。
"""

import argparse
import csv
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

ACTIONS = ("turn_left", "turn_right", "look_up", "look_down",
           "center", "stop", "scan", "none")

# (中文指令, 期望动作)
CASES = [
    ("向左转", "turn_left"),
    ("向右转", "turn_right"),
    ("往左一点点", "turn_left"),
    ("往右一点点", "turn_right"),
    ("抬头", "look_up"),
    ("低头", "look_down"),
    ("回中", "center"),
    ("停一下", "stop"),
    ("帮我找一下红色的杯子", "scan"),
    ("桌上有哪些东西", "scan"),
    ("今天天气怎么样", "none"),
    ("你叫什么名字", "none"),
]

SYSTEM_PROMPT = """你是机器人指令解析器。可用动作与含义：
- turn_left：向左转、往左一点
- turn_right：向右转、往右一点
- look_up：抬头、向上看
- look_down：低头、向下看
- center：回中、复位、正前方
- stop：停下、别动
- scan：找东西、看看有什么、搜索目标
- none：与上面动作无关，或闲聊
规则：只输出一行 JSON，格式 {"function":"动作名","arguments":{}}。
不要输出解释，不要用 markdown 代码块。判断不了就输出 {"function":"none","arguments":{}}。"""


# --------------------------------------------------------------------------
# 后端探测
# --------------------------------------------------------------------------
def _get_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def detect_backend(host, timeout=5):
    """先试 OpenAI 的 /v1/models，再试 Ollama 的 /api/tags。"""
    base = host.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    try:
        _get_json(base + "/v1/models", timeout)
        return "openai"
    except (urllib.error.URLError, OSError, ValueError):
        pass
    try:
        _get_json(base + "/api/tags", timeout)
        return "ollama"
    except (urllib.error.URLError, OSError, ValueError):
        return None


def list_models(host, backend):
    base = host.rstrip("/")
    if backend == "openai":
        data = _get_json(base + "/v1/models")
        return [m.get("id", "") for m in data.get("data", [])]
    data = _get_json(base + "/api/tags")
    return [m.get("name", "") for m in data.get("models", [])]


# --------------------------------------------------------------------------
# 请求：两种后端共用一个返回结构
# --------------------------------------------------------------------------
def _stream_lines(resp):
    for raw in resp:
        line = raw.strip()
        if line:
            yield line


def chat_openai(host, model, text, timeout):
    base = host.rstrip("/")
    payload = {
        "model": model or "local",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": 0,
        "max_tokens": 48,
    }
    req = urllib.request.Request(
        base + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    t0 = time.perf_counter()
    ttft, parts, usage = None, [], {}
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for line in _stream_lines(resp):
            if not line.startswith(b"data:"):
                continue
            chunk = line[5:].strip()
            if chunk == b"[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            choices = obj.get("choices") or []
            delta = (choices[0].get("delta") or {}) if choices else {}
            piece = delta.get("content") or ""
            if piece:
                if ttft is None:
                    ttft = (time.perf_counter() - t0) * 1000.0
                parts.append(piece)

    total_ms = (time.perf_counter() - t0) * 1000.0
    completion = usage.get("completion_tokens") or 0
    # 没有 usage 时用内容长度粗估（中文 ≈1.5 token/字）
    if not completion:
        completion = max(1, int(len("".join(parts)) * 1.5))
    decode_ms = total_ms - (ttft or 0.0)
    speed = completion / (decode_ms / 1000.0) if decode_ms > 1 else 0.0
    return {"content": "".join(parts).strip(), "ttft_ms": ttft or total_ms,
            "total_ms": total_ms, "eval_count": completion, "tok_per_s": speed}


def chat_ollama(host, model, text, timeout):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "stream": True,
        "options": {"temperature": 0, "num_predict": 48},
    }
    req = urllib.request.Request(
        host.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    t0 = time.perf_counter()
    ttft, parts, final = None, [], {}
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for line in _stream_lines(resp):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            piece = (obj.get("message") or {}).get("content") or ""
            if piece:
                if ttft is None:
                    ttft = (time.perf_counter() - t0) * 1000.0
                parts.append(piece)
            if obj.get("done"):
                final = obj

    total_ms = (time.perf_counter() - t0) * 1000.0
    eval_count = final.get("eval_count") or 0
    eval_ns = final.get("eval_duration") or 0
    speed = (eval_count / (eval_ns / 1e9)) if eval_count and eval_ns else 0.0
    return {"content": "".join(parts).strip(), "ttft_ms": ttft or total_ms,
            "total_ms": total_ms, "eval_count": eval_count, "tok_per_s": speed}


def chat(backend, host, model, text, timeout):
    return (chat_openai if backend == "openai" else chat_ollama)(host, model, text, timeout)


# --------------------------------------------------------------------------
def extract_json(text):
    """从模型输出里抠出第一个完整 JSON 对象；失败返回 None。"""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None


def pct(values, p):
    if not values:
        return 0.0
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def main(argv=None):
    ap = argparse.ArgumentParser(description="指令→JSON 合法率/TTFT 测试")
    ap.add_argument("--host", default="http://127.0.0.1:11434",
                    help="服务地址；openai 后端常用 http://127.0.0.1:8080")
    ap.add_argument("--backend", choices=("auto", "openai", "ollama"), default="auto")
    ap.add_argument("--model", default="", help="模型名；openai 后端可随便填 local")
    ap.add_argument("--runs", type=int, default=1, help="每条指令跑几次，默认 1")
    ap.add_argument("--timeout", type=float, default=90.0, help="单次请求超时（秒）")
    ap.add_argument("--out", default="results", help="CSV 输出目录")
    ap.add_argument("--no-warmup", action="store_true", help="跳过预热（首跑会含模型加载）")
    args = ap.parse_args(argv)

    backend = args.backend
    if backend == "auto":
        backend = detect_backend(args.host)
        if backend is None:
            print(f"[FAIL] {args.host} 上没有可用的 LLM 服务。")
            print("       llama.cpp:  bash 21_serve_llamacpp.sh --status")
            print("       ollama   :  systemctl status ollama")
            return 2
        print(f"探测到后端: {backend}")

    if backend == "openai" and not args.model:
        args.model = "local"

    try:
        names = list_models(args.host, backend)
    except (urllib.error.URLError, OSError, ValueError):
        names = []
    if names and args.model not in names:
        print(f"[WARN] 服务里的模型列表没有 {args.model}：{', '.join(names[:5])}")
        print("       继续跑也行，llama-server 通常只有一份模型、名字随便填。")

    if not args.no_warmup:
        print(f"预热 {args.model} ...", end="", flush=True)
        try:
            r = chat(backend, args.host, args.model, "你好", args.timeout)
            print(f" 完成（{r['total_ms']:.0f} ms，含模型加载）")
        except Exception as e:  # noqa: BLE001
            print(f" 失败：{e}")
            print("[FAIL] 预热都过不了，先确认模型能单独跑起来")
            return 2

    rows = []
    n = 0
    print()
    print(f"{'#':<3}{'指令':<22}{'期望':<12}{'输出':<12}{'JSON':<6}{'正确':<6}{'TTFT':>8}{'总耗时':>9}{'tok/s':>7}")
    print("-" * 96)

    for run in range(args.runs):
        for i, (text, expected) in enumerate(CASES, 1):
            n += 1
            try:
                r = chat(backend, args.host, args.model, text, args.timeout)
                err = ""
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                r = {"content": "", "ttft_ms": 0, "total_ms": 0, "eval_count": 0, "tok_per_s": 0}
                err = str(e)

            obj = extract_json(r["content"]) if r["content"] else None
            func = str((obj or {}).get("function", "")).strip().lower()
            json_ok = obj is not None and func in ACTIONS
            correct = json_ok and func == expected

            rows.append({
                "run": run + 1, "case": i, "text": text, "expected": expected,
                "function": func, "json_ok": json_ok, "correct": correct,
                "ttft_ms": round(r["ttft_ms"], 1), "total_ms": round(r["total_ms"], 1),
                "eval_count": r["eval_count"], "tok_per_s": round(r["tok_per_s"], 2),
                "raw": r["content"].replace("\n", " ")[:200], "error": err,
            })

            mark = "OK" if json_ok else "BAD"
            print(f"{n:<3}{text:<22}{expected:<12}{(func or '-'):<12}{mark:<6}"
                  f"{('Y' if correct else 'n'):<6}{r['ttft_ms']:>8.0f}{r['total_ms']:>9.0f}"
                  f"{r['tok_per_s']:>7.1f}")
            if err:
                print(f"     ! {err[:90]}")

    good = [x for x in rows if x["error"] == ""]
    json_rate = sum(1 for x in good if x["json_ok"]) / len(rows) * 100 if rows else 0
    acc_rate = sum(1 for x in good if x["correct"]) / len(rows) * 100 if rows else 0
    ttfts = [x["ttft_ms"] for x in good]
    totals = [x["total_ms"] for x in good]
    speeds = [x["tok_per_s"] for x in good if x["tok_per_s"] > 0]

    print()
    print("=" * 60)
    print(f"后端        : {backend}     模型: {args.model}     样本: {len(rows)}（失败 {len(rows) - len(good)}）")
    print(f"JSON 合法率 : {json_rate:5.1f}%  ({sum(1 for x in good if x['json_ok'])}/{len(rows)})"
          f"   验收线 ≥90%")
    print(f"动作准确率  : {acc_rate:5.1f}%  ({sum(1 for x in good if x['correct'])}/{len(rows)})")
    if ttfts:
        print(f"TTFT        : P50 {pct(ttfts, 50):.0f} ms   P90 {pct(ttfts, 90):.0f} ms")
        print(f"总耗时      : P50 {pct(totals, 50):.0f} ms   P90 {pct(totals, 90):.0f} ms")
    if speeds:
        print(f"生成速度    : P50 {pct(speeds, 50):.2f} tok/s（X60 上 0.6B Q4 约 10~12 tok/s）")
    print("=" * 60)

    if json_rate < 90:
        print("\n提示：JSON 不合法先看 raw 列。常见修法——")
        print("  1) 系统提示末尾再补一句『只输出 JSON，禁止任何其他字符』；")
        print("  2) max_tokens/num_predict 调到 32，别让它有空间啰嗦；")
        print("  3) 用经过 function-calling 微调的模型（Ollama 路线的 *-fc 版本）。")

    os.makedirs(args.out, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_model = (args.model or "local").replace(":", "_")
    csv_path = os.path.join(args.out, f"llm_decision_{backend}_{safe_model}_{stamp}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nCSV: {csv_path}")
    return 0 if json_rate >= 90 else 1


if __name__ == "__main__":
    sys.exit(main())
