"""舵机模型：角度→脉宽映射、软限位、梯形速度规划。

术语约定（全工程统一，别混）：
    轴角 axis    ：业务用角度。pan 是方位角 0~270（0=基准方向，顺时针为正）；
                   tilt 是俯仰角，抬头为正、低头为负，范围见配置。
    舵机角 servo ：舵机自己的 0~travel 刻度。servo = zero + (±1)*axis。
    脉宽 us      ：500~2500 典型。servo → us 是线性映射。

三套刻度分开，是为了后期换舵机、换零位时不改业务代码。
"""

import math
from dataclasses import dataclass


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


@dataclass
class ServoSpec:
    name: str
    channel: int
    travel_deg: float = 270.0        # 该舵机型号的机械行程（TBS-K15 有 180/270 两版）
    min_servo_deg: float = 2.0
    max_servo_deg: float = 267.0
    zero_deg: float = 0.0            # axis=0 对应的舵机角
    invert: bool = False
    min_us: float = 500.0
    max_us: float = 2500.0
    trim_us: float = 0.0             # 中位微调，正数整体右移
    speed_dps: float = 45.0          # 最大角速度
    accel_dps2: float = 180.0        # 角加速度

    @staticmethod
    def from_dict(name, d):
        return ServoSpec(
            name=name,
            channel=int(d["channel"]),
            travel_deg=float(d.get("travel_deg", 270.0)),
            min_servo_deg=float(d.get("min_servo_deg", 2.0)),
            max_servo_deg=float(d.get("max_servo_deg", 267.0)),
            zero_deg=float(d.get("zero_deg", 0.0)),
            invert=bool(d.get("invert", False)),
            min_us=float(d.get("min_us", 500.0)),
            max_us=float(d.get("max_us", 2500.0)),
            trim_us=float(d.get("trim_us", 0.0)),
            speed_dps=float(d.get("speed_dps", 45.0)),
            accel_dps2=float(d.get("accel_dps2", 180.0)),
        )

    # ---------- 刻度换算 ----------
    def axis(self, servo_deg):
        s = -1.0 if self.invert else 1.0
        return (servo_deg - self.zero_deg) * s

    def axis_limits(self):
        a1 = self.axis(self.min_servo_deg)
        a2 = self.axis(self.max_servo_deg)
        return (min(a1, a2), max(a1, a2))

    def servo(self, axis_deg):
        s = -1.0 if self.invert else 1.0
        return self.zero_deg + s * axis_deg

    def axis_to_us(self, axis_deg, report=None):
        lo, hi = self.axis_limits()
        a = axis_deg
        if a < lo or a > hi:
            a = clamp(a, lo, hi)
            if report is not None:
                report.append("%s: 目标 %.1f° 超出软限位 [%.1f, %.1f]，已夹到 %.1f°"
                              % (self.name, axis_deg, lo, hi, a))
        sd = self.servo(a)
        us = self.min_us + (self.max_us - self.min_us) * sd / self.travel_deg
        return us + self.trim_us, a

    def describe(self):
        lo, hi = self.axis_limits()
        return ("%-4s ch%d  travel=%.0f°  axis=[%.1f, %.1f]  us=[%.0f, %.0f]  "
                "zero=%.1f°  invert=%s"
                % (self.name, self.channel, self.travel_deg, lo, hi,
                   self.min_us, self.max_us, self.zero_deg, self.invert))


class Servo:
    """单轴运行时：保存当前位置，按梯形速度逼近目标。"""

    def __init__(self, spec, backend):
        self.spec = spec
        self.backend = backend
        lo, hi = spec.axis_limits()
        self.cur = clamp(0.0, lo, hi)
        self.target = self.cur
        self.rate = 0.0            # 当前角速度（°/s）
        self.last_report = []
        self._warned = set()
        self._us, _ = spec.axis_to_us(self.cur)
        self._last_written = None
        self.backend.set_pulse_us(spec.channel, self._us)

    # ---------- 指令 ----------
    def set_target(self, axis_deg):
        lo, hi = self.spec.axis_limits()
        a = clamp(axis_deg, lo, hi)
        if abs(a - axis_deg) > 1e-6:
            # 同一个越界目标只提示一次，避免"0/270 四方位"每次都刷屏
            key = round(float(axis_deg), 1)
            if key not in self._warned:
                self._warned.add(key)
                self.last_report.append(
                    "%s: 目标 %.1f° 超出软限位 [%.1f, %.1f]，已夹到 %.1f°"
                    "（如需要，改 config 的 min/max_servo_deg 或 zero_deg）"
                    % (self.spec.name, axis_deg, lo, hi, a))
        self.target = a

    def at_target(self, tol=0.4):
        return abs(self.target - self.cur) <= tol

    # ---------- 每拍推进 ----------
    def update(self, dt):
        err = self.target - self.cur
        if abs(err) < 1e-3:
            self.cur = self.target
            self.rate = 0.0
            return False

        sp = self.spec
        # 减速段允许速度：v = sqrt(2*a*s)，避免冲过头
        v_decel = math.sqrt(max(0.0, 2.0 * sp.accel_dps2 * abs(err)))
        v_want = min(sp.speed_dps, v_decel)
        direction = 1.0 if err > 0 else -1.0

        # 加速度受限地调整速率
        dv = sp.accel_dps2 * dt
        if self.rate * direction < v_want - 1e-9:
            self.rate += direction * dv
        if abs(self.rate) > v_want:
            self.rate = direction * v_want

        step = self.rate * dt
        if abs(step) >= abs(err):
            self.cur = self.target
            self.rate = 0.0
        else:
            self.cur += step
        return True

    def apply(self):
        us, _ = self.spec.axis_to_us(self.cur)
        # 只在脉宽变化超过 0.5 us（约 0.06°）时才写 I2C，
        # 避免 100 Hz 控制线程把总线占满，抢 MPU6050 的读窗口。
        if self._last_written is None or abs(us - self._last_written) > 0.5:
            self.backend.set_pulse_us(self.spec.channel, us)
            self._last_written = us
        self._us = us
        return us

    def relax(self):
        """停止输出脉冲，舵机卸力（调试或搬运前用）。"""
        self.backend.set_pulse_us(self.spec.channel, 0)
        self._last_written = 0.0


class FakeBackend:
    """无硬件时记录脉冲值，用于 selftest / 离线联调。"""

    def __init__(self, channels=16):
        self.pulses = {c: 0.0 for c in range(channels)}
        self.log = []

    def set_pulse_us(self, ch, us):
        self.pulses[ch] = us
        self.log.append((ch, us))
