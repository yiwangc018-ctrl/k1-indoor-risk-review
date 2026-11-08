"""语音→意图→动作。

分工要清楚：
    ASR（麦克风转文字）在 mic.py，这里只做**文字到意图**，是纯函数，可离线测。
    这样即使 ASR 还没装好，也能用键盘先把控制链路和演示脚本跑通。

支持的指令（口语容错，中文数字与阿拉伯数字都认）：
    转到九十度 / 到180度 / 方位270        → pan 绝对方位
    左 / 向左 / 左一点 / 左15度            → pan 相对
    右 / 右一点                           → pan 相对
    俯角30度 / 低头45度 / 向下30度         → tilt 绝对方位（负）
    仰角20度 / 抬头15度 / 向上20度         → tilt 绝对方位（正）
    再低一点 / 再高一点                    → tilt 相对
    稳定在俯仰-30度 / 保持俯仰-30度        → tilt 闭环（用 MPU6050）
    回中 / 复位 / 回正                     → pan=home, tilt=0
    下一个 / 换一个方位                    → 依次跳到下一个预设方位
    拍照 / 拍四张 / 扫描 / 开始扫描         → 默认拍摄：4 方位 × 俯角 30°，共 4 张
    完整扫描 / 三环 / 全景                  → 3 环 × 4 方位，共 12 张
    拍一张 / 补拍                          → 只在当前位姿拍一张（不移动）
    检查水平 / 调平                        → 水平门禁
    状态 / 现在在哪                        → 播报当前姿态

注意"默认拍摄"这条：俯角 30° 写在 config.json 的 scan.modes.quick 里，
说"拍照"就走它，**不需要先报角度**。要换角度才说"俯角45度"再"拍照"。
"""

import inspect
import re

DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
          "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

NUM = r"([零〇一二两三四五六七八九十百\d]+)"
SIGN_NUM = r"([-负]?[零〇一二两三四五六七八九十百\d]+)"


def cn2num(s):
    """中文数字 → 数值。支持 零/一/十/百，例如 一百八十 → 180。"""
    if s is None:
        return None
    s = s.strip().replace("度", "").replace("°", "")
    if s == "":
        return None
    if re.fullmatch(r"[-+]?\d+(\.\d+)?", s):
        return float(s) if "." in s else int(s)

    neg = False
    if s[0] in "负-":
        neg = True
        s = s[1:]

    total = 0
    if "百" in s:
        head, _, s = s.partition("百")
        h = DIGITS.get(head, 1) if head else 1
        if head and head not in DIGITS:
            return None
        total += h * 100
    if "十" in s:
        head, _, s = s.partition("十")
        h = DIGITS.get(head, 1) if head else 1
        if head and head not in DIGITS:
            return None
        total += h * 10
    if s:
        if s in DIGITS:
            total += DIGITS[s]
        else:
            return None
    val = -total if neg else total
    return val


class Intent:
    __slots__ = ("kind", "value", "raw", "text")

    def __init__(self, kind, value=None, raw="", text=""):
        self.kind = kind
        self.value = value
        self.raw = raw
        self.text = text

    def __repr__(self):
        return "Intent(%s, %r)" % (self.kind, self.value)


# 顺序有意义：先匹配复合/带限定词的说法，再匹配裸数字
_RULES = [
    ("HOME",    r"(回中|回正|复位|归位|回到中间|回原点|居中)"),
    ("STOP",    r"^(停|停止|别动|暂停|不要动)"),
    ("STATUS",  r"(状态|报告|现在在哪|当前位置|到哪了)"),
    ("LEVEL",   r"(检查水平|水平检查|调平|水平|水平仪)"),
    # 完整扫描要先匹配，否则"完整扫描"会被下面的"扫描"抢走
    ("SCAN_FULL", r"(完整扫描|全面扫描|全向扫描|全景扫描|全景|三环|深扫|扫一圈|转一圈)"),
    ("CAPTURE_ONE", r"(拍一张|单张|补拍|只拍一张|再来一张)"),
    # 默认拍摄：四张、俯角 30°，不用报角度
    ("SHOOT",   r"(拍照|拍四张|拍4张|拍一组|拍几张|拍照片|开始拍摄|开始扫描|扫描|采集|取图)"),
    ("NEXT",    r"(下一个|下一张|换一个|下一方位|下一个位置)"),
    ("TILT_PITCH", r"(?:稳定|保持|锁定|停在)[^0-9零〇一二两三四五六七八九十百]*?"
                   r"(?:俯仰|角度|仰角|俯角)?\s*" + SIGN_NUM + r"\s*度?"),
    ("TILT_DOWN", r"(?:俯角|低头|向下|下俯|往下)\s*" + NUM + r"\s*度?"),
    ("TILT_UP",   r"(?:仰角|抬头|向上|上仰|往上)\s*" + NUM + r"\s*度?"),
    ("TILT_REL",  r"(再低一点|低一点|往下一点|再高一点|高一点|往上一点)"),
    ("PAN_LEFT",  r"(?:向左|往左|左转|左边|左)\s*" + NUM + r"?\s*度?"),
    ("PAN_RIGHT", r"(?:向右|往右|右转|右边|右)\s*" + NUM + r"?\s*度?"),
    ("PAN_ABS",   r"(?:转到|到|方位|位置|朝向|朝|位于)\s*" + NUM + r"\s*度"),
    ("BARE_DEG",  r"^\s*" + NUM + r"\s*度\s*$"),
]


def parse(text, default_rel=15.0):
    """文字 → Intent。纯函数，不碰硬件。"""
    if text is None:
        return Intent("UNKNOWN", raw="")
    t = re.sub(r"[\s，。、！？!?,.]", "", text)
    if not t:
        return Intent("UNKNOWN", raw=text)

    for kind, pat in _RULES:
        m = re.search(pat, t)
        if not m:
            continue

        if kind == "TILT_DOWN":
            v = cn2num(m.group(1))
            return Intent("TILT_ABS", -abs(float(v)) if v is not None else -30.0, t)
        if kind == "TILT_UP":
            v = cn2num(m.group(1))
            return Intent("TILT_ABS", abs(float(v)) if v is not None else 20.0, t)
        if kind == "TILT_PITCH":
            v = cn2num(m.group(1))
            return Intent("TILT_PITCH", float(v) if v is not None else 0.0, t)
        if kind == "TILT_REL":
            low = any(k in m.group(0) for k in ("低", "下"))
            return Intent("TILT_REL", -5.0 if low else 5.0, t)
        if kind == "PAN_LEFT":
            v = cn2num(m.group(1)) if m.groups() and m.group(1) else default_rel
            return Intent("PAN_REL", -abs(float(v)), t)
        if kind == "PAN_RIGHT":
            v = cn2num(m.group(1)) if m.groups() and m.group(1) else default_rel
            return Intent("PAN_REL", abs(float(v)), t)
        if kind in ("PAN_ABS", "BARE_DEG"):
            v = cn2num(m.group(1))
            if v is None:
                continue
            return Intent("PAN_ABS", float(v), t)
        return Intent(kind, None, t)

    # 兜底：句子里出现"度"且只有一个数字，按方位角理解（底座为主）
    m = re.search(r"([零〇一二两三四五六七八九十百\d]+)\s*度", t)
    if m:
        v = cn2num(m.group(1))
        if v is not None:
            return Intent("PAN_ABS", float(v), t)
    return Intent("UNKNOWN", raw=t, text=t)


def _arity(fn):
    try:
        return len([p for p in inspect.signature(fn).parameters.values()
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])
    except (TypeError, ValueError):
        return 0


def _call_scan(fn, mode, tilt=None):
    """按回调声明的形参个数传参。

    兼容三种写法：on_scan()、on_scan(mode)、on_scan(mode, tilt)。
    """
    n = _arity(fn)
    if n >= 2:
        return fn(mode, tilt)
    if n >= 1:
        return fn(mode)
    return fn()


class Dispatcher:
    """把意图落到云台上，并生成一句口语回复。"""

    def __init__(self, gimbal, log=print, on_capture=None, on_scan=None, say=None):
        self.g = gimbal
        self.log = log
        self.on_capture = on_capture
        self.on_scan = on_scan
        self.say = say or (lambda s: None)
        # 最近一次用户说过的俯仰角：说了就沿用，没说就用 config 的默认（-30°）。
        # "回中"会把这条记忆清掉，恢复默认。
        self.scan_tilt = None

    def handle(self, intent):
        g = self.g
        k = intent.kind

        if k == "PAN_ABS":
            presets = self.g.cfg.get("presets", {}).get("pan", [0, 90, 180, 270])
            deg = intent.value
            near = min(presets, key=lambda p: abs(p - deg))
            if abs(near - deg) > 3.0:
                self.log("[Voice] %s 不在预设方位 %s 上，按精确角度执行" % (deg, presets))
            ok = g.goto(pan=float(deg))
            # 报"实际指令角"而不是"你说的角度"：软限位夹紧后两者可能差几度
            return self._r(ok, "已转到方位 %.0f 度" % g.pan.target, "转到方向时超时")

        if k == "PAN_REL":
            ok = g.nudge(pan_d=intent.value)
            return self._r(ok, "已%s %.0f 度" % ("左转" if intent.value < 0 else "右转",
                                                abs(intent.value)), "转动超时")

        if k == "NEXT":
            ok = g.next_preset()
            return self._r(ok, "已换到方位 %.0f 度" % g.pan.target, "转动超时")

        if k == "TILT_ABS":
            ok = g.goto(tilt=float(intent.value))
            if ok:
                self.scan_tilt = g.tilt.target
            return self._r(ok, "俯仰已设为 %.0f 度，拍照沿用" % g.tilt.target,
                           "俯仰动作超时")

        if k == "TILT_REL":
            ok = g.nudge(tilt_d=intent.value)
            if ok:
                self.scan_tilt = g.tilt.target
            return self._r(ok, "俯仰已调整 %.0f 度，拍照沿用" % intent.value,
                           "俯仰动作超时")

        if k == "TILT_PITCH":
            try:
                r = g.tilt_to_pitch(float(intent.value), verbose=True)
            except Exception as e:
                return "俯仰闭环失败：%s" % e
            if r["ok"]:
                self.scan_tilt = float(intent.value)
                return "相机俯仰已稳定在 %.1f 度，拍照沿用" % r["achieved"]
            return "俯仰未收敛，当前 %.1f 度，目标 %.1f 度" % (r["achieved"], r["target"])

        if k == "HOME":
            ok = g.home()
            if ok:
                self.scan_tilt = None      # 回中 = 忘掉刚才的角度，恢复默认俯角
            return self._r(ok, "已回到基准位，拍照恢复默认俯角", "回位超时")

        if k == "STOP":
            g.stop(relax=False)
            return "已停止"

        if k == "STATUS":
            st = g.state()
            s = "方位 %.0f 度，俯仰 %.0f 度" % (st.get("pan_target", 0), st.get("tilt_target", 0))
            if st.get("camera_pitch") is not None:
                s += "，实测俯仰 %.1f 度、左右倾 %.1f 度" % (st["camera_pitch"], st["camera_roll"])
            return s

        if k == "LEVEL":
            lg = g.level_gate()
            if lg["ok"]:
                return "水平正常，左右倾 %.2f 度" % (lg["roll"] or 0.0)
            return "不满足水平要求：%s" % lg["reason"]

        if k == "SHOOT":
            return self._scan("quick")

        if k == "SCAN_FULL":
            return self._scan("full")

        if k == "CAPTURE_ONE":
            ok, reasons = g.capture_gate()
            if not ok:
                return "还不能拍：%s" % "、".join(reasons)
            if self.on_capture is None:
                return "采集未接入"
            return self.on_capture()

        return "没听懂：%s" % (intent.raw or "")

    def _scan(self, mode):
        if self.on_scan is None:
            return "扫描流程未接入"
        tilt = self.scan_tilt if mode == "quick" else None
        try:
            msg = _call_scan(self.on_scan, mode, tilt)
        except Exception as e:
            return "扫描失败：%s" % e
        if msg:
            return msg
        return "已按快速四张拍完" if mode == "quick" else "已按三环全向拍完"

    def _r(self, ok, good, bad):
        return good if ok else bad


def run_text_loop(dispatcher, stream, say, log=print, on_text=None):
    """从任意行输入源跑指令循环（键盘/管道/ASR 桥接都用它）。"""
    log("语音/文字指令已就绪，输入「状态」查看当前姿态，「退出」结束。")
    for line in stream:
        text = (line or "").strip()
        if not text:
            continue
        if text in ("退出", "quit", "exit", "q"):
            break
        it = parse(text)
        reply = dispatcher.handle(it)
        log("你：%s" % text)
        log("云台：%s" % reply)
        say(reply)
        if on_text:
            on_text(text, it, reply)
