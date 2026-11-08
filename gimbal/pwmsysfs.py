"""备选后端：K1 硬件 PWM（/sys/class/pwm）。

用途：手上一时没有 PCA9685 时，用 40-pin 上的 PWM 通道先点亮云台。
限制（写进文档，别在答辩时被问倒）：
    1. 40-pin 上可用 PWM 通道少（一般 2 路），多路云台就要 PCA9685；
    2. sysfs 改 duty 是逐个文件写，多轴之间不同步，抖动约百微秒级——
       单轴静态定位够用，不适合做高速视觉伺服；
    3. 部分板子的 PWM 引脚与 GPIO/I2S 复用，改 pinmux 才能出波形，
       本项目正式方案仍是 PCA9685。

用法：
    backend = SysfsPwmBackend(chip=0, channels={"pan": 0, "tilt": 1})
    backend.set_pulse_us(0, 1500)
"""

import os
import time

PERIOD_NS = 20_000_000       # 50 Hz

BASE = "/sys/class/pwm"


class SysfsPwmBackend:
    def __init__(self, chip=0, period_ns=PERIOD_NS, invert=False):
        self.chip = chip
        self.period_ns = period_ns
        self.invert = invert
        self.root = os.path.join(BASE, "pwmchip%d" % chip)
        self._exported = set()
        if not os.path.isdir(self.root):
            raise OSError("找不到 %s，确认 PWM 通道已在内核里使能" % self.root)

    def _p(self, ch, leaf):
        return os.path.join(self.root, "pwm%d" % ch, leaf)

    def _wr(self, path, val):
        with open(path, "w") as f:
            f.write(str(val))

    def _rd(self, path):
        with open(path) as f:
            return f.read().strip()

    def export(self, ch):
        if ch in self._exported:
            return
        if not os.path.isdir(os.path.join(self.root, "pwm%d" % ch)):
            self._wr(os.path.join(self.root, "export"), ch)
            time.sleep(0.05)
        self._exported.add(ch)
        self._wr(self._p(ch, "period"), self.period_ns)
        pol = "inversed" if self.invert else "normal"
        try:
            self._wr(self._p(ch, "polarity"), pol)
        except OSError:
            pass

    def set_pulse_us(self, ch, us):
        self.export(ch)
        if us <= 0:
            try:
                self._wr(self._p(ch, "enable"), 0)
            except OSError:
                pass
            return
        duty = int(round(us * 1000.0))
        duty = max(0, min(self.period_ns, duty))
        self._wr(self._p(ch, "duty_cycle"), duty)
        self._wr(self._p(ch, "enable"), 1)

    def unexport(self, ch):
        try:
            self._wr(self._p(ch, "enable"), 0)
            self._wr(os.path.join(self.root, "unexport"), ch)
        except OSError:
            pass
        self._exported.discard(ch)
