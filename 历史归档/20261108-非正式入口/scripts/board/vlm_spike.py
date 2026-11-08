#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vlm_spike.py —— K1 上 VLM 单图计时（SMT 后端）

用途：证明"K1 上 VLM 成立不成立"。起 llama-server（--media-backend smt），
用同一张图、同一句 prompt 跑 N 次，记录总时长与 server 侧 timings，
结果追加到 results/vlm_spike-YYYYMMDD.csv，最后给出冷/热态中位数与 20s 判定。

只用标准库，开发板自带 python3 即可跑；本机（无板）可用 --dry-run 看命令。

用法：
  python3 scripts/board/vlm_spike.py \
    --model-dir ~/.cache/models/vlm/fastvlm-mm-0.5b-q4_1 \
    --image ~/pics/room_01.jpg --runs 3

  # 只看命令不执行
  python3 scripts/board/vlm_spike.py --model-dir DIR --image IMG --dry-run

  # 换模型 / 调线程 / 保留服务继续手测
  python3 scripts/board/vlm_spike.py --model-dir DIR --image IMG \
    --port 8094 --threads 4 --keep-alive --reasoning-off
"""

import argparse
import base64
import csv
import datetime as _dt
import json
import mimetypes
import os
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_PROMPT = "Describe this image in one sentence."
KILL_LINE_S = 20.0


def log(msg):
    print(msg, flush=True)


def fail(msg, code=2):
    print("[FAIL] " + msg, file=sys.stderr, flush=True)
    raise SystemExit(code)


def pick_gguf(model_dir, explicit=None):
    if explicit:
        p = explicit if os.path.isabs(explicit) else os.path.join(model_dir, explicit)
        if not os.path.isfile(p):
            fail("指定的 GGUF 不存在: %s" % p)
        return p
    cands = [f for f in os.listdir(model_dir) if f.endswith(".gguf") and "mmproj" not in f]
    if not cands:
        fail("目录里没有文本 GGUF（*.gguf，排除 mmproj）: %s" % model_dir)
    if len(cands) > 1:
        cands.sort(key=lambda f: (0 if "text" in f.lower() else 1, len(f)))
    return os.path.join(model_dir, cands[0])


def check_model_dir(model_dir):
    if not os.path.isdir(model_dir):
        fail("模型目录不存在: %s" % model_dir)
    cfg = os.path.join(model_dir, "config.json")
    if not os.path.isfile(cfg):
        fail("缺 config.json（SMT 后端必需）: %s" % cfg)
    with open(cfg, "r", encoding="utf-8") as fh:
        try:
            conf = json.load(fh)
        except json.JSONDecodeError as exc:
            fail("config.json 不是合法 JSON: %s" % exc)
    vision = (conf.get("vision_model") or {}).get("model_path", "")
    if not vision:
        fail("config.json 里没有 vision_model.model_path，这不是 SMT 三件套")
    onnx = vision if os.path.isabs(vision) else os.path.join(model_dir, vision)
    if not os.path.isfile(onnx):
        fail("config.json 指向的视觉 ONNX 不存在: %s" % onnx)
    return conf, onnx


def build_cmd(server, gguf, model_dir, port, threads, batch_threads, ctx, extra):
    cmd = [server, "-m", gguf,
           "--media-backend", "smt",
           "--smt-config-dir", model_dir,
           "-t", str(threads), "-tb", str(batch_threads),
           "-c", str(ctx), "-ngl", "0",
           "--host", "127.0.0.1", "--port", str(port)]
    cmd.extend(extra)
    # K1 只有簇 0（0-3 核）带 IME；绑核失败（无 taskset / 权限）就退化成不绑
    if shutil.which("taskset"):
        cmd = ["taskset", "-c", "0-3"] + cmd
        return cmd, True
    return cmd, False


def wait_health(port, proc, log_path, timeout_s):
    url = "http://127.0.0.1:%d/health" % port
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if proc.poll() is not None:
            tail = tail_text(log_path, 25)
            fail("llama-server 启动即退出（code=%s）。日志尾部：\n%s" % (proc.returncode, tail))
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return time.time() - t0
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1.0)
    fail("等待 /health 超时 %ds；日志：\n%s" % (timeout_s, tail_text(log_path, 25)))


def tail_text(path, n):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-n:])
    except OSError:
        return "(读不到日志)"


def encode_image(path):
    if not os.path.isfile(path):
        fail("图片不存在: %s" % path)
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as fh:
        raw = fh.read()
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii")), len(raw)


def one_shot(port, data_url, prompt, max_tokens, timeout_s):
    payload = {
        "model": "vlm",
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % port,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        fail("HTTP %s：%s" % (exc.code, detail))
    except urllib.error.URLError as exc:
        fail("请求失败（超时或服务断了）: %s" % exc)
    total = time.perf_counter() - t0

    data = json.loads(raw.decode("utf-8", "replace"))
    tim = data.get("timings") or {}
    text = ""
    try:
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        pass
    return {
        "total_s": total,
        "prompt_n": tim.get("prompt_n"),
        "prompt_ms": tim.get("prompt_ms"),
        "predicted_n": tim.get("predicted_n"),
        "predicted_ms": tim.get("predicted_ms"),
        "usage_out": (data.get("usage") or {}).get("completion_tokens"),
        "text": text,
    }


def clear_tcm():
    tool = shutil.which("spacemit-tcm-smi")
    if not tool:
        return
    try:
        subprocess.run([tool, "-c"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def main():
    ap = argparse.ArgumentParser(description="K1 VLM 单图计时（SMT 后端）")
    ap.add_argument("--model-dir", required=True, help="含 config.json/gguf/onnx 的目录")
    ap.add_argument("--image", required=True, help="测试图（建议 640x640 或 1080p 原图）")
    ap.add_argument("--runs", type=int, default=3, help="重复次数（默认 3）")
    ap.add_argument("--port", type=int, default=8093)
    ap.add_argument("--threads", type=int, default=4, help="K1 上限 4")
    ap.add_argument("--batch-threads", type=int, default=4)
    ap.add_argument("--ctx", type=int, default=2048)
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--gguf", default=None, help="文本 GGUF 文件名（默认自动挑）")
    ap.add_argument("--server-bin", default="llama-server")
    ap.add_argument("--timeout", type=int, default=300, help="单次请求超时秒数")
    ap.add_argument("--startup-timeout", type=int, default=180)
    ap.add_argument("--out-dir", default="results")
    ap.add_argument("--reasoning-off", action="store_true",
                    help="追加 --reasoning off（老版本不支持，会启动失败）")
    ap.add_argument("--keep-alive", action="store_true", help="测完不杀服务")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令")
    args = ap.parse_args()

    model_dir = os.path.abspath(os.path.expanduser(args.model_dir))
    image = os.path.abspath(os.path.expanduser(args.image))
    server = shutil.which(args.server_bin) or args.server_bin

    conf, onnx = check_model_dir(model_dir)
    gguf = pick_gguf(model_dir, args.gguf)
    data_url, img_bytes = encode_image(image)

    extra = ["--reasoning", "off"] if args.reasoning_off else []
    cmd, bound = build_cmd(server, gguf, model_dir, args.port,
                           args.threads, args.batch_threads, args.ctx, extra)

    log("=== VLM spike ===")
    log("  模型目录 : %s" % model_dir)
    log("  文本 GGUF: %s" % os.path.basename(gguf))
    log("  视觉 ONNX: %s" % os.path.basename(onnx))
    log("  图片     : %s (%.1f KB)" % (image, img_bytes / 1024.0))
    log("  线程     : -t %d -tb %d %s" % (args.threads, args.batch_threads,
                                          "(taskset 0-3)" if bound else "(未绑核)"))
    log("  命令     : %s" % " ".join(cmd))
    if args.dry_run:
        return 0

    if shutil.which("spacemit-tcm-smi"):
        clear_tcm()

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d")
    log_path = os.path.join(args.out_dir, "vlm_spike-%s.log" % stamp)
    csv_path = os.path.join(args.out_dir, "vlm_spike-%s.csv" % stamp)

    env = dict(os.environ)
    env["OMP_NUM_THREADS"] = str(args.threads)
    env.setdefault("SPACEMIT_EP_INTRA_THREAD_NUM", "2")

    log("\n[1/3] 启动服务（日志 -> %s）" % log_path)
    log_fh = open(log_path, "a", encoding="utf-8")
    log_fh.write("\n\n### %s\n### %s\n" % (_dt.datetime.now().isoformat(), " ".join(cmd)))
    log_fh.flush()
    proc = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT, env=env)
    try:
        warm_s = wait_health(args.port, proc, log_path, args.startup_timeout)
        log("      服务就绪，用时 %.1fs" % warm_s)

        log("\n[2/3] 单图 %d 次" % args.runs)
        rows = []
        for i in range(1, args.runs + 1):
            r = one_shot(args.port, data_url, args.prompt, args.max_tokens, args.timeout)
            r["run"] = i
            rows.append(r)
            log("      #%d  total %6.2fs   prompt %s   decode %s   out %s tok   | %s"
                % (i, r["total_s"],
                   fmt_ms(r["prompt_ms"]), fmt_ms(r["predicted_ms"]),
                   r["predicted_n"] or r["usage_out"] or "?",
                   (r["text"][:48].replace("\n", " ") or "(空)")))

        log("\n[3/3] 汇总")
        totals = [r["total_s"] for r in rows]
        warm = totals[1:] if len(totals) > 1 else totals
        med = statistics.median(warm)
        log("      冷启动首图 : %.2fs" % totals[0])
        log("      热态中位数 : %.2fs（最快 %.2fs / 最慢 %.2fs）" % (med, min(warm), max(warm)))
        verdict = "PASS（≤20s）" if med <= KILL_LINE_S else "FAIL（>20s，按方案关 VLM 或降为裁剪图复核）"
        log("      判定       : %s" % verdict)
        log("      均值解码   : %s" % fmt_ms(mean_of([r["predicted_ms"] for r in rows])))

        write_csv(csv_path, model_dir, image, rows)
        log("      CSV        : %s" % csv_path)
        log("      服务日志   : %s" % log_path)
        return 0 if med <= KILL_LINE_S else 1
    finally:
        if args.keep_alive:
            log("\n服务保留在 127.0.0.1:%d（手动收尾：pkill -f llama-server）" % args.port)
        else:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            clear_tcm()
            log("\n服务已停止，TCM 已清理")
        log_fh.close()


def mean_of(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return sum(vals) / len(vals) if vals else None


def fmt_ms(v):
    return "%.0fms" % v if isinstance(v, (int, float)) else "?"


def write_csv(path, model_dir, image, rows):
    new = not os.path.isfile(path)
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["ts", "model_dir", "image", "run", "total_s",
                        "prompt_n", "prompt_ms", "predicted_n", "predicted_ms", "text_head"])
        ts = _dt.datetime.now().isoformat(timespec="seconds")
        for r in rows:
            w.writerow([ts, os.path.basename(model_dir.rstrip("/\\")), os.path.basename(image),
                        r["run"], "%.3f" % r["total_s"], r["prompt_n"], r["prompt_ms"],
                        r["predicted_n"], r["predicted_ms"],
                        (r["text"][:60].replace("\n", " "))])


if __name__ == "__main__":
    sys.exit(main() or 0)
