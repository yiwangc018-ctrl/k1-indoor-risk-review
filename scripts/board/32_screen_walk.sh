#!/usr/bin/env bash
# 屏幕体检：全亮 → 边框 → 四角 → 123，每步停 2.5 秒
# 用法：bash 32_screen_walk.sh
set -u
DELAY="${1:-2.5}"
cd "$(dirname "$0")" 2>/dev/null || true
python3 - "$DELAY" <<'PY'
import sys, time
sys.path.insert(0, "/home/biandu/rv/app")
import display as d

DELAY = float(sys.argv[1]) if len(sys.argv) > 1 else 2.5
disp = d.open_display(d._load_cfg())
print("[walk] 后端 =", disp.name)
if disp.name != "i2c":
    print("[walk] 没有真正驱动 I2C（看上面的降级原因），后面的画面不会出现")
    sys.exit(1)

def pause(msg):
    print("[walk]", msg, flush=True)
    time.sleep(DELAY)

# 1) 全亮：能看出屏有没有坏点/整块不亮
f = d.Frame()
for i in range(len(f.buf)):
    f.buf[i] = 0xFF
disp.show_frame(f)
pause("1/4 全亮（整屏白）")

# 2) 只有边框：验证列/页寻址，四边是否齐全
f = d.Frame(); f.border()
disp.show_frame(f)
pause("2/4 边框（应是一个完整的矩形）")

# 3) 四个角：验证方向，位置不对就要改 rotate
f = d.Frame()
for x, y in ((0, 0), (d.W - 4, 0), (0, d.H - 4), (d.W - 4, d.H - 4)):
    f.rect(x, y, 4, 4)
disp.show_frame(f)
pause("3/4 四角（应只在四个角）")

# 4) 文字
used = disp.show("123")
print("[walk] 4/4 文字 123（scale=%s）" % used)
disp.close()
PY
