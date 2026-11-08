#!/usr/bin/env bash
# 初始化序列 A/B 试探：SSD1306 与 SH1106 各刷一次白屏 + 一个大字母
# 用法：bash 33_init_probe.sh [每步秒数，默认 4]
set -u
DELAY="${1:-4}"
python3 - "$DELAY" <<'PY'
import sys, time
sys.path.insert(0, "/home/biandu/rv/app")
import display as d

DELAY = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
BUS, ADDR = 4, 0x3C


def full_white(disp, label, glyph):
    f = d.Frame()
    for i in range(len(f.buf)):
        f.buf[i] = 0xFF
    disp.show_frame(f)
    print("[probe] %s：整屏白" % label, flush=True)
    time.sleep(DELAY)
    disp.show(glyph)          # 白屏后画一个大字母，验证寻址不只是"全亮"
    print("[probe] %s：白色大字母 %s" % (label, glyph), flush=True)
    time.sleep(DELAY)


print("[probe] 开始，两次各 %s 秒。先 A（SSD1306），后 B（SH1106）" % DELAY)

try:
    a = d.SSD1306Display(BUS, ADDR, contrast=255)
    full_white(a, "A / SSD1306", "A")
    a.close()
except Exception as exc:
    print("[probe] A 失败：%s" % exc)

time.sleep(0.5)

try:
    b = d.SH1106Display(BUS, ADDR, contrast=255)
    full_white(b, "B / SH1106", "B")
    b.close()
except Exception as exc:
    print("[probe] B 失败：%s" % exc)

print("[probe] 结束。请记住：A 亮还是 B 亮（或者都没亮）")
PY
