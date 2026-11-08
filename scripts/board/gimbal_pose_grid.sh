#!/bin/bash
# 位姿网格：pan 45/135/225 × tilt -30/0/+30/+60 各拍一张，存 /tmp/grid。
# 用于人工挑"回正"位姿，或判断相机是否随 pan 转（不同 pan 的画面应明显不同）。
set -eu
cd /home/biandu/rv
rm -rf /tmp/grid && mkdir -p /tmp/grid
python3 - <<'PY'
import json, subprocess, time
from gimbal.i2c import I2CBus
from gimbal.pca9685 import PCA9685
from gimbal.servo import ServoSpec

cfg = json.load(open('gimbal/config.json', encoding='utf-8'))
pan = ServoSpec.from_dict('pan', cfg['servos']['pan'])
tilt = ServoSpec.from_dict('tilt', cfg['servos']['tilt'])
bus = I2CBus(4)
pca = PCA9685(bus, 0x40, 50)
camera = '/dev/v4l/by-id/usb-Sonix_Technology_Co.__Ltd._UGREEN_Camera_1080P_SN0001-video-index0'

def shot(name):
    path = '/tmp/grid/%s.jpg' % name
    subprocess.run(['v4l2-ctl', '-d', camera,
                    '--set-fmt-video=width=960,height=540,pixelformat=MJPG',
                    '--stream-mmap', '--stream-count=1', '--stream-to=' + path],
                   check=True, capture_output=True, timeout=15)

for p in (45.0, 135.0, 225.0):
    p_us, _ = pan.axis_to_us(p)
    pca.set_pulse_us(0, p_us)
    for t in (-30.0, 0.0, 30.0, 60.0):
        t_us, _ = tilt.axis_to_us(t)
        pca.set_pulse_us(1, t_us)
        time.sleep(0.9)
        shot('pan%03d_tilt%+03d' % (int(p), int(t)))
        print('拍：pan=%3.0f tilt=%+3.0f  (CH0=%.0fus CH1=%.0fus)' % (p, t, p_us, t_us), flush=True)

pca.set_pulse_us(0, 1500.0)
pca.set_pulse_us(1, 1500.0)
print('结束回 1500/1500', flush=True)
bus.close()
PY
ls /tmp/grid
