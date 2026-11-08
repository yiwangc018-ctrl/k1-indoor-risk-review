"""PCA9685 16 路 12-bit PWM 驱动（50 Hz 舵机模式）。

为什么必须用 PCA9685、而不是 K1 的 GPIO 软件 PWM：
    Linux 不是实时系统，Python 线程的调度抖动在 1~10 ms。
    舵机脉冲周期 20 ms、有效脉宽差 1 ms 对应约 100°，5 ms 抖动就是 500° 的等效误差。
    PCA9685 自己在硬件里出 50 Hz / 12-bit，CPU 只负责"写一个数"。
"""

import time

MODE1 = 0x00
MODE2 = 0x01
PRESCALE = 0xFE
LED0_ON_L = 0x06

RESTART = 0x80
SLEEP = 0x10
ALLCALL = 0x01
AI = 0x20          # auto-increment
OUTDRV = 0x04      # totem pole


class PCA9685:
    def __init__(self, bus, addr=0x40, freq_hz=50.0):
        self.bus = bus
        self.addr = addr
        self.freq_hz = freq_hz
        self._init_chip()

    # ---------- 初始化 ----------
    def _init_chip(self):
        b, a = self.bus, self.addr
        b.write_reg(a, MODE2, OUTDRV)
        # 0x21 = ALLCALL | AI。AI 必须置 1，否则 4 字节连续写只会落到同一个寄存器。
        b.write_reg(a, MODE1, ALLCALL | AI)
        time.sleep(0.005)

        prescale = int(round(25_000_000.0 / (4096.0 * self.freq_hz))) - 1
        if not 3 <= prescale <= 255:
            raise ValueError("频率 %.1f Hz 超出 PCA9685 能力" % self.freq_hz)

        old = b.read_reg(a, MODE1)
        b.write_reg(a, MODE1, (old | SLEEP | AI) & ~RESTART)   # sleep 才能写 prescale
        b.write_reg(a, PRESCALE, prescale)
        time.sleep(0.005)
        b.write_reg(a, MODE1, (old | AI) & ~SLEEP)             # 唤醒，保持 AI
        time.sleep(0.005)
        b.write_reg(a, MODE1, ((old | AI) & ~SLEEP) | RESTART) # 重启 PWM
        time.sleep(0.005)

        for ch in range(16):                              # 全部先置 0（无脉冲）
            self.set_pulse_us(ch, 0)

    # ---------- 输出 ----------
    def set_ticks(self, ch, on, off):
        # 逐寄存器写，不依赖 AI 位，避免 PCA 只收到最后一个字节。
        reg = LED0_ON_L + 4 * ch
        for offset, value in enumerate((on & 0xFF, (on >> 8) & 0x0F,
                                        off & 0xFF, (off >> 8) & 0x0F)):
            self.bus.write_reg(self.addr, reg + offset, value)

    def set_pulse_us(self, ch, us):
        """us=0 表示不输出脉冲（舵机卸力）。"""
        if us <= 0:
            self.set_ticks(ch, 0, 0)
            return
        period_us = 1_000_000.0 / self.freq_hz
        ticks = int(round(us / period_us * 4096.0))
        ticks = max(0, min(4095, ticks))
        self.set_ticks(ch, 0, ticks)

    def read_pulse_us(self, ch):
        reg = LED0_ON_L + 4 * ch
        d = self.bus.read_regs(self.addr, reg, 4)
        off = d[2] | ((d[3] & 0x0F) << 8)
        return off * (1_000_000.0 / self.freq_hz) / 4096.0

    def sleep(self, enable=True):
        old = self.bus.read_reg(self.addr, MODE1)
        v = (old | SLEEP) if enable else (old & ~SLEEP)
        self.bus.write_reg(self.addr, MODE1, v)
