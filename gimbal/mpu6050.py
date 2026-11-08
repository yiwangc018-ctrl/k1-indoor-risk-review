"""MPU6050（I2C 0x68）姿态读取。

在本项目里它承担两个职责，必须分清：

1) 俯仰角绝对反馈（tilt 闭环）
   加速度计测重力方向，得到的是**绝对**角度，没有累积误差。
   所以 tilt 用 MPU6050 闭环是成立的，而且能顺带消掉舵机下垂、回差、温漂。

2) 三脚架水平自检
   相机绕光轴的滚转角 roll ≈ 三脚架左右倾角，pan 绕竖直轴旋转不改变 roll，
   俯仰转动也不改变 roll。因此**同一颗 MPU6050 就能同时做倾角门禁和俯仰闭环**，
   不需要第二颗传感器、也不需要在底座再装一颗。

它做不到的事（要写清楚，否则方案会被挑）：
   **测不出绝对方位角 yaw**。陀螺仪积分会漂，加速度计没有 yaw 参考。
   所以 pan 的四个位置是"开环索引 + 限位归零"，不是靠 MPU6050 闭环。

坐标约定（可通过配置改符号/偏移适配实际安装方向）：
   pitch > 0 表示镜头朝上；roll > 0 表示画面右边低。
"""

import math
import time

ADDR = 0x68

PWR_MGMT_1 = 0x6B
SMPLRT_DIV = 0x19
CONFIG = 0x1A
GYRO_CONFIG = 0x1B
ACCEL_CONFIG = 0x1C
ACCEL_XOUT_H = 0x3B
WHO_AM_I = 0x75

ACCEL_LSB_PER_G = 16384.0      # ±2g
GYRO_LSB_PER_DPS = 131.0       # ±250 °/s


class MPU6050:
    def __init__(self, bus, addr=ADDR, pitch_sign=1.0, roll_sign=1.0,
                 pitch_offset=0.0, alpha=0.98, gyro_axis="y"):
        self.bus = bus
        self.addr = addr
        self.pitch_sign = pitch_sign
        self.roll_sign = roll_sign
        self.pitch_offset = pitch_offset
        self.alpha = alpha
        self.gyro_axis = gyro_axis
        self._fused = None
        self._last_t = None
        self.gyro_bias = {"x": 0.0, "y": 0.0, "z": 0.0}
        self._init()

    # ---------- 初始化 ----------
    def _init(self):
        self.bus.write_reg(self.addr, PWR_MGMT_1, 0x80)
        time.sleep(0.1)
        self.bus.write_reg(self.addr, PWR_MGMT_1, 0x01)     # PLL = X 轴陀螺
        time.sleep(0.05)
        self.bus.write_reg(self.addr, SMPLRT_DIV, 9)        # 1 kHz / 10 = 100 Hz
        self.bus.write_reg(self.addr, CONFIG, 0x03)         # DLPF 44 Hz
        self.bus.write_reg(self.addr, GYRO_CONFIG, 0x00)    # ±250 °/s
        self.bus.write_reg(self.addr, ACCEL_CONFIG, 0x00)   # ±2 g
        time.sleep(0.05)

    def whoami(self):
        return self.bus.read_reg(self.addr, WHO_AM_I)

    # ---------- 原始值 ----------
    def read_raw(self):
        d = self.bus.read_regs(self.addr, ACCEL_XOUT_H, 14)

        def s16(i):
            v = (d[i] << 8) | d[i + 1]
            return v - 65536 if v >= 32768 else v

        ax, ay, az = s16(0), s16(2), s16(4)
        temp = s16(6)
        gx, gy, gz = s16(8), s16(10), s16(12)
        return {
            "ax": ax / ACCEL_LSB_PER_G,
            "ay": ay / ACCEL_LSB_PER_G,
            "az": az / ACCEL_LSB_PER_G,
            "gx": gx / GYRO_LSB_PER_DPS,
            "gy": gy / GYRO_LSB_PER_DPS,
            "gz": gz / GYRO_LSB_PER_DPS,
            "temp_c": temp / 340.0 + 36.53,
        }

    # ---------- 姿态 ----------
    def read_acc_angles(self):
        r = self.read_raw()
        ax, ay, az = r["ax"], r["ay"], r["az"]
        pitch = math.degrees(math.atan2(-ax, math.sqrt(ay * ay + az * az)))
        roll = math.degrees(math.atan2(ay, az))
        return pitch, roll, r

    def read_pitch_roll(self, use_gyro=True):
        """返回 (pitch, roll)，已做符号与零点修正。"""
        if not use_gyro or self.alpha >= 0.999:
            p, r, _ = self.read_acc_angles()
            return self.pitch_sign * p + self.pitch_offset, self.roll_sign * r

        now = time.monotonic()
        p_acc, r_acc, raw = self.read_acc_angles()
        if self._fused is None or self._last_t is None:
            self._fused = p_acc
            self._last_t = now
            return self.pitch_sign * p_acc + self.pitch_offset, self.roll_sign * r_acc

        dt = max(1e-3, min(0.2, now - self._last_t))
        self._last_t = now
        rate = raw["g" + self.gyro_axis] - self.gyro_bias[self.gyro_axis]
        pred = self._fused + rate * dt
        self._fused = self.alpha * pred + (1.0 - self.alpha) * p_acc
        return self.pitch_sign * self._fused + self.pitch_offset, self.roll_sign * r_acc

    def calibrate_gyro(self, n=200, label=""):
        """静止状态采样，求零偏。上电后必须做一次，否则融合会漂。"""
        acc = {"x": 0.0, "y": 0.0, "z": 0.0}
        for _ in range(n):
            r = self.read_raw()
            acc["x"] += r["gx"]
            acc["y"] += r["gy"]
            acc["z"] += r["gz"]
            time.sleep(0.002)
        for k in acc:
            acc[k] /= n
        self.gyro_bias = acc
        print("[MPU6050]%s 陀螺零偏 x=%.2f y=%.2f z=%.2f °/s（采样 %d 次）"
              % (label, acc["x"], acc["y"], acc["z"], n))
        return acc

    def set_pitch_zero(self):
        """把当前俯仰角定为 0（用于安装后标定水平零位），返回写回的 offset。"""
        p, _ = self.read_acc_angles()
        self.pitch_offset = -self.pitch_sign * p
        return self.pitch_offset
