#!/bin/bash
# 只读回读 PCA9685：CH0/CH1 的脉宽与 MODE1/MODE2。
# 注意：不要为了回读构造 PCA9685——构造函数会把 16 路先置 0，等于给舵机卸力。
set -eu
cd /home/biandu/rv
python3 - <<'PY'
from gimbal.i2c import I2CBus

def pulse_us(bus, ch):
    d = bus.read_regs(0x40, 0x06 + 4 * ch, 4)
    off = d[2] | ((d[3] & 0x0F) << 8)
    return off * 20000.0 / 4096.0

bus = I2CBus(4)
print('CH0=%.1fus  CH1=%.1fus' % (pulse_us(bus, 0), pulse_us(bus, 1)))
print('MODE1=0x%02X  MODE2=0x%02X' % (bus.read_reg(0x40, 0x00), bus.read_reg(0x40, 0x01)))
bus.close()
PY
