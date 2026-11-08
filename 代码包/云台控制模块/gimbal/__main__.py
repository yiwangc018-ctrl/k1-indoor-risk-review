"""正式云台辅助入口：离线自检或只读检查，不提供旧扫描/任意转动命令。"""
import argparse
import json
from pathlib import Path
from .i2c import I2CBus


def selftest():
    from .pca9685 import PCA9685, MODE1, PRESCALE
    class FakeBus:
        def __init__(self): self.regs = {}
        def write_reg(self, addr, reg, value): self.regs[reg] = value
        def read_reg(self, addr, reg): return self.regs.get(reg, 0)
        def read_regs(self, addr, reg, count): return [self.regs.get(reg + i, 0) for i in range(count)]
    bus = FakeBus()
    pca = PCA9685(bus)
    assert bus.regs[MODE1] & 0x20
    assert bus.regs[PRESCALE] == 121
    pca.set_pulse_us(15, 1500)
    assert abs(pca.read_pulse_us(15) - 1500) < 5
    print("gimbal selftest PASS (fake I2C; no motor access)")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("selftest", "read"))
    args = parser.parse_args()
    if args.command == "selftest": return selftest()
    cfg = json.loads(Path(__file__).with_name("config.json").read_text(encoding="utf-8"))
    bus = I2CBus(cfg["bus"])
    try:
        for name in ("pan", "tilt"):
            channel = cfg[name]["channel"]
            data = bus.read_regs(cfg["address"], 0x06 + 4 * channel, 4)
            ticks = data[2] | ((data[3] & 15) << 8)
            print(f"{name}: CH{channel} {ticks * 20000.0 / 4096.0:.1f} us (register value)")
    finally:
        bus.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
