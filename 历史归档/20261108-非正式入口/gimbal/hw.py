"""硬件装配：按配置探测 I2C 总线、挂载 PCA9685 与 MPU6050。

设计原则：**探测失败必须说清楚失败在哪一步**，而不是抛一个 "device not found"。
现场调试 90% 的时间花在"到底是没接上、地址错了、还是没权限"，所以这里
把每一步的结论都收进 self.notes，让 `python -m gimbal detect` 一次讲清。
"""

import os

from .i2c import I2CBus, I2CError, list_buses, scan
from .pca9685 import PCA9685
from .mpu6050 import MPU6050
from .pwmsysfs import SysfsPwmBackend


class Hardware:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self.bus = None
        self.bus_num = None
        self.mpu_bus = None
        self.mpu_bus_num = None
        self.backend = None
        self.mpu = None
        self.notes = []

        ic = cfg.get("i2c", {})
        want_pca = int(ic.get("pca9685_addr", 0x40))
        want_mpu = int(ic.get("mpu6050_addr", 0x68))
        mode = ic.get("backend", "auto")

        candidates = []
        for b in [ic.get("bus")] + list(ic.get("bus_candidates", [4, 3])):
            if b is not None and int(b) not in candidates:
                candidates.append(int(b))
        avail = list_buses()
        if not avail:
            self.notes.append("没有找到任何 /dev/i2c-N：确认系统已加载 i2c 驱动")
        else:
            self.notes.append("系统可用 I2C 总线：%s" % avail)

        # ---- 找挂着 PCA9685 或 MPU6050 的那条总线 ----
        self.bus_num = self._pick_bus(candidates, [want_pca, want_mpu])
        if self.bus_num is not None:
            try:
                self.bus = I2CBus(self.bus_num)
                found = scan(self.bus_num)
                self.notes.append("总线 i2c-%d 上应答的地址：%s"
                                  % (self.bus_num,
                                     ["0x%02X" % a for a in found] or "无"))
            except (OSError, I2CError) as e:
                self.notes.append("打开 i2c-%d 失败：%s" % (self.bus_num, e))

        # ---- MPU6050 可单独走另一条总线（例如 PCA 在 i2c-4，MPU 在 i2c-3）----
        mpu_bus_cfg = ic.get("mpu_bus")
        self.mpu_bus_num = int(mpu_bus_cfg) if mpu_bus_cfg is not None else self.bus_num
        if self.mpu_bus_num is not None:
            if self.bus is not None and self.mpu_bus_num == self.bus_num:
                self.mpu_bus = self.bus
            else:
                try:
                    self.mpu_bus = I2CBus(self.mpu_bus_num)
                    found = scan(self.mpu_bus_num)
                    self.notes.append("MPU 总线 i2c-%d 上应答：%s"
                                      % (self.mpu_bus_num,
                                         ["0x%02X" % a for a in found] or "无"))
                except (OSError, I2CError) as e:
                    self.notes.append("打开 MPU 总线 i2c-%d 失败：%s"
                                      % (self.mpu_bus_num, e))

        # ---- PWM 后端 ----
        if mode in ("auto", "pca9685") and self.bus is not None:
            if want_pca in scan(self.bus_num, [want_pca]):
                try:
                    self.backend = PCA9685(self.bus, want_pca)
                    self.notes.append("PCA9685 已初始化（0x%02X @ i2c-%d，50 Hz，12-bit）"
                                      % (want_pca, self.bus_num))
                except (OSError, I2CError) as e:
                    self.notes.append("PCA9685 初始化失败：%s" % e)
            else:
                self.notes.append("0x%02X 无应答：PCA9685 未接、未供电，或地址被改（A0~A5）"
                                  % want_pca)

        if self.backend is None and mode in ("auto", "sysfs"):
            chip = int(ic.get("pwm_chip", 0))
            try:
                self.backend = SysfsPwmBackend(chip=chip)
                self.notes.append("回退到硬件 PWM：/sys/class/pwm/pwmchip%d" % chip)
            except OSError as e:
                self.notes.append("硬件 PWM 不可用：%s" % e)

        # ---- MPU6050 ----
        if (self.mpu_bus is not None
                and self.mpu_bus_num is not None
                and want_mpu in scan(self.mpu_bus_num, [want_mpu])):
            mnt = cfg.get("mount", {})
            try:
                self.mpu = MPU6050(
                    self.mpu_bus, want_mpu,
                    pitch_sign=float(mnt.get("pitch_sign", 1.0)),
                    roll_sign=float(mnt.get("roll_sign", 1.0)),
                    pitch_offset=float(mnt.get("pitch_offset_deg", 0.0)),
                    alpha=float(mnt.get("alpha", 0.98)),
                    gyro_axis=mnt.get("gyro_axis", "y"),
                )
                who = self.mpu.whoami()
                self.notes.append("MPU6050 已就绪（WHO_AM_I=0x%02X%s，i2c-%d）"
                                  % (who, "" if who == 0x68 else "，非 0x68，疑为兼容片",
                                     self.mpu_bus_num))
            except (OSError, I2CError) as e:
                self.notes.append("MPU6050 通信失败：%s" % e)
        elif self.mpu_bus is not None:
            self.notes.append("0x%02X 无应答：MPU6050 未接或 SDA/SCL 反接（i2c-%s）"
                              % (want_mpu, self.mpu_bus_num))

    def _pick_bus(self, candidates, addrs):
        avail = list_buses()
        for b in candidates:
            if b not in avail:
                continue
            if any(a in scan(b, [a]) for a in addrs):
                return b
        for b in candidates:
            if b in avail:
                return b
        return avail[0] if avail else None

    # ---------- 汇总 ----------
    def ok(self):
        return self.backend is not None

    def report(self):
        lines = ["== 硬件探测 =="]
        lines += ["  " + n for n in self.notes]
        lines.append("  PWM 后端：%s" % ("OK" if self.backend else "失败"))
        lines.append("  MPU6050 ：%s" % ("OK" if self.mpu else "不可用（俯仰闭环将退化为开环）"))
        return "\n".join(lines)

    def close(self):
        if self.mpu_bus is not None and self.mpu_bus is not self.bus:
            self.mpu_bus.close()
        self.mpu_bus = None
        if self.bus is not None:
            self.bus.close()
            self.bus = None


def ensure_i2c_permission(log=print):
    """给出权限提示，不自动改系统（改权限属于用户决定）。"""
    tips = []
    for n in list_buses():
        p = "/dev/i2c-%d" % n
        if not os.access(p, os.R_OK | os.W_OK):
            tips.append("  %s 不可读写 → sudo chmod a+rw %s  或 sudo usermod -aG i2c $USER" % (p, p))
    if tips:
        log("I2C 权限不足：")
        for t in tips:
            log(t)
    return tips
