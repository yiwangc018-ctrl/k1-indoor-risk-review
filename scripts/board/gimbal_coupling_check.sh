#!/bin/bash
# 耦合终检：同一俯仰下，CH0 三个位置各拍一张，比较画面差；结束回中位并保持力矩。
set -eu
cd /home/biandu/rv
python3 - <<'PY'
import subprocess, time
from PIL import Image, ImageChops, ImageStat
from gimbal.i2c import I2CBus
from gimbal.pca9685 import PCA9685

bus = I2CBus(4)
pca = PCA9685(bus, 0x40, 50)
camera = '/dev/v4l/by-id/usb-Sonix_Technology_Co.__Ltd._UGREEN_Camera_1080P_SN0001-video-index0'

def shot(tag):
    path = '/tmp/final_%s.mjpg' % tag
    subprocess.run(['v4l2-ctl', '-d', camera,
                    '--set-fmt-video=width=960,height=540,pixelformat=MJPG',
                    '--stream-mmap', '--stream-count=1', '--stream-to=' + path],
                   check=True, capture_output=True, timeout=15)
    with Image.open(path) as im:
        return im.convert('L')

imgs = {}
for tag, us in (('center', 1500.0), ('min', 500.0), ('max', 2500.0), ('back', 1500.0)):
    pca.set_pulse_us(0, us)
    pca.set_pulse_us(1, 1500.0)
    time.sleep(1.2)
    imgs[tag] = shot(tag)
    print('CH0=%4.0fus → /tmp/final_%s.mjpg' % (us, tag), flush=True)

for a, b in (('center', 'min'), ('center', 'max'), ('min', 'max'), ('center', 'back')):
    print('%s vs %s 画面差=%.2f' % (a, b, ImageStat.Stat(ImageChops.difference(imgs[a], imgs[b])).mean[0]))

pca.set_pulse_us(0, 1500.0)
pca.set_pulse_us(1, 1500.0)
print('结束：CH0/CH1=1500us 中位，保持力矩', flush=True)
bus.close()
PY
