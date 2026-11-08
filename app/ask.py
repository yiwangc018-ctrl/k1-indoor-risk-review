#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一个入口，把"终端里问一句"变成"屏幕上一个数"。

    python3 ask.py "输出数字123"              # 一次问答
    python3 ask.py                            # 交互模式，连问
    python3 ask.py --mock "输出数字123"        # 不连模型、不碰屏幕（PC 上验证链路）
    python3 ask.py --check                    # 只看后端通不通
    python3 ask.py --raw "输出数字123"         # 打印模型的原始输出，用于调提示词

退出码：0 成功 / 2 模型或网络失败 / 3 模型回话里没有数字（契约层拒绝）
        / 4 配置错误（显式配置缺失、缺 PyYAML、YAML 不合法）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import display as display_mod  # noqa: E402
import llm as llm_mod  # noqa: E402

EXIT_OK, EXIT_LLM, EXIT_CONTRACT = 0, 2, 3
EXIT_CONFIG = 4


# ---------------------------------------------------------------------------
# 日志：一行一次调用，报告的"合法率/延迟表"直接从它统计
# ---------------------------------------------------------------------------
def log_call(path, record):
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        record = dict(record)
        record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print("[ask] 日志写入失败：%s" % exc, file=sys.stderr)


# ---------------------------------------------------------------------------
# 一次完整链路
# ---------------------------------------------------------------------------
def run_once(text, cli, disp, cfg, show_raw=False, force_llm=False):
    """一条链路：输入 → 模型 → 契约层 → 屏幕。

    app.mode 决定契约：
      text   —— 显示模型给出的任意短文字（默认，支持中文）
      digits —— 只接受 1~5 位数字，并带"话里没数字就不调用模型"的规则快路径
    """
    mode = str(cfg.get("app", {}).get("mode", "digits")).lower()
    print("[ask] 输入 : %s" % text)
    print("[ask] 后端 : %s -> %s  模式=%s%s"
          % (cli.backend, cli.url or "(本地)", mode,
             "  grammar=on" if (cli.use_grammar and cli.grammar and cli.backend == "llamacpp")
             else ""))
    if mode == "digits":
        return _run_digits(text, cli, disp, cfg, show_raw, force_llm)
    return _run_text(text, cli, disp, cfg, show_raw)


def _run_text(text, cli, disp, cfg, show_raw):
    """任意文字模式：模型说什么，就把它的前 max_chars 个字显示出来。"""
    app_cfg = cfg.get("app", {}) or {}
    max_chars = int(app_cfg.get("max_chars", 32))

    reply = cli.ask(text)
    if reply.error:
        print("[ask] 失败 : %s" % reply.error)
        disp.show("ERR")
        log_call(app_cfg.get("log"),
                 {"input": text, "ok": False, "error": reply.error, "mode": "text",
                  "backend": cli.backend, "ms": round(reply.ms, 1)})
        return EXIT_LLM

    print("[ask] 模型 : %s ms  |%s|" % (round(reply.ms), reply.raw.replace("\n", "\\n")))
    if show_raw:
        print("[ask] 原始 : %r" % reply.raw)

    shown, note = llm_mod.extract_text(reply.raw, max_chars)
    if shown is None:
        print("[ask] 契约 : 拒绝 —— 模型没给出可显示的文字，屏幕显示 ?")
        disp.show("?")
        log_call(app_cfg.get("log"),
                 {"input": text, "raw": reply.raw, "ok": False, "source": "empty",
                  "mode": "text", "backend": cli.backend, "ms": round(reply.ms, 1)})
        return EXIT_CONTRACT

    disp_cfg = cfg.get("display", {}) or {}
    size, lines = disp.show_text(
        shown,
        disp_cfg.get("font") or None,
        tuple(disp_cfg.get("sizes") or display_mod.DEFAULT_SIZES),
    )
    print("[ask] 契约 : %s  (%s)" % (shown, note))
    print("[ask] 显示 : %d 行 / 字号 %s / 后端=%s" % (len(lines), size, disp.name))
    for ln in lines:
        print("        | %s" % ln)

    log_call(app_cfg.get("log"),
             {"input": text, "raw": reply.raw, "parsed": shown, "source": note,
              "mode": "text", "lines": lines, "ok": True,
              "backend": cli.backend, "ms": round(reply.ms, 1)})
    return EXIT_OK


def _run_digits(text, cli, disp, cfg, show_raw, force_llm=False):
    """数字模式：0.5B 被"只输出数字"逼着会硬编一个数字（"你好" -> "1"），
    所以先做规则判定：话里没有数字就直接显示 ?，不打扰模型。"""
    app_cfg = cfg.get("app", {}) or {}
    expected = llm_mod.numbers_in(text)
    if not expected and not force_llm:
        print("[ask] 判定 : 输入里没有数字 -> 不调用模型（省 1.2 秒，也不会瞎编）")
        print("[ask] 契约 : 拒绝 —— 屏幕显示 ?")
        disp.show("?")
        log_call(app_cfg.get("log"),
                 {"input": text, "ok": False, "source": "precheck", "mode": "digits",
                  "reason": "no-number-in-input", "backend": cli.backend, "ms": 0.0})
        return EXIT_CONTRACT

    reply = cli.ask(text)

    if reply.error:
        print("[ask] 失败 : %s" % reply.error)
        disp.show("ERR")
        log_call(app_cfg.get("log"),
                 {"input": text, "ok": False, "error": reply.error, "mode": "digits",
                  "backend": cli.backend, "ms": round(reply.ms, 1)})
        return EXIT_LLM

    print("[ask] 模型 : %s ms  |%s|" % (round(reply.ms), reply.raw.replace("\n", "\\n")))
    if show_raw:
        print("[ask] 原始 : %r" % reply.raw)

    if reply.parsed is None:
        print("[ask] 契约 : 拒绝 —— 回话里没有 1~5 位数字，屏幕显示 ?")
        print("       调提示词：python3 ask.py --raw \"%s\"" % text)
        disp.show("?")
        log_call(app_cfg.get("log"),
                 {"input": text, "raw": reply.raw, "ok": False, "source": "none",
                  "mode": "digits", "backend": cli.backend, "ms": round(reply.ms, 1)})
        return EXIT_CONTRACT

    match = None
    if expected:
        match = reply.parsed in expected
        if not match:
            print("[ask] 注意 : 你话里的数字是 %s，模型给的是 %s（已按模型输出显示）"
                  % ("/".join(expected), reply.parsed))

    used = disp.show(reply.parsed)
    print("[ask] 契约 : %s  (%s)" % (reply.parsed, reply.source))
    print("[ask] 显示 : %s  scale=%s  后端=%s" % (reply.parsed, used, disp.name))

    log_call(app_cfg.get("log"),
             {"input": text, "raw": reply.raw, "parsed": reply.parsed,
              "source": reply.source, "match": match, "mode": "digits", "ok": True,
              "backend": cli.backend, "ms": round(reply.ms, 1)})
    return EXIT_OK


def run_loop(cli, disp, cfg, show_raw=False, force_llm=False):
    print("交互模式：输入一句话回车，Ctrl+C 退出")
    code = EXIT_OK
    while True:
        try:
            text = input("\n>>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[ask] 退出")
            return code
        if not text:
            continue
        code = run_once(text, cli, disp, cfg, show_raw, force_llm)
        print("[ask] exit=%d" % code)


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="问一句 → 屏幕显示数字（LLM + I2C OLED）")
    ap.add_argument("text", nargs="*", help="要问的话；省略则进交互模式")
    ap.add_argument("--mock", action="store_true", help="全链路离线：假模型 + 模拟屏幕")
    ap.add_argument("--mock-llm", action="store_true", help="只把模型换成假回答，屏幕照常驱动")
    ap.add_argument("--mock-display", action="store_true", help="只把屏幕换成字符画，模型照常调用")
    ap.add_argument("--no-display", action="store_true", help="不驱动屏幕，只打印")
    ap.add_argument("--raw", action="store_true", help="打印模型原始输出")
    ap.add_argument("--check", action="store_true", help="只检查后端可达性后退出")
    ap.add_argument("--config", default=None, help="配置文件路径，默认自动找 config.yaml")
    ap.add_argument("--scale", type=int, default=0, help="字号，0=自动")
    ap.add_argument("--loop", action="store_true", help="强制进交互模式")
    ap.add_argument("--force-llm", action="store_true",
                    help="跳过「话里没数字就不调用模型」的规则快路径（做实验用）")
    args = ap.parse_args(argv)

    try:
        cfg = llm_mod.load_config(args.config)
    except llm_mod.ConfigError as exc:
        print("[ask] 配置错误: %s" % exc, file=sys.stderr)
        return EXIT_CONFIG

    if args.mock:
        cfg["llm"]["backend"] = "mock"
        cfg["display"]["driver"] = "mock"
    if args.mock_llm:
        cfg["llm"]["backend"] = "mock"
    if args.mock_display:
        cfg["display"]["driver"] = "mock"
    if args.no_display:
        cfg["display"]["driver"] = "none"

    try:
        cli = llm_mod.LLM(cfg)
    except (TypeError, ValueError) as exc:
        print("[ask] 配置错误: %s" % exc, file=sys.stderr)
        return EXIT_CONFIG

    if args.check:
        ok, detail = cli.check()
        print("[ask] backend=%s url=%s" % (cli.backend, cli.url))
        print("[ask] %s: %s" % ("在线" if ok else "不可用", detail))
        return EXIT_OK if ok else EXIT_LLM

    disp = display_mod.open_display(cfg.get("display", {}))
    addr = cfg["display"].get("address")
    try:
        addr = "0x%02X" % int(str(addr), 0)
    except (TypeError, ValueError):
        pass
    print("[ask] 屏幕 : %s  bus=%s addr=%s"
          % (disp.name, cfg["display"].get("bus"), addr))

    try:
        text = " ".join(args.text).strip()
        if text and not args.loop:
            return run_once(text, cli, disp, cfg, args.raw, args.force_llm)
        return run_loop(cli, disp, cfg, args.raw, args.force_llm)
    finally:
        disp.close()


if __name__ == "__main__":
    sys.exit(main())
