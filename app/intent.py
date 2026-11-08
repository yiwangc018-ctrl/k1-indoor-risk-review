#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确定性中文指令解析（阶段 1：不联网、不调用 LLM）。

支持的指令
----------
* 停止：停 / 停止 / 停下 / 急停 / 别动 / 暂停
* 回中：回中 / 回正 / 复位 / 居中 / 正前方 / 回到中间
* 方向：向左 / 向右 / 抬头 / 低头 / 往上 / 往下
* 角度：向左 30 度、左转四十五度、抬头 15 度
* 绝对：转到 45 度、转到四十五度、水平转到 -30 度、tilt=30

角度约定
--------
* pan 正=右、负=左；tilt 正=上、负=下。
* 相对指令的 value 带符号；绝对指令的 value 就是目标角。
* 无法确定/有歧义的句子返回 None，由调用方安全拒绝，绝不让舵机动。

这个模块只做字符串规则，没有任何网络/模型依赖；解析结果用 Intent 表达。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

PAN = "pan"
TILT = "tilt"

STOP = "stop"
CENTER = "center"
MOVE = "move"

# 软限位由 servo 负责；这里只拦截明显离谱的数值，避免把 10^9 写进日志。
MAX_ABS_ANGLE = 999.0

_FULLWIDTH = str.maketrans("０１２３４５６７８９．", "0123456789.")
_PUNCT = " \t\r\n。！？!?，,、；;：:"

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_CN_CLASS = "零〇一二两三四五六七八九十百千万"
_CN_RUN_RE = re.compile(r"[%s]+" % _CN_CLASS)

_STOP_WORDS = ("急停", "停止", "停下", "停下来", "停住", "暂停", "别动", "不要动", "停")
_NEG_STOP_RE = re.compile(r"(不要|别|不用|不必|不)停")
_NEG_CENTER_RE = re.compile(r"(不要|别|不用|不必|不)(回中|回正|复位|居中)")
_CENTER_WORDS = ("回中", "回正", "复位", "居中", "正前方", "回到中间", "转到中间",
                 "回到中位", "中位", "回原点")

_ABS_NUM_RE = re.compile(
    r"(?:转到|转至|旋转到|定位到|绝对角度|目标角度|回到)\s*"
    r"(-?\d+(?:\.\d+)?)\s*(?:度|°)?")
_ABS_CN_RE = re.compile(
    r"(?:转到|转至|旋转到|定位到|绝对角度|目标角度|回到)\s*"
    r"([%s]+)\s*(?:度|°)?$" % _CN_CLASS)
_AXIS_KV_RE = re.compile(r"(pan|tilt)\s*[=:：]?\s*(-?\d+(?:\.\d+)?)")
_AXIS_NUM_RE = re.compile(r"(水平|俯仰)(?:角度)?\s*(-?\d+(?:\.\d+)?)\s*(?:度|°)?")
_ANGLE_NUM_RE = re.compile(r"角度\s*(-?\d+(?:\.\d+)?)\s*(?:度|°)?")
_TURN_NUM_RE = re.compile(r"转\s*(-?\d+(?:\.\d+)?)\s*(?:度|°)?$")

_LEFT = ("向左", "左转", "往左", "左边", "左")
_RIGHT = ("向右", "右转", "往右", "右边", "右")
_UP = ("抬头", "向上", "往上", "上面", "抬高", "上仰", "仰头")
_DOWN = ("低头", "向下", "往下", "下面", "降低", "下俯", "俯身")


@dataclass(frozen=True)
class Intent:
    """一条结构化指令。

    action: stop / center / move
    axis:   pan / tilt（stop/center 为 None）
    mode:   relative / absolute（stop/center 为 None）
    value:  带符号角度（相对）或目标角（绝对）
    raw:    原始输入，便于日志复盘
    """

    action: str
    axis: Optional[str] = None
    mode: Optional[str] = None
    value: Optional[float] = None
    raw: str = ""

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "axis": self.axis,
            "mode": self.mode,
            "value": self.value,
            "raw": self.raw,
        }

    def describe(self) -> str:
        if self.action in (STOP, CENTER):
            return self.action
        return "%s %s %+.1f" % (self.axis, self.mode, float(self.value))

    def __str__(self) -> str:
        return self.describe()


def _normalize(text: str) -> str:
    t = (text or "").translate(_FULLWIDTH).strip()
    t = re.sub(r"\s+", "", t)
    return t.strip(_PUNCT)


def cn_to_int(text: str) -> Optional[int]:
    """中文数字 -> 整数。支持逐位（二零二六）和带单位（一百二十三、一万二千）。

    只做确定性字面解析，不做“一百二=120”这类口语补全。
    """
    if not text:
        return None
    if not any(ch in _CN_UNITS for ch in text):
        digits = [_CN_DIGITS[ch] for ch in text if ch in _CN_DIGITS]
        return int("".join(str(d) for d in digits)) if digits else None

    total, num = 0, 0
    for ch in text:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
            continue
        unit = _CN_UNITS.get(ch)
        if unit is None:
            continue
        if num == 0:
            num = 1
        if unit == 10000:
            total = (total + num) * 10000
        else:
            total += num * unit
        num = 0
    return total + num


def _number_in(text: str) -> Optional[float]:
    """从文本里取第一个角度数值（阿拉伯优先，其次中文）。"""
    m = re.search(r"(-?\d+(?:\.\d+)?)", text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    m = re.search(r"([%s]+)\s*(?:度|°)" % _CN_CLASS, text)
    if m:
        value = cn_to_int(m.group(1))
        return None if value is None else float(value)
    # “左转四十五”“抬头三十”这类省略“度”的口语
    m = re.search(r"(?:左|右|上|下|抬|低|往|转|到)(?:头|起|转)?([%s]+)$" % _CN_CLASS, text)
    if m:
        value = cn_to_int(m.group(1))
        return None if value is None else float(value)
    return None


def _default_step(text: str, axis: str) -> float:
    if "一点点" in text or "稍稍" in text:
        return 5.0
    if "一点" in text or "稍微" in text:
        return 10.0
    return 30.0 if axis == PAN else 15.0


def _axis_hint(text: str) -> str:
    if ("俯仰" in text or TILT in text
            or any(k in text for k in _UP) or any(k in text for k in _DOWN)):
        return TILT
    return PAN


def _direction_signs(text: str):
    left = any(k in text for k in _LEFT)
    right = any(k in text for k in _RIGHT)
    up = any(k in text for k in _UP)
    down = any(k in text for k in _DOWN)
    return left, right, up, down


def _parse_absolute(text: str, raw: str) -> Optional[Intent]:
    # pan=30 / tilt=-15
    m = _AXIS_KV_RE.search(text)
    if m:
        axis = PAN if m.group(1) == "pan" else TILT
        value = float(m.group(2))
        if abs(value) > MAX_ABS_ANGLE:
            return None
        return Intent(MOVE, axis, "absolute", value, raw)

    # 水平 45 / 俯仰 -30
    m = _AXIS_NUM_RE.search(text)
    if m:
        axis = PAN if m.group(1) == "水平" else TILT
        value = float(m.group(2))
        if abs(value) > MAX_ABS_ANGLE:
            return None
        return Intent(MOVE, axis, "absolute", value, raw)

    # 转到 45 度 / 转到四十五度 / 回到 0 度
    m = _ABS_NUM_RE.search(text)
    if m:
        value = float(m.group(1))
    else:
        m = _ABS_CN_RE.search(text)
        if not m:
            value = None
        else:
            parsed = cn_to_int(m.group(1))
            value = None if parsed is None else float(parsed)
    if value is not None:
        if abs(value) > MAX_ABS_ANGLE:
            return None
        # “向左转到45度”按左理解为 -45；“向下转到30度”按 -30。
        left, right, up, down = _direction_signs(text)
        if value > 0:
            if left and not right:
                value = -value
            elif down and not up:
                value = -value
        return Intent(MOVE, _axis_hint(text), "absolute", value, raw)

    # 角度 45
    m = _ANGLE_NUM_RE.search(text)
    if m:
        value = float(m.group(1))
        if abs(value) > MAX_ABS_ANGLE:
            return None
        return Intent(MOVE, PAN, "absolute", value, raw)

    # 转 45 度（没有“到”）
    m = _TURN_NUM_RE.fullmatch(text)
    if m:
        value = float(m.group(1))
        if abs(value) > MAX_ABS_ANGLE:
            return None
        return Intent(MOVE, PAN, "absolute", value, raw)
    return None


def _parse_relative(text: str, raw: str) -> Optional[Intent]:
    left, right, up, down = _direction_signs(text)
    pans = [s for s, on in ((-1.0, left), (1.0, right)) if on]
    tilts = [s for s, on in ((1.0, up), (-1.0, down)) if on]

    # 同时出现左右/上下/两个轴：有歧义，安全拒绝。
    if len(pans) > 1 or len(tilts) > 1 or (pans and tilts):
        return None
    if not pans and not tilts:
        return None

    axis = PAN if pans else TILT
    sign = pans[0] if pans else tilts[0]
    magnitude = _number_in(text)
    if magnitude is None:
        magnitude = _default_step(text, axis)
    magnitude = abs(float(magnitude))
    if magnitude > MAX_ABS_ANGLE:
        return None
    return Intent(MOVE, axis, "relative", sign * magnitude, raw)


def parse_command(text: str) -> Optional[Intent]:
    """解析一条中文指令；无法确定时返回 None（调用方必须安全拒绝）。"""
    raw = "" if text is None else str(text).strip()
    t = _normalize(raw)
    if not t:
        return None

    # 安全优先：停止 > 回中 > 运动；带否定的“不要停/不要回中”一律不动作。
    if _NEG_STOP_RE.search(t) or _NEG_CENTER_RE.search(t):
        return None
    if any(word in t for word in _STOP_WORDS):
        return Intent(STOP, raw=raw)
    if any(word in t for word in _CENTER_WORDS):
        return Intent(CENTER, raw=raw)

    absolute = _parse_absolute(t, raw)
    if absolute is not None:
        return absolute
    return _parse_relative(t, raw)


# 便于调用方使用更短的名字
parse = parse_command


__all__ = [
    "PAN", "TILT", "STOP", "CENTER", "MOVE", "Intent",
    "parse_command", "parse", "cn_to_int",
]
