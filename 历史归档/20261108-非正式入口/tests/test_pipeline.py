#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PC 上的自测：不需要板子、不需要屏幕、不需要模型。

    python tests/test_pipeline.py

覆盖三件事：
  1) 字模/帧缓冲/居中/旋转 —— 显示层算得对不对
  2) 契约层抽取 —— 模型乱说话时能不能兜住
  3) 端到端 —— 假模型 + 模拟屏幕，退出码对不对

其中的字符画是可以"肉眼验收"的：'#' 构成 123 的形状，说明整条渲染链没问题。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

# Windows 控制台默认 GBK，中文会乱码；Linux 上本来就是 UTF-8，无副作用
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

import ask  # noqa: E402
import display  # noqa: E402
import llm  # noqa: E402

FAILS = []
PASSES = 0


def check(name, cond, detail=""):
    global PASSES
    if cond:
        PASSES += 1
        print("  ok   %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL %s  %s" % (name, detail))


def section(title):
    print("\n== %s ==" % title)


# ---------------------------------------------------------------------------
section("1. 字模完整性")
need = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ :.-+?!/%=<>_(),*"
missing = [ch for ch in need if ch not in display.GLYPHS]
check("所有用到的字符都有 5x7 字模", not missing, "缺: %r" % missing)
check("每个字模都是 5 列", all(len(g) == 5 for g in display.GLYPHS.values()))
check("小写自动映射到大写", display.glyph("a") == display.glyph("A"))
check("未知字符不炸，退化成空格", display.glyph("中") == display.glyph(" "))

# 逐个字符做像素对拍：字模表怎么写的，画出来就必须一模一样
mismatch = []
for ch in need:
    rows = display.FONT_5x7[ch]
    fr = display.Frame()
    fr.text(ch, 0, 0, 1)
    for r in range(7):
        for c in range(5):
            want = rows[r][c] == "1"
            got = bool((fr.buf[(r >> 3) * 128 + c] >> (r & 7)) & 1)
            if want != got:
                mismatch.append((ch, r, c))
check("每个字符的渲染与字模像素一致", not mismatch, str(mismatch[:6]))


# ---------------------------------------------------------------------------
section("2. 帧缓冲坐标")
f = display.Frame()
check("帧缓冲 1024 字节 (128x64/8)", len(f.buf) == 1024)
f.pixel(0, 0)
check("(0,0) -> buf[0] bit0", f.buf[0] == 0b0000_0001)
f.clear()
f.pixel(0, 7)
check("(0,7) -> buf[0] bit7", f.buf[0] == 0b1000_0000)
f.clear()
f.pixel(127, 63)
check("(127,63) -> 最后一页最后一列", f.buf[7 * 128 + 127] == 0b1000_0000)
f.clear()
f.pixel(-1, 0)
f.pixel(128, 0)
f.pixel(0, 64)
check("越界像素被丢弃而不是抛异常", f.lit_count() == 0)


# ---------------------------------------------------------------------------
section("3. 大字渲染 + 居中")
frame, scale = display.render_big("123")
tw, th = display.text_width("123", scale), display.text_height(scale)
x0, y0 = (128 - tw) // 2, (64 - th) // 2
check("自动选到最大字号 scale=6", scale == 6, "实际 %s" % scale)
check("占用 102x42 像素", (tw, th) == (102, 42), "实际 %sx%s" % (tw, th))
check("有像素被点亮", frame.lit_count() > 0)
# 内容必须落在居中框内（四周留白）
cols = [x for x in range(128) if any(frame.buf[p * 128 + x] for p in range(8))]
rows = [y for y in range(64)
        if any((frame.buf[(y >> 3) * 128 + x] >> (y & 7)) & 1 for x in range(128))]
check("水平方向在居中框内", cols and min(cols) >= x0 - 1 and max(cols) <= x0 + tw,
      "x %s..%s, 期望 %s..%s" % (min(cols), max(cols), x0, x0 + tw))
check("垂直方向在居中框内", rows and min(rows) >= y0 - 1 and max(rows) <= y0 + th,
      "y %s..%s, 期望 %s..%s" % (min(rows), max(rows), y0, y0 + th))

print("\n   屏幕效果（2:1 降采样，'#' = 亮）：")
for line in frame.ascii_art(2):
    print("   |%s|" % line)

print("\n   GOOD 的渲染（验证字母字模）：")
good, gscale = display.render_big("GOOD")
for line in good.ascii_art(2):
    print("   |%s|" % line)

long_ok, lscale = display.render_big("123456789012345678901")
check("21 字符长文本降为 scale=1 且不溢出", lscale == 1 and display.text_width("123456789012345678901", 1) <= 128)


# ---------------------------------------------------------------------------
section("4. 旋转 180（屏装反了）")
rot = frame.rotate180()
check("旋转后亮像素数不变", sum(bin(b).count("1") for b in rot) == frame.lit_count())
p = display.Frame()
p.pixel(0, 0)
check("(0,0) 转到 (127,63)", p.rotate180()[7 * 128 + 127] == 0b1000_0000)


# ---------------------------------------------------------------------------
section("5. 契约层：从模型回话里取数字")
cases = [
    ("123", "123", "clean"),
    ("  123\n", "123", "clean"),
    ("数字是123。", "123", "regex"),
    ("当然可以！结果是 456 哦", "456", "regex"),
    ("１２３", "123", "clean"),          # 全角数字
    ("1 2 3", "1", "regex"),             # 多个数字取第一个
    ("", None, "none"),
    ("我不太确定你问的是什么。", None, "none"),
    ("1234567", "12345", "regex"),       # 超过 5 位，截前 5 位
]
for raw, want, want_src in cases:
    got, src = llm.extract_number(raw)
    check("extract(%r) -> %r/%s" % (raw, want, want_src), (got, src) == (want, want_src),
          "实际 %r/%s" % (got, src))

check("numbers_in 取出用户话里的数字", llm.numbers_in("输出数字123") == ["123"])
check("numbers_in 没有数字时为空", llm.numbers_in("随便说点什么") == [])

# 中文数字：不然"输出数字一百二十三"会被规则判定拦成"没有数字"
for text, want in (("一二三", 123), ("十五", 15), ("二十三", 23), ("一百二十三", 123),
                   ("一千零八", 1008), ("二零二六", 2026), ("十", 10), ("二百零五", 205)):
    got = llm.cn_to_number(text)
    check("cn_to_number(%r) -> %r" % (text, want), got == want, "实际 %r" % got)
check("中文数字也进 numbers_in", llm.numbers_in("输出数字一百二十三") == ["123"],
      repr(llm.numbers_in("输出数字一百二十三")))
check("中文数字的话不会再被判成没数字",
      llm.numbers_in("输出数字两百") == ["200"], repr(llm.numbers_in("输出数字两百")))
check("extract_number 兜住中文数字", llm.extract_number("一百二十三") == ("123", "cn"),
      repr(llm.extract_number("一百二十三")))


# ---------------------------------------------------------------------------
section("6. 假模型（后端 mock）")
cfg = llm.load_config()
check("默认配置能加载",
      cfg["llm"]["backend"] in ("llamacpp", "ollama", "openai", "mock")
      and int(cfg["display"]["bus"]) == 4,
      "backend=%r bus=%r" % (cfg["llm"]["backend"], cfg["display"]["bus"]))

mock_cfg = json.loads(json.dumps(cfg))
mock_cfg["llm"]["backend"] = "mock"
cli = llm.LLM(mock_cfg)

os.environ.pop("MOCK_STYLE", None)
r = cli.ask("输出数字123")
check("mock clean：直接吐 123", (r.parsed, r.source) == ("123", "clean"), repr(r))

os.environ["MOCK_STYLE"] = "noisy"
r = cli.ask("输出数字123")
check("mock noisy：句子里带 123，靠正则兜住", (r.parsed, r.source) == ("123", "regex"), repr(r))

os.environ["MOCK_STYLE"] = "garbage"
r = cli.ask("输出数字123")
check("mock garbage：承认失败而不是瞎猜", r.parsed is None and r.source == "none", repr(r))
os.environ.pop("MOCK_STYLE", None)


# ---------------------------------------------------------------------------
section("7. 端到端（假模型 + 模拟屏幕）")
tmpdir = tempfile.mkdtemp(prefix="k1oled-")
log_path = os.path.join(tmpdir, "run.jsonl")
e2e_cfg = json.loads(json.dumps(mock_cfg))
e2e_cfg["app"]["log"] = log_path
e2e_cfg["display"]["driver"] = "mock"

disp = display.open_display(e2e_cfg["display"])
buf = io.StringIO()
with redirect_stdout(buf):
    code = ask.run_once("输出数字123", cli, disp, e2e_cfg)
check("成功链路退出码 0", code == 0, "实际 %s" % code)
check("字符画里出现了 123 的形状", buf.getvalue().count("#") > 50)

with open(log_path, encoding="utf-8") as fh:
    rec = json.loads(fh.readline())
check("日志记录了输入/输出/耗时", rec.get("input") == "输出数字123" and rec.get("parsed") == "123"
      and isinstance(rec.get("ms"), float), str(rec)[:120])

os.environ["MOCK_STYLE"] = "garbage"
with redirect_stdout(io.StringIO()):
    code = ask.run_once("输出数字123", cli, disp, e2e_cfg)
check("模型不说数字时退出码 3", code == 3, "实际 %s" % code)
os.environ.pop("MOCK_STYLE", None)

# 规则快路径：话里没有数字时，压根不调用模型（0.5B 会硬编一个数字出来）
called = {"n": 0}
real_ask = cli.ask


def spy(text):
    called["n"] += 1
    return real_ask(text)


cli.ask = spy
with redirect_stdout(io.StringIO()) as out2:
    code = ask.run_once("你好", cli, disp, e2e_cfg)
check("话里没有数字时退出码 3", code == 3, "实际 %s" % code)
check("话里没有数字时不调用模型", called["n"] == 0, "调用了 %d 次" % called["n"])
check("提示了不调用模型的原因", "没有数字" in out2.getvalue())

with redirect_stdout(io.StringIO()):
    code = ask.run_once("你好", cli, disp, e2e_cfg, force_llm=True)
check("--force-llm 可以强行走模型", called["n"] == 1, "调用了 %d 次" % called["n"])
cli.ask = real_ask

# 网络不通 → 退出码 2（换一个必定拒绝连接的端口）
dead_cfg = json.loads(json.dumps(mock_cfg))
dead_cfg["llm"]["backend"] = "llamacpp"
dead_cfg["llm"]["url"] = "http://127.0.0.1:9"
dead_cfg["llm"]["timeout"] = 0.5
dead_cli = llm.LLM(dead_cfg)
with redirect_stdout(io.StringIO()):
    code = ask.run_once("输出数字123", dead_cli, disp, e2e_cfg)
check("服务连不上时退出码 2（不卡死）", code == 2, "实际 %s" % code)


# ---------------------------------------------------------------------------
section("8. 后端降级：没有硬件也不该崩")
check("driver=none 得到空后端", isinstance(display.open_display({"driver": "none"}), display.NullDisplay))
check("driver=mock 得到模拟屏", isinstance(display.open_display({"driver": "mock"}), display.MockDisplay))
fallback = display.open_display({"driver": "ssd1306", "bus": 99, "address": "0x3C"})
check("i2c 打不开时降级为 mock 而不是抛异常", isinstance(fallback, display.MockDisplay),
      type(fallback).__name__)


# ---------------------------------------------------------------------------
section("9. CLI 入口")
tmpcfg = os.path.join(tmpdir, "config.yaml")
with open(tmpcfg, "w", encoding="utf-8") as fh:
    fh.write(
        "display:\n  driver: mock\n"
        "llm:\n  backend: mock\n  timeout: 1.0\n"
        "app:\n  log: %s\n" % os.path.join(tmpdir, "cli.jsonl").replace("\\", "/")
    )
with redirect_stdout(io.StringIO()) as out:
    code = ask.main(["--config", tmpcfg, "输出数字123"])
check("python3 ask.py --config ... '输出数字123' 返回 0", code == 0, "实际 %s" % code)
check("CLI 打印了契约结果", "契约" in out.getvalue() and "123" in out.getvalue())


# ---------------------------------------------------------------------------
print("\n%s" % ("-" * 60))
if FAILS:
    print("失败 %d 项 / 通过 %d 项" % (len(FAILS), PASSES))
    for name in FAILS:
        print("  - %s" % name)
    sys.exit(1)
print("全部通过：%d 项" % PASSES)
