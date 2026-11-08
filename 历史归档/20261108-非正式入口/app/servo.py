#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""舵机抽象层（阶段 1：PC 可验证，不碰 GPIO）。

角度约定
--------
* pan  ：正值向右，负值向左，默认软限位 -75°..+75°
* tilt ：正值向上，负值向下，默认软限位 -30°..+60°
* 0,0  ：回中（正前方）

设计要点
--------
1. MockServo 是唯一可执行的实现：所有动作只改内存状态并写事件，PC 上可完整验收。
2. GpioServo 只是阶段 2 的占位：构造函数可建，但任何动作都抛 NotImplementedError，
   避免“看起来已经接了硬件”。
3. 软限位在 move_to 内做 clamp，而不是让上层猜；被裁剪时 clamped=True 并记录事件。
4. stop() 是急停闩锁：停止后 move_to 会抛 ServoError，必须显式 center() 或
   clear_stop() 才能恢复。center() 被视为安全恢复动作，会清除闩锁。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


class ServoError(RuntimeError):
    """舵机层拒绝执行（急停、参数不合法等）。"""


@dataclass(frozen=True)
class ServoEvent:
    """一条不可变的舵机事件，便于测试/日志/复盘。"""

    seq: int
    at: float
    kind: str
    pan: float
    tilt: float
    requested_pan: Optional[float] = None
    requested_tilt: Optional[float] = None
    clamped: bool = False
    reason: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "at": self.at,
            "kind": self.kind,
            "pan": self.pan,
            "tilt": self.tilt,
            "requested_pan": self.requested_pan,
            "requested_tilt": self.requested_tilt,
            "clamped": self.clamped,
            "reason": self.reason,
            "extra": dict(self.extra),
        }


@dataclass(frozen=True)
class MoveResult:
    """一次移动/回中的结果。"""

    requested_pan: Optional[float]
    requested_tilt: Optional[float]
    pan: float
    tilt: float
    clamped: bool
    event: ServoEvent


class BaseServo:
    """所有舵机实现的公共接口。"""

    name = "base"

    def move_to(self, pan: Optional[float] = None,
                tilt: Optional[float] = None, reason: str = "") -> MoveResult:
        raise NotImplementedError

    def stop(self, reason: str = "") -> ServoEvent:
        raise NotImplementedError

    def center(self, reason: str = "") -> MoveResult:
        raise NotImplementedError

    def clear_stop(self, reason: str = "") -> None:
        raise NotImplementedError

    def state(self) -> dict:
        raise NotImplementedError

    @property
    def events(self) -> list:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


class MockServo(BaseServo):
    """纯内存舵机：记录角度、限位裁剪与事件，不依赖任何硬件。"""

    name = "mock"

    def __init__(self, pan: float = 0.0, tilt: float = 0.0,
                 pan_min: float = -75.0, pan_max: float = 75.0,
                 tilt_min: float = -30.0, tilt_max: float = 60.0,
                 max_events: int = 1000):
        if pan_min >= pan_max:
            raise ValueError("pan_min 必须小于 pan_max")
        if tilt_min >= tilt_max:
            raise ValueError("tilt_min 必须小于 tilt_max")
        if not (pan_min <= 0 <= pan_max):
            raise ValueError("回中点 0 必须在 pan 软限位内")
        if not (tilt_min <= 0 <= tilt_max):
            raise ValueError("回中点 0 必须在 tilt 软限位内")
        self.pan_min = float(pan_min)
        self.pan_max = float(pan_max)
        self.tilt_min = float(tilt_min)
        self.tilt_max = float(tilt_max)
        self._pan = self._clamp(float(pan), self.pan_min, self.pan_max)
        self._tilt = self._clamp(float(tilt), self.tilt_min, self.tilt_max)
        self._stopped = False
        self._seq = 0
        self._events = []
        self.max_events = int(max_events)

    # --- 状态 ---------------------------------------------------------------
    @property
    def pan(self) -> float:
        return self._pan

    @property
    def tilt(self) -> float:
        return self._tilt

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def events(self) -> list:
        return list(self._events)

    def state(self) -> dict:
        return {
            "name": self.name,
            "pan": self._pan,
            "tilt": self._tilt,
            "stopped": self._stopped,
            "pan_min": self.pan_min,
            "pan_max": self.pan_max,
            "tilt_min": self.tilt_min,
            "tilt_max": self.tilt_max,
        }

    # --- 动作 ---------------------------------------------------------------
    def move_to(self, pan: Optional[float] = None,
                tilt: Optional[float] = None, reason: str = "") -> MoveResult:
        if self._stopped:
            raise ServoError("急停闩锁生效；先 center() 或 clear_stop() 再移动")
        if pan is None and tilt is None:
            raise ServoError("未指定 pan/tilt，拒绝空动作")

        requested_pan = None if pan is None else float(pan)
        requested_tilt = None if tilt is None else float(tilt)
        new_pan = self._pan if requested_pan is None else self._clamp(
            requested_pan, self.pan_min, self.pan_max)
        new_tilt = self._tilt if requested_tilt is None else self._clamp(
            requested_tilt, self.tilt_min, self.tilt_max)
        clamped = ((requested_pan is not None and new_pan != requested_pan)
                   or (requested_tilt is not None and new_tilt != requested_tilt))

        self._pan, self._tilt = new_pan, new_tilt
        event = self._record(
            "clamp" if clamped else "move",
            requested_pan=requested_pan,
            requested_tilt=requested_tilt,
            clamped=clamped,
            reason=reason,
        )
        return MoveResult(
            requested_pan=requested_pan,
            requested_tilt=requested_tilt,
            pan=new_pan,
            tilt=new_tilt,
            clamped=clamped,
            event=event,
        )

    def stop(self, reason: str = "") -> ServoEvent:
        self._stopped = True
        return self._record("stop", reason=reason)

    def center(self, reason: str = "") -> MoveResult:
        recovered = self._stopped
        self._stopped = False
        self._pan = self._clamp(0.0, self.pan_min, self.pan_max)
        self._tilt = self._clamp(0.0, self.tilt_min, self.tilt_max)
        event = self._record(
            "center",
            requested_pan=0.0,
            requested_tilt=0.0,
            clamped=False,
            reason=reason,
            extra={"recovered_from_stop": recovered},
        )
        return MoveResult(
            requested_pan=0.0,
            requested_tilt=0.0,
            pan=self._pan,
            tilt=self._tilt,
            clamped=False,
            event=event,
        )

    def clear_stop(self, reason: str = "") -> None:
        self._stopped = False
        self._record("clear_stop", reason=reason)

    # --- 内部 ---------------------------------------------------------------
    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, float(value)))

    def _record(self, kind: str, requested_pan: Optional[float] = None,
                requested_tilt: Optional[float] = None, clamped: bool = False,
                reason: str = "", extra: Optional[dict] = None) -> ServoEvent:
        self._seq += 1
        event = ServoEvent(
            seq=self._seq,
            at=time.time(),
            kind=kind,
            pan=self._pan,
            tilt=self._tilt,
            requested_pan=requested_pan,
            requested_tilt=requested_tilt,
            clamped=clamped,
            reason=reason,
            extra=dict(extra or {}),
        )
        self._events.append(event)
        if len(self._events) > self.max_events:
            del self._events[:len(self._events) - self.max_events]
        return event

    def __repr__(self) -> str:
        return "MockServo(pan=%.1f, tilt=%.1f, stopped=%s)" % (
            self._pan, self._tilt, self._stopped)


class GpioServo(BaseServo):
    """阶段 2 的真实 GPIO 实现占位：不 import gpiozero，也不碰引脚。

    真实实现应在阶段 2 用 gpiozero.Servo / lgpio 完成：
      - 构造时设置 GPIO 引脚、脉宽范围、50 Hz；
      - move_to 把角度映射到 -1..1，带缓动与软限位；
      - stop 立即 detach，center 回 0,0；
      - 断线/堵转要向上抛 ServoError。
    """

    name = "gpio-placeholder"

    def __init__(self, pan_pin: int, tilt_pin: Optional[int] = None,
                 pan_min: float = -75.0, pan_max: float = 75.0,
                 tilt_min: float = -30.0, tilt_max: float = 60.0):
        if pan_min >= pan_max or tilt_min >= tilt_max:
            raise ValueError("软限位配置不合法")
        self.pan_pin = int(pan_pin)
        self.tilt_pin = None if tilt_pin is None else int(tilt_pin)
        self.pan_min, self.pan_max = float(pan_min), float(pan_max)
        self.tilt_min, self.tilt_max = float(tilt_min), float(tilt_max)

    def _not_ready(self):
        raise NotImplementedError(
            "GpioServo 是阶段 2 占位；本阶段只允许 MockServo，未接触真实 GPIO")

    def move_to(self, pan=None, tilt=None, reason=""):
        self._not_ready()

    def stop(self, reason=""):
        self._not_ready()

    def center(self, reason=""):
        self._not_ready()

    def clear_stop(self, reason=""):
        self._not_ready()

    def state(self) -> dict:
        return {
            "name": self.name,
            "pan": None,
            "tilt": None,
            "stopped": None,
            "pan_pin": self.pan_pin,
            "tilt_pin": self.tilt_pin,
            "pan_min": self.pan_min,
            "pan_max": self.pan_max,
            "tilt_min": self.tilt_min,
            "tilt_max": self.tilt_max,
        }

    @property
    def events(self) -> list:
        return []


def create_servo(kind: str = "mock", **kwargs) -> BaseServo:
    """工厂：阶段 1 只应使用 mock；gpio 仅用于阶段 2 接线。"""
    kind = (kind or "mock").strip().lower()
    if kind == "mock":
        return MockServo(**kwargs)
    if kind in ("gpio", "real", "hardware"):
        return GpioServo(**kwargs)
    raise ValueError("未知 servo kind=%r（mock/gpio）" % kind)


__all__ = [
    "ServoError", "ServoEvent", "MoveResult", "BaseServo", "MockServo",
    "GpioServo", "create_servo",
]
