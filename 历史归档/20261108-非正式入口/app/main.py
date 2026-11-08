#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 1 主链路：文本指令 -> 确定性解析 -> 舵机动作 -> 反馈 -> 指标。

    python app/main.py --mock --once "向左30度"
    python app/main.py --mock --once "停"
    python app/main.py --mock --once "打开灯"      # 未知命令：ERROR，不动机
    python app/main.py --mock                      # 交互模式

状态机
------
IDLE -> THINK -> ACT -> FEEDBACK
                 └──> ERROR（未知命令 / 舵机拒绝）

规则命令在 THINK 阶段只做字符串解析，不调用 LLM、不访问网络；解析完立即进入
ACT，因此“首响应”只包含解析和一次内存/驱动调用。阶段 2 接真实 GPIO 时，
只需要把 MockServo 换成 GpioServo，main 的流程不变。

本文件不 import display/llm，也不接 ASR/CV；阶段 1 只证明指令 -> 执行器这一段。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import intent as intent_mod  # noqa: E402
import metrics as metrics_mod  # noqa: E402
import servo as servo_mod  # noqa: E402

EXIT_OK = 0
EXIT_BAD_COMMAND = 2
EXIT_SERVO = 3
EXIT_INTERNAL = 4


class State(str, Enum):
    IDLE = "IDLE"
    THINK = "THINK"
    ACT = "ACT"
    FEEDBACK = "FEEDBACK"
    ERROR = "ERROR"


@dataclass
class CommandResult:
    text: str
    state: State
    exit_code: int
    ok: bool
    intent: Optional[intent_mod.Intent]
    reply: str
    error: Optional[str]
    states: List[State]
    total_ms: float
    first_response_ms: Optional[float]
    servo_state: dict

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "state": self.state.value,
            "exit_code": self.exit_code,
            "ok": self.ok,
            "intent": self.intent.to_dict() if self.intent else None,
            "reply": self.reply,
            "error": self.error,
            "states": [s.value for s in self.states],
            "total_ms": round(self.total_ms, 3),
            "first_response_ms": (None if self.first_response_ms is None
                                  else round(self.first_response_ms, 3)),
            "servo_state": dict(self.servo_state),
        }


class Feedback:
    """反馈接口：OLED/TTS/日志都可以实现它。"""

    def show(self, text: str) -> None:
        pass

    def say(self, text: str) -> None:
        pass


class NullFeedback(Feedback):
    pass


class ConsoleFeedback(Feedback):
    """阶段 1 的终端反馈；不假装有真实 TTS。"""

    def show(self, text: str) -> None:
        print("[fb] %s" % text)

    def say(self, text: str) -> None:
        print("[tts] %s" % text)


class DisplayFeedback(Feedback):
    """把 show() 转给现有 display.Display；say() 仍只打印。"""

    def __init__(self, display):
        self.display = display

    def show(self, text: str) -> None:
        self.display.show(text)

    def say(self, text: str) -> None:
        print("[tts] %s" % text)


def _safe_show(feedback: Feedback, text: str) -> None:
    try:
        feedback.show(text)
    except Exception:
        pass


def _safe_say(feedback: Feedback, text: str) -> None:
    try:
        feedback.say(text)
    except Exception:
        pass


def _servo_state(servo) -> dict:
    try:
        return dict(servo.state())
    except Exception:
        return {}


def _move_label(intent: intent_mod.Intent, applied: float) -> str:
    prefix = "PAN" if intent.axis == intent_mod.PAN else "TILT"
    return "%s%+.0f" % (prefix, applied)


def _move_reply(intent: intent_mod.Intent, applied: float,
                clamped: bool) -> str:
    axis = "水平" if intent.axis == intent_mod.PAN else "俯仰"
    text = "已转到%s %.1f 度" % (axis, applied)
    if clamped:
        text += "（已软限位）"
    return text


def _finish(text: str, state: State, exit_code: int, ok: bool,
            intent: Optional[intent_mod.Intent], reply: str,
            error: Optional[str], states: List[State], t0: float,
            first_ms: Optional[float], servo, metrics: metrics_mod.Metrics,
            extra: Optional[dict] = None) -> CommandResult:
    total_ms = (time.perf_counter() - t0) * 1000.0
    metrics.latency("total", total_ms, state=state.value, command=text,
                    ok=ok, error=error, detail=dict(extra or {}))
    return CommandResult(
        text=text,
        state=state,
        exit_code=exit_code,
        ok=ok,
        intent=intent,
        reply=reply,
        error=error,
        states=list(states),
        total_ms=total_ms,
        first_response_ms=first_ms,
        servo_state=_servo_state(servo),
    )


def run_command(text: str, servo, feedback: Optional[Feedback] = None,
                metrics: Optional[metrics_mod.Metrics] = None) -> CommandResult:
    """执行一条指令并返回结构化结果；不抛异常（除非 Metrics 已关闭）。

    这是阶段 1 的核心入口，可被 CLI、测试或未来的 ASR 循环直接调用。
    """
    t0 = time.perf_counter()
    text = "" if text is None else str(text).strip()
    feedback = feedback or NullFeedback()
    metrics = metrics or metrics_mod.Metrics()
    states = [State.IDLE]

    def enter(state: State) -> None:
        states.append(state)

    enter(State.THINK)
    metrics.event("command", state=State.THINK.value, command=text)

    parse_started = time.perf_counter()
    parsed = intent_mod.parse_command(text)
    parse_ms = (time.perf_counter() - parse_started) * 1000.0
    metrics.latency("parse", parse_ms, state=State.THINK.value, command=text,
                    ok=parsed is not None,
                    detail={"intent": parsed.to_dict() if parsed else None})

    if parsed is None:
        enter(State.ERROR)
        _safe_show(feedback, "ERR")
        _safe_say(feedback, "没听懂，未执行")
        metrics.event("reject", state=State.ERROR.value, command=text,
                      ok=False, error="unknown-command")
        return _finish(text, State.ERROR, EXIT_BAD_COMMAND, False, None,
                       "没听懂，未执行", "unknown-command", states, t0,
                       None, servo, metrics)

    enter(State.ACT)
    first_ms = (time.perf_counter() - t0) * 1000.0
    metrics.latency("first_response", first_ms, state=State.ACT.value,
                    command=text, ok=True,
                    detail={"action": parsed.action, "axis": parsed.axis})

    act_started = time.perf_counter()
    try:
        if parsed.action == intent_mod.STOP:
            servo.stop(reason=text)
            applied = None
            clamped = False
            label = "STOP"
            reply = "已停止"
        elif parsed.action == intent_mod.CENTER:
            moved = servo.center(reason=text)
            applied = 0.0
            clamped = moved.clamped
            label = "CENT"
            reply = "已回中" + ("（已软限位）" if clamped else "")
        else:
            pan = parsed.value if parsed.axis == intent_mod.PAN else None
            tilt = parsed.value if parsed.axis == intent_mod.TILT else None
            moved = servo.move_to(pan=pan, tilt=tilt, reason=text)
            applied = moved.pan if parsed.axis == intent_mod.PAN else moved.tilt
            clamped = moved.clamped
            label = _move_label(parsed, applied)
            reply = _move_reply(parsed, applied, clamped)

        act_ms = (time.perf_counter() - act_started) * 1000.0
        state_now = _servo_state(servo)
        metrics.latency("act", act_ms, state=State.ACT.value, command=text,
                        ok=True, detail={"action": parsed.action})
        metrics.event(
            "action", state=State.ACT.value, command=text, action=parsed.action,
            axis=parsed.axis, mode=parsed.mode, value=parsed.value,
            applied=applied, clamped=clamped, ok=True,
            detail={"pan": state_now.get("pan"), "tilt": state_now.get("tilt")},
        )

        enter(State.FEEDBACK)
        _safe_show(feedback, label)
        _safe_say(feedback, reply)
        metrics.event("result", state=State.FEEDBACK.value, command=text,
                      action=parsed.action, ok=True, detail={"reply": reply})
        return _finish(text, State.FEEDBACK, EXIT_OK, True, parsed, reply,
                       None, states, t0, first_ms, servo, metrics)

    except servo_mod.ServoError as exc:
        act_ms = (time.perf_counter() - act_started) * 1000.0
        metrics.latency("act", act_ms, state=State.ACT.value, command=text,
                        ok=False, error=str(exc))
        metrics.event("error", state=State.ACT.value, command=text,
                      action=parsed.action, ok=False, error=str(exc))
        enter(State.ERROR)
        _safe_show(feedback, "ERR")
        _safe_say(feedback, "舵机拒绝执行")
        return _finish(text, State.ERROR, EXIT_SERVO, False, parsed,
                       "舵机拒绝执行", str(exc), states, t0, first_ms,
                       servo, metrics)
    except Exception as exc:  # 驱动层未知错误：不让主循环崩，记录后转 ERROR
        act_ms = (time.perf_counter() - act_started) * 1000.0
        error = "%s: %s" % (type(exc).__name__, exc)
        metrics.latency("act", act_ms, state=State.ACT.value, command=text,
                        ok=False, error=error)
        metrics.event("error", state=State.ACT.value, command=text,
                      action=parsed.action, ok=False, error=error)
        enter(State.ERROR)
        _safe_show(feedback, "ERR")
        _safe_say(feedback, "执行失败")
        return _finish(text, State.ERROR, EXIT_INTERNAL, False, parsed,
                       "执行失败", error, states, t0, first_ms, servo, metrics)


def _print_result(result: CommandResult) -> None:
    first = "-" if result.first_response_ms is None else "%.1fms" % result.first_response_ms
    print("[main] state=%s exit=%d ok=%s total=%.1fms first=%s"
          % (result.state.value, result.exit_code, result.ok,
             result.total_ms, first))
    print("[main] intent=%s" % (result.intent.describe() if result.intent else "None"))
    state = result.servo_state
    print("[main] servo=pan=%s tilt=%s stopped=%s"
          % (state.get("pan"), state.get("tilt"), state.get("stopped")))
    if result.error:
        print("[main] error=%s" % result.error)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="阶段 1：文本指令 -> 舵机（mock）-> 反馈 -> 指标")
    ap.add_argument("text", nargs="*", help="要执行的指令；省略则进入交互模式")
    ap.add_argument("--mock", action="store_true",
                    help="使用 MockServo（阶段 1 必填；真实 GPIO 尚未接入）")
    ap.add_argument("--once", action="store_true", help="只执行一次后退出")
    ap.add_argument("--csv", default=None, help="事件/延迟 CSV 路径（可选）")
    ap.add_argument("--jsonl", default=None, help="事件/延迟 JSONL 路径（可选）")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    text = " ".join(args.text).strip()

    if not args.mock:
        print("[main] 阶段 1 只支持 --mock；真实 GPIO 尚未接入", file=sys.stderr)
        return EXIT_BAD_COMMAND
    if args.once and not text:
        print("[main] --once 需要一条指令", file=sys.stderr)
        return EXIT_BAD_COMMAND

    servo = servo_mod.MockServo()
    feedback = ConsoleFeedback()
    metrics = metrics_mod.Metrics(csv_path=args.csv, jsonl_path=args.jsonl)
    try:
        if args.once:
            result = run_command(text, servo, feedback, metrics)
            _print_result(result)
            return result.exit_code

        print("[main] 交互模式：输入指令，Ctrl+C 退出。例：向左30度 / 抬头 / 停 / 回中")
        while True:
            try:
                line = input(">>> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[main] 退出")
                return EXIT_OK
            if not line:
                continue
            result = run_command(line, servo, feedback, metrics)
            _print_result(result)
    finally:
        metrics.close()


if __name__ == "__main__":
    sys.exit(main())
