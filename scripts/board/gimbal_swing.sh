#!/bin/bash
# 水平轴来回摆 6 次（500us↔2500us），用于肉眼确认"舵机盘转不转、支架/相机跟不跟"。
# 俯仰保持 1500us 中位。结束后两轴回 1500/1500 并保持力矩。
set -eu
cd /home/biandu/rv
python3 - <<'PY'
import json, time
from gimbal.i2c import I2CBus
from gimbal.pca9685 import PCA9685
from gimbal.servo import ServoSpec

cfg = json.load(open('gimbal/config.json', encoding='utf-8'))
tilt = ServoSpec.from_dict('tilt', cfg['servos']['tilt'])
bus = I2CBus(4)
pca = PCA9685(bus, 0x40, 50)
t_us, _ = tilt.axis_to_us(0.0)
pca.set_pulse_us(1, t_us)
print('俯仰保持 1500us；水平轴 500↔2500us 来回 6 次，每档 1.5s', flush=True)
for i in range(6):
    pca.set_pulse_us(0, 2500.0)
    print('%d/6 → 2500us（约 267°）' % (i + 1), flush=True)
    time.sleep(1.5)
    pca.set_pulse_us(0, 500.0)
    print('%d/6 → 500us（约 2°）' % (i + 1), flush=True)
    time.sleep(1.5)
pca.set_pulse_us(0, 1500.0)
pca.set_pulse_us(1, 1500.0)
print('结束回 1500/1500（中位，保持力矩）', flush=True)
bus.close()
PY
