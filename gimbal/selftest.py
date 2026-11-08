"""离线自检：不需要任何硬件，在 PC 或板子上都能跑。

覆盖四类最容易出错、且错了以后极难在台架上定位的地方：
    1) 中文数字与意图解析（语音链路的地基）
    2) 角度↔脉宽换算与软限位（换舵机后第一件事）
    3) 梯形速度规划（会不会冲过头、会不会永远到不了）
    4) 俯仰闭环（含"方向配反要能报错"这一条）
    5) 四方位覆盖算术（报告里"无盲区"这句话站不站得住）
"""

import json
import os
import time

from .control import Gimbal, GimbalError
from .scan import coverage_gaps
from .servo import FakeBackend, Servo, ServoSpec
from .voice import cn2num, parse

_ok = [0]
_fail = [0]


def check(name, cond, detail=""):
    if cond:
        _ok[0] += 1
        print("  PASS  %s" % name)
    else:
        _fail[0] += 1
        print("  FAIL  %s   %s" % (name, detail))


def near(a, b, tol=0.51):
    return abs(a - b) <= tol


# ---------------- 1. 语音 ----------------
def test_numbers():
    print("[1] 中文数字")
    cases = {"零": 0, "九": 9, "十": 10, "三十": 30, "四十五": 45,
             "九十": 90, "一百八十": 180, "二百七十": 270, "180": 180,
             "负三十": -30, "两": 2}
    for s, want in cases.items():
        got = cn2num(s)
        check("cn2num(%s)=%s" % (s, want), got == want, "实得 %r" % (got,))


def test_intents():
    print("[2] 意图解析")
    cases = [
        ("转到90度", "PAN_ABS", 90),
        ("转到九十度", "PAN_ABS", 90),
        ("方位270度", "PAN_ABS", 270),
        ("到180度", "PAN_ABS", 180),
        ("俯角30度", "TILT_ABS", -30.0),
        ("低头45度", "TILT_ABS", -45.0),
        ("仰角20度", "TILT_ABS", 20.0),
        ("抬头15度", "TILT_ABS", 15.0),
        ("左转15度", "PAN_REL", -15.0),
        ("向右", "PAN_REL", 15.0),
        ("回中", "HOME", None),
        ("开始扫描", "SHOOT", None),
        ("拍照", "SHOOT", None),
        ("拍四张", "SHOOT", None),
        ("完整扫描", "SCAN_FULL", None),
        ("三环", "SCAN_FULL", None),
        ("拍一张", "CAPTURE_ONE", None),
        ("检查水平", "LEVEL", None),
        ("下一个", "NEXT", None),
    ]
    for text, kind, val in cases:
        it = parse(text)
        ok = it.kind == kind and (val is None or near(float(it.value), val, 1e-6))
        check("parse(%s) → %s" % (text, kind), ok, "实得 %r" % (it,))

    it = parse("稳定在俯仰-30度")
    check("parse(稳定在俯仰-30度) → TILT_PITCH(-30)", it.kind == "TILT_PITCH"
          and near(float(it.value), -30, 1e-6), "实得 %r" % (it,))


# ---------------- 2. 刻度换算 ----------------
def test_mapping():
    print("[3] 角度↔脉宽")
    sp = ServoSpec("pan", 0, travel_deg=270, min_servo_deg=0, max_servo_deg=270,
                   zero_deg=0, min_us=500, max_us=2500)
    check("pan 0° → 500us", near(sp.axis_to_us(0)[0], 500, 1))
    check("pan 135° → 1500us", near(sp.axis_to_us(135)[0], 1500, 1))
    check("pan 270° → 2500us", near(sp.axis_to_us(270)[0], 2500, 1))

    sp2 = ServoSpec("pan", 0, travel_deg=270, min_servo_deg=2, max_servo_deg=267,
                    zero_deg=0, min_us=500, max_us=2500)
    us, a = sp2.axis_to_us(280)
    check("软限位夹紧 280→267", near(a, 267, 0.01), "实得 %.2f" % a)

    sp3 = ServoSpec("tilt", 1, travel_deg=180, min_servo_deg=30, max_servo_deg=150,
                    zero_deg=90, min_us=500, max_us=2500)
    check("tilt 轴 0° → 舵机 90°", near(sp3.servo(0), 90, 1e-6))
    check("tilt 轴 -60° → 舵机 30°", near(sp3.servo(-60), 30, 1e-6))
    lo, hi = sp3.axis_limits()
    check("tilt 轴范围 = [-60, 60]", near(lo, -60, 1e-6) and near(hi, 60, 1e-6),
          "实得 [%.1f, %.1f]" % (lo, hi))

    sp4 = ServoSpec("pan", 0, travel_deg=270, min_servo_deg=0, max_servo_deg=270,
                    zero_deg=135, invert=True, min_us=500, max_us=2500)
    check("invert: 轴 0° → 舵机 135° → 1500us",
          near(sp4.axis_to_us(0)[0], 1500, 1), "实得 %.1f" % sp4.axis_to_us(0)[0])
    check("invert: 轴 +90° → 舵机 45° → 833us",
          near(sp4.axis_to_us(90)[0], 833.3, 1), "实得 %.1f" % sp4.axis_to_us(90)[0])


# ---------------- 3. 速度规划 ----------------
def test_slew():
    print("[4] 梯形速度规划")
    sp = ServoSpec("pan", 0, min_servo_deg=0, max_servo_deg=270, min_us=500,
                   max_us=2500, speed_dps=90, accel_dps2=270)
    s = Servo(sp, FakeBackend())
    s.set_target(90)
    dt = 0.01
    max_step = 0.0
    t0 = time.monotonic()
    while not s.at_target(0.2) and time.monotonic() - t0 < 5:
        prev = s.cur
        s.update(dt)
        max_step = max(max_step, abs(s.cur - prev))
        s.apply()
    check("90° 内到达（%.2fs）" % (time.monotonic() - t0), s.at_target(0.2),
          "实得 %.2f°" % s.cur)
    check("单步不超过最大速度", max_step <= 90 * dt + 1e-6,
          "单步 %.4f° > %.4f°" % (max_step, 90 * dt))
    check("不过冲", s.cur <= 90 + 0.2, "实得 %.2f°" % s.cur)

    s2 = Servo(sp, FakeBackend())
    s2.set_target(30)
    for _ in range(2000):
        s2.update(0.01)
    check("长时间运行后精确停在目标", near(s2.cur, 30, 1e-6), "实得 %.4f" % s2.cur)


# ---------------- 4. 闭环 ----------------
class FakePlant:
    """假 MPU6050：实测俯仰 = 指令角 × sag，模拟负载下垂。"""

    def __init__(self, sag=0.9, sign=1.0, roll=0.1):
        self.g = None
        self.sag = sag
        self.sign = sign
        self.roll = roll
        self.reads = 0

    def read_pitch_roll(self):
        self.reads += 1
        return self.sign * self.g.tilt.cur * self.sag, self.roll


def make_cfg(tmp_dir):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["control"]["tilt_iter_s"] = 0.01
    cfg["control"]["tilt_timeout_s"] = 6.0
    cfg["control"]["loop_hz"] = 200
    cfg["servos"]["pan"]["speed_dps"] = 270
    cfg["servos"]["pan"]["accel_dps2"] = 2000
    cfg["servos"]["tilt"]["speed_dps"] = 270
    cfg["servos"]["tilt"]["accel_dps2"] = 2000
    cfg["control"]["settle_ms"] = 50
    return cfg


def test_closed_loop(tmp_dir):
    print("[5] 俯仰闭环（含发散保护）")
    cfg = make_cfg(tmp_dir)
    plant = FakePlant(sag=0.9)
    g = Gimbal(cfg, FakeBackend(), mpu=plant, log=lambda *a: None)
    plant.g = g
    g.start()

    ok = g.goto(pan=90, tilt=-30, blocking=True, timeout=10)
    check("goto 到位", ok and near(g.pan.cur, 90, 1) and near(g.tilt.cur, -30, 1),
          "pan=%.1f tilt=%.1f" % (g.pan.cur, g.tilt.cur))

    r = g.tilt_to_pitch(-30.0)
    check("闭环收敛到 -30°", r["ok"] and abs(r["error"]) <= 0.35,
          "实得 %.2f° 误差%.2f" % (r["achieved"], r["error"]))
    check("闭环确实补偿了下垂（指令角 ≠ -30）",
          abs(g.tilt.cur - (-30.0)) > 1.0, "指令角 %.2f°" % g.tilt.cur)

    lg = g.level_gate()
    check("水平门禁放行（roll=0.1°）", lg["ok"], str(lg))

    g.stop(relax=False)

    # 方向配反 → 必须报错而不是把舵机顶到限位
    cfg2 = make_cfg(tmp_dir)
    plant2 = FakePlant(sag=1.0, sign=-1.0)
    g2 = Gimbal(cfg2, FakeBackend(), mpu=plant2, log=lambda *a: None)
    plant2.g = g2
    g2.start(park=None)
    raised = False
    try:
        g2.tilt_to_pitch(20.0, timeout=4.0)
    except GimbalError as e:
        raised = "发散" in str(e)
    check("方向配反时报「发散」而不是死顶限位", raised)
    g2.stop(relax=False)


# ---------------- 5. 覆盖算术 ----------------
def test_coverage():
    print("[6] 四方位覆盖算术")
    az4 = [2.0, 91.3, 180.7, 267.0]
    g110 = coverage_gaps(az4, 110.0)
    check("4 方位 × 110° 视场 → 无盲区", len(g110) == 0, "缺口 %s" % (g110,))

    # 关键约束：pan 软限位 2~267°，机械扫过的方位跨度只有 265°，
    # 所以 HFOV 必须 ≥ 360-265 = 95°，否则**加多少方位都补不上**。
    g95 = coverage_gaps(az4, 96.0)
    check("4 方位 × 96° 视场 → 刚好闭合", len(g95) == 0, "缺口 %s" % (g95,))

    g76 = coverage_gaps(az4, 76.0)
    total = sum(b - a for a, b in g76)
    check("4 方位 × 76° 视场 → 有缺口（应告警）", total > 0, "缺口合计 %.1f°" % total)

    az8 = [2 + (265.0 * i / 7) for i in range(8)]
    g76b = coverage_gaps(az8, 76.0)
    check("8 方位 × 76° 视场 → 仍有缺口（加方位救不了窄视场）",
          len(g76b) > 0, "缺口 %s" % (g76b,))

    az12 = [2 + (265.0 * i / 11) for i in range(12)]
    g76c = coverage_gaps(az12, 76.0)
    check("12 方位 × 76° 视场 → 缺口依旧存在，只被摊薄",
          len(g76c) > 0, "缺口 %s" % (g76c,))


def test_default_plan(tmp_dir):
    print("[7] 默认拍摄计划（4 张 / 俯角 30°）")
    from .scan import default_plan

    cfg = make_cfg(tmp_dir)
    p = default_plan(cfg)                       # 不传 mode，应走配置里的默认
    check("默认模式 = quick", p["mode"] == "quick", "实得 %s" % p["mode"])
    check("默认 1 环 × 4 方位 = 4 张",
          p["count"] == 4 and len(p["rings"]) == 1,
          "实得 %d 环 %d 方位" % (len(p["rings"]), len(p["azimuths"])))
    check("默认俯角 30°（tilt=-30）", near(p["rings"][0], -30, 1e-6),
          "实得 %s" % p["rings"])
    check("方位取预设 0/90/180/270 并夹到软限位",
          p["azimuths"] == [2.0, 90.0, 180.0, 267.0], "实得 %s" % p["azimuths"])

    pf = default_plan(cfg, "full")
    check("full 模式 = 3 环 × 4 方位 = 12 张", pf["count"] == 12,
          "实得 %d" % pf["count"])
    check("full 模式含俯视/平视/仰视三环",
          near(pf["rings"][0], -35) and near(pf["rings"][1], 0.0)
          and near(pf["rings"][2], 25.0), "实得 %s" % pf["rings"])

    bad = False
    try:
        default_plan(cfg, "nope")
    except ValueError:
        bad = True
    check("未知模式报错而不是静默跑错协议", bad)


def test_vertical():
    print("[8] 垂直覆盖（俯角 30° 到底能看见什么）")
    from .scan import vfov_from_hfov, ground_span

    vf = vfov_from_hfov(110.0, 16.0 / 9.0)
    check("HFOV 110° / 16:9 → 垂视场 ≈78°", 76.0 <= vf <= 80.0, "实得 %.1f°" % vf)

    g = ground_span(1.4, -30.0, vf)
    check("机高 1.4 m、俯角 30° → 地面从约 0.5 m 起可见",
          g["near_m"] is not None and 0.45 <= g["near_m"] <= 0.65,
          "实得 %s" % g["near_m"])
    check("俯角 30° 时地平线入画（远处墙脚能看到）", g["sees_horizon"], str(g))
    check("墙面只到约 1.6 m，天花板看不到",
          g["wall_top_abs_m"] is not None and 1.45 <= g["wall_top_abs_m"] <= 1.80,
          "实得 %s m" % g["wall_top_abs_m"])

    g45 = ground_span(1.4, -45.0, vf)
    check("俯角 45° 时脚边更近（近距更清楚）",
          g45["near_m"] < g["near_m"], "%.2f vs %.2f" % (g45["near_m"], g["near_m"]))


class _StubG:
    """只实现 Dispatcher 用到的方法，不启线程、不碰硬件。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.pan = type("Ax", (), {"target": 0.0, "cur": 0.0})()
        self.tilt = type("Ax", (), {"target": 0.0, "cur": 0.0})()

    def goto(self, pan=None, tilt=None, **kw):
        if pan is not None:
            self.pan.target = self.pan.cur = float(pan)
        if tilt is not None:
            self.tilt.target = self.tilt.cur = float(tilt)
        return True

    def nudge(self, pan_d=None, tilt_d=None, **kw):
        return self.goto(pan=None if pan_d is None else self.pan.target + pan_d,
                         tilt=None if tilt_d is None else self.tilt.target + tilt_d)

    def home(self):
        return self.goto(pan=0.0, tilt=0.0)

    def capture_gate(self):
        return True, []

    def tilt_to_pitch(self, deg, **kw):
        self.goto(tilt=deg)
        return {"ok": True, "achieved": deg, "target": deg, "error": 0.0}

    def state(self):
        return {"pan_target": self.pan.target, "tilt_target": self.tilt.target}

    def stop(self, **kw):
        pass


def test_voice_scan_tilt(tmp_dir):
    print("[9] 语音 → 默认四张 / 俯角沿用")
    from .voice import Dispatcher

    cfg = make_cfg(tmp_dir)
    g = _StubG(cfg)
    scans, ones = [], []

    def on_scan(mode="quick", tilt=None):
        scans.append((mode, tilt))
        return "ok"

    def on_capture():
        ones.append(1)
        return "one"

    d = Dispatcher(g, log=lambda *a: None, on_scan=on_scan, on_capture=on_capture)

    d.handle(parse("拍照"))
    check("「拍照」→ quick，不指定角度（用默认 -30°）",
          scans[-1] == ("quick", None), "实得 %s" % (scans[-1],))

    d.handle(parse("俯角45度"))
    d.handle(parse("拍照"))
    check("先「俯角45度」再「拍照」→ 沿用 -45°",
          scans[-1] == ("quick", -45.0), "实得 %s" % (scans[-1],))

    d.handle(parse("回中"))
    d.handle(parse("拍照"))
    check("「回中」后恢复默认俯角（None）",
          scans[-1] == ("quick", None), "实得 %s" % (scans[-1],))

    d.handle(parse("完整扫描"))
    check("「完整扫描」→ full，不受俯角记忆影响",
          scans[-1] == ("full", None), "实得 %s" % (scans[-1],))

    d.handle(parse("拍一张"))
    check("「拍一张」只走单张回调，不启动扫描",
          len(ones) == 1 and len(scans) == 4,
          "单张 %d 次 / 扫描 %d 次" % (len(ones), len(scans)))


def run_all():
    _ok[0] = _fail[0] = 0
    import tempfile
    tmp = tempfile.mkdtemp(prefix="gimbal_selftest_")
    print("== 云台离线自检 ==")
    test_numbers()
    test_intents()
    test_mapping()
    test_slew()
    test_closed_loop(tmp)
    test_coverage()
    test_default_plan(tmp)
    test_vertical()
    test_voice_scan_tilt(tmp)
    print("----")
    print("通过 %d 项，失败 %d 项" % (_ok[0], _fail[0]))
    return _fail[0] == 0
