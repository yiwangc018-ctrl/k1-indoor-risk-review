"""PCA9685 driver offline test: no hardware needed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gimbal"))
from pca9685 import PCA9685, MODE1, MODE2, PRESCALE, LED0_ON_L


class FakeBus:
    def __init__(self):
        self.regs = {}

    def write_reg(self, addr, reg, val):
        self.regs[reg] = val

    def read_reg(self, addr, reg):
        return self.regs.get(reg, 0)

    def write_regs(self, addr, reg, values):
        for i, value in enumerate(values):
            self.regs[reg + i] = value

    def read_regs(self, addr, reg, count):
        return [self.regs.get(reg + i, 0) for i in range(count)]


def main():
    bus = FakeBus()
    pca = PCA9685(bus, 0x40, 50)
    assert bus.regs[MODE1] & 0x20, "AI bit not set"
    assert bus.regs[PRESCALE] == 121, bus.regs[PRESCALE]
    assert bus.regs[MODE2] == 0x04, bus.regs[MODE2]

    pca.set_pulse_us(0, 1500)
    pca.set_pulse_us(1, 1500)
    ch0 = [bus.regs[LED0_ON_L + i] for i in range(4)]
    ch1 = [bus.regs[LED0_ON_L + 4 + i] for i in range(4)]
    assert ch0 == [0, 0, 51, 1], ch0
    assert ch1 == [0, 0, 51, 1], ch1
    print("PCA driver test PASS")
    print("MODE1=0x%02X MODE2=0x%02X PRESCALE=%d" % (
        bus.regs[MODE1], bus.regs[MODE2], bus.regs[PRESCALE]))
    print("CH0=%s CH1=%s" % (ch0, ch1))


if __name__ == "__main__":
    main()
