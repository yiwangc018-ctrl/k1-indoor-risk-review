"""云台控制核心。

三层职责：
    1) 运动层：100 Hz 线程跑梯形速度规划，输出脉宽；
    2) 反馈层：用 MPU6050 的绝对俯仰角做 tilt 闭环，吃掉舵机下垂/回差/温漂；
    3) 门禁层：出图前检查"到位 + 消振 + 水平"，任一不满足就不许采图。

两条必须记住的边界：
    * pan 是开环索引（0/90/180/270 由舵机行程给出），MPU6050 测不出绝对方位角；
    * tilt 是闭环（目标俯仰角 → PI 修正舵机角），因为重力给了绝对参考。
"""

import math
import threading
import time

from .servo import Servo, ServoSpec


class GimbalError(RuntimeError):
    pass


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


class Gimbal:
    def __init__(self, cfg, backend, mpu=None, log=print):
        self.cfg = cfg
        self.log = log
        self.mpu = mpu

        sv = cfg["servos"]
        self.pan = Servo(ServoSpec.from_dict("pan", sv["pan"]), backend)
        self.tilt = Servo(ServoSpec.from_dict("tilt", sv["tilt"]), backend)

        c = cfg.get("control", {})
        self.loop_hz = float(c.get("loop_hz", 100))
        self.settle_s = float(c.get("settle_ms", 300)) / 1000.0
        self.tilt_kp = float(c.get("tilt_kp", 0.35))
        self.tilt_ki = float(c.get("tilt_ki", 0.0))
        self.tilt_deadband = float(c.get("tilt_deadband_deg", 0.3))
        self.tilt_step_max = float(c.get("tilt_step_max_deg", 2.0))
        self.tilt_timeout = float(c.get("tilt_timeout_s", 8.0))
        self.tilt_iter_s = float(c.get("tilt_iter_s", 0.25))

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._th = None
        self._settle_until = 0.0
        self._last_state = {}

    # ================= 生命周期 =================
    def start(self, park=None):
        if self._th is not None:
            return
        if park is not None:
            self.pan.set_target(park[0])
            self.tilt.set_target(park[1])
        self._stop.clear()
        self._th = threading.Thread(target=self._run, name="gimbal-ctrl", daemon=True)
        self._th.start()
        self.log("[Gimbal] 控制线程已启动 %.0f Hz" % self.loop_hz)

    def stop(self, relax=True):
        self._stop.set()
        if self._th is not None:
            self._th.join(timeout=1.0)
            self._th = None
        if relax:
            try:
                self.pan.relax()
                self.tilt.relax()
            except Exception:
                pass

    def _run(self):
        period = 1.0 / self.loop_hz
        last = time.monotonic()
        nxt = last
        while not self._stop.is_set():
            now = time.monotonic()
            dt = _clamp(now - last, 1e-3, 0.05)
            last = now
            try:
                with self._lock:
                    self.pan.update(dt)
                    self.pan.apply()
                    self.tilt.update(dt)
                    self.tilt.apply()
                    self._last_state = {
                        "pan": self.pan.cur,
                        "tilt": self.tilt.cur,
                        "pan_target": self.pan.target,
                        "tilt_target": self.tilt.target,
                        "at_target": self.pan.at_target() and self.tilt.at_target(),
                    }
            except Exception as e:                  # 单次 I2C 抖动不终止线程
                self.log("[Gimbal] 控制线程异常（已忽略）：%s" % e)
            nxt += period
            sleep = nxt - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                nxt = time.monotonic()      # 落后了就重新对齐，避免追帧雪崩

    # ================= 运动 =================
    def goto(self, pan=None, tilt=None, blocking=True, settle=True, timeout=30.0):
        report = []
        with self._lock:
            if pan is not None:
                self.pan.set_target(pan)
                report += self.pan.last_report
                self.pan.last_report = []
            if tilt is not None:
                self.tilt.set_target(tilt)
                report += self.tilt.last_report
                self.tilt.last_report = []
            self._settle_until = time.monotonic() + (self.settle_s if settle else 0.0)
        for r in report:
            self.log("[Gimbal] " + r)
        if blocking:
            return self.wait_settled(timeout)
        return True

    def nudge(self, pan_d=0.0, tilt_d=0.0, blocking=True):
        with self._lock:
            p = self.pan.target + pan_d
            t = self.tilt.target + tilt_d
        return self.goto(pan=p, tilt=t, blocking=blocking)

    def wait_settled(self, timeout=30.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            with self._lock:
                done = self.pan.at_target() and self.tilt.at_target()
            if done and time.monotonic() >= self._settle_until:
                return True
            time.sleep(0.01)
        self.log("[Gimbal] 等待到位超时（%.1fs），当前位置 pan=%.1f tilt=%.1f"
                 % (timeout, self.pan.cur, self.tilt.cur))
        return False

    def home(self, blocking=True):
        p = self.cfg.get("presets", {}).get("home", [0.0, 0.0])
        return self.goto(pan=p[0], tilt=p[1], blocking=blocking)

    def goto_preset_index(self, idx, blocking=True):
        presets = self.cfg.get("presets", {}).get("pan", [0, 90, 180, 270])
        idx = int(idx) % len(presets)
        return self.goto(pan=float(presets[idx]), blocking=blocking)

    def next_preset(self, blocking=True):
        presets = self.cfg.get("presets", {}).get("pan", [0, 90, 180, 270])
        cur = self.pan.target
        # 找严格大于当前、且差值超过 5° 的最小预设，绕回第一个
        cand = [p for p in presets if p > cur + 5.0]
        tgt = float(cand[0]) if cand else float(presets[0])
        return self.goto(pan=tgt, blocking=blocking)

    # ================= 反馈 =================
    def tilt_to_pitch(self, target_pitch, timeout=None, kp=None, ki=None,
                      deadband=None, step_max=None, verbose=False):
        """让相机实际俯仰角收敛到 target_pitch（绝对角度，度，抬头为正）。

        为什么是迭代而不是一次算完：
            舵机是位置伺服，内部已经有自己的闭环，我们只能"改指令再观察"。
            外环带宽远低于内环，所以每次只挪一点（step_max），等它稳了再测。
        """
        if self.mpu is None:
            raise GimbalError("没有 MPU6050，俯仰闭环不可用；请用 goto(tilt=...) 开环定位")

        kp = self.tilt_kp if kp is None else kp
        ki = self.tilt_ki if ki is None else ki
        deadband = self.tilt_deadband if deadband is None else deadband
        step_max = self.tilt_step_max if step_max is None else step_max
        timeout = self.tilt_timeout if timeout is None else timeout

        lo, hi = self.tilt.spec.axis_limits()
        cmd = self.tilt.target
        integ = 0.0
        hist = []
        t0 = time.monotonic()
        it = 0

        while time.monotonic() - t0 < timeout:
            p, _ = self.mpu.read_pitch_roll()
            err = target_pitch - p
            hist.append(err)
            if verbose:
                self.log("  [tilt闭环 %02d] 实测%.2f° 目标%.2f° 误差%+.2f° 指令%.2f°"
                         % (it, p, target_pitch, err, cmd))
            if abs(err) <= deadband:
                return {"ok": True, "iterations": it, "achieved": p,
                        "target": target_pitch, "error": err, "history": hist}

            # 发散保护：误差连续三步变大，几乎一定是方向配反了
            if len(hist) >= 3 and abs(hist[-1]) > abs(hist[-2]) > abs(hist[-3]) \
                    and abs(hist[-1]) > 2.0 * deadband:
                raise GimbalError(
                    "俯仰闭环发散：误差 %+.2f→%+.2f→%+.2f 持续增大。"
                    "检查 mount.pitch_sign 与 servos.tilt.invert 是否反了"
                    % (hist[-3], hist[-2], hist[-1]))

            integ += err * self.tilt_iter_s
            step = kp * err + ki * integ
            step = _clamp(step, -step_max, step_max)
            cmd = _clamp(cmd + step, lo, hi)
            self.goto(tilt=cmd, blocking=True, timeout=10.0)
            time.sleep(self.tilt_iter_s)
            it += 1

        p, _ = self.mpu.read_pitch_roll()
        return {"ok": False, "iterations": it, "achieved": p, "target": target_pitch,
                "error": target_pitch - p, "history": hist,
                "reason": "超时 %.1fs 未进入 %.2f° 死区（检查配重不足/舵机堵转/负载下坠）"
                          % (timeout, deadband)}

    # ================= 门禁 =================
    def level_gate(self, max_deg=None, require_mpu=None, ensure_tilt_zero=True,
                   check_pitch=None):
        """三脚架水平门禁。倾斜超限就不要开始扫描——否则地平线是歪的，
        "地面高差/门槛/湿滑"会全判错，拼接也会错行。

        判据只在地平参考位（tilt=0）检查前后倾，原因有两个：
          1) 扫描到 tilt=-35° 时实测俯仰本来就该是 -35°，拿它跟 0 比会 100% 误判
             （这条是仿真里踩出来的，不是拍脑袋）；
          2) 俯仰闭环会把三脚架的前后倾一起吃掉，此时"指令角 ≠ 目标俯仰角"是
             正常现象，不能当故障。
        左右倾 roll 不受 tilt 影响，任何时候都检查。
        """
        max_deg = float(self.cfg.get("safety", {}).get("level_max_deg", 0.5)) \
            if max_deg is None else max_deg
        if require_mpu is None:
            require_mpu = bool(self.cfg.get("safety", {}).get("require_mpu", True))
        if check_pitch is None:
            check_pitch = ensure_tilt_zero

        if self.mpu is None:
            return {"ok": not require_mpu, "roll": None, "pitch": None,
                    "reason": "no_mpu" if require_mpu else "no_mpu_but_allowed",
                    "max_deg": max_deg}

        if ensure_tilt_zero:
            self.goto(tilt=0.0, blocking=True, timeout=10.0)

        p, r = self.mpu.read_pitch_roll()
        bad = []
        if abs(r) > max_deg:
            bad.append("左右倾 %.2f° > %.2f°" % (r, max_deg))
        if check_pitch and abs(p) > max_deg:
            bad.append("前后倾 %.2f° > %.2f°" % (p, max_deg))
        return {"ok": not bad, "roll": r, "pitch": p, "max_deg": max_deg,
                "pitch_checked": bool(check_pitch),
                "reason": "；".join(bad) if bad else "ok"}

    def capture_gate(self):
        """采图前的一次性检查，返回 (ok, 原因列表)。

        只管三件事：到位没有、抖完没有、三脚架左右歪没歪。
        俯仰精度由 tilt_to_pitch 自己负责并单独返回结果，不在这里重复判。
        """
        reasons = []
        if not (self.pan.at_target() and self.tilt.at_target()):
            reasons.append("未到位")
        if time.monotonic() < self._settle_until:
            reasons.append("消振未完成")
        lg = self.level_gate(ensure_tilt_zero=False, check_pitch=False)
        if not lg["ok"]:
            reasons.append("水平：" + str(lg["reason"]))
        return (len(reasons) == 0), reasons

    # ================= 状态 =================
    def state(self):
        with self._lock:
            st = dict(self._last_state)
        st["pan_target"] = self.pan.target
        st["tilt_target"] = self.tilt.target
        if self.mpu is not None:
            try:
                p, r = self.mpu.read_pitch_roll()
                st["camera_pitch"] = p
                st["camera_roll"] = r
            except Exception as e:
                st["camera_pitch"] = None
                st["mpu_error"] = str(e)
        return st

    def describe(self):
        lines = ["== 云台配置 ==",
                 "  " + self.pan.spec.describe(),
                 "  " + self.tilt.spec.describe(),
                 "  loop=%.0f Hz  settle=%.0f ms  tilt_kp=%.2f 死区=%.2f°"
                 % (self.loop_hz, self.settle_s * 1000, self.tilt_kp, self.tilt_deadband)]
        presets = self.cfg.get("presets", {})
        lines.append("  pan 预设：%s" % presets.get("pan"))
        lines.append("  tilt 预设：%s" % presets.get("tilt"))
        return "\n".join(lines)
