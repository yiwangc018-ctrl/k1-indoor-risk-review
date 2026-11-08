#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 1 MVP 的 PC 自测。

    python tests/test_mvp.py

覆盖：中文指令解析、软限位、急停/回中、未知命令、CSV/JSONL 指标、端到端 mock、
CLI --mock/--once。不碰 GPIO、不连网络、不调用 LLM。
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import intent as intent_mod  # noqa: E402
import main as mvp_main  # noqa: E402
import metrics as metrics_mod  # noqa: E402
import servo as servo_mod  # noqa: E402


class RecordingFeedback(mvp_main.Feedback):
    def __init__(self):
        self.shown = []
        self.said = []

    def show(self, text):
        self.shown.append(text)

    def say(self, text):
        self.said.append(text)


class TestIntent(unittest.TestCase):
    def test_stop_variants(self):
        for text in ("停", "停止", "停下", "停下来", "急停", "别动", "暂停"):
            with self.subTest(text=text):
                it = intent_mod.parse_command(text)
                self.assertIsNotNone(it)
                self.assertEqual(it.action, intent_mod.STOP)
                self.assertIsNone(it.axis)

    def test_negated_stop_is_safe(self):
        for text in ("不要停", "别停", "不用停", "不必停", "不停"):
            with self.subTest(text=text):
                self.assertIsNone(intent_mod.parse_command(text))

    def test_negated_center_is_safe(self):
        for text in ("不要回中", "别复位", "不用居中"):
            with self.subTest(text=text):
                self.assertIsNone(intent_mod.parse_command(text))

    def test_center_variants(self):
        for text in ("回中", "回正", "复位", "居中", "正前方", "回到中间", "转到中间", "中位"):
            with self.subTest(text=text):
                it = intent_mod.parse_command(text)
                self.assertIsNotNone(it)
                self.assertEqual(it.action, intent_mod.CENTER)

    def test_direction_defaults(self):
        cases = [
            ("向左", intent_mod.PAN, -30.0),
            ("向左转", intent_mod.PAN, -30.0),
            ("向右", intent_mod.PAN, 30.0),
            ("往右转", intent_mod.PAN, 30.0),
            ("抬头", intent_mod.TILT, 15.0),
            ("低头", intent_mod.TILT, -15.0),
            ("往上", intent_mod.TILT, 15.0),
            ("向下", intent_mod.TILT, -15.0),
        ]
        for text, axis, value in cases:
            with self.subTest(text=text):
                it = intent_mod.parse_command(text)
                self.assertIsNotNone(it)
                self.assertEqual(it.action, intent_mod.MOVE)
                self.assertEqual(it.mode, "relative")
                self.assertEqual(it.axis, axis)
                self.assertAlmostEqual(it.value, value)

    def test_relative_angles(self):
        cases = [
            ("向左30度", intent_mod.PAN, -30.0),
            ("向右 45 度", intent_mod.PAN, 45.0),
            ("左转四十五度", intent_mod.PAN, -45.0),
            ("左转四十五", intent_mod.PAN, -45.0),
            ("抬头30度", intent_mod.TILT, 30.0),
            ("低头四十五度", intent_mod.TILT, -45.0),
            ("抬头三十", intent_mod.TILT, 30.0),
        ]
        for text, axis, value in cases:
            with self.subTest(text=text):
                it = intent_mod.parse_command(text)
                self.assertIsNotNone(it)
                self.assertEqual(it.mode, "relative")
                self.assertEqual(it.axis, axis)
                self.assertAlmostEqual(it.value, value)

    def test_small_step_words(self):
        self.assertAlmostEqual(intent_mod.parse_command("向左一点").value, -10.0)
        self.assertAlmostEqual(intent_mod.parse_command("向右一点点").value, 5.0)
        self.assertAlmostEqual(intent_mod.parse_command("往左稍微").value, -10.0)

    def test_absolute_angles(self):
        cases = [
            ("转到45度", intent_mod.PAN, 45.0),
            ("转到四十五度", intent_mod.PAN, 45.0),
            ("转到-30度", intent_mod.PAN, -30.0),
            ("水平转到30度", intent_mod.PAN, 30.0),
            ("俯仰转到-15度", intent_mod.TILT, -15.0),
            ("向上转到30度", intent_mod.TILT, 30.0),
            ("向下转到30度", intent_mod.TILT, -30.0),
            ("向左转到45度", intent_mod.PAN, -45.0),
            ("tilt=30", intent_mod.TILT, 30.0),
            ("pan=-15", intent_mod.PAN, -15.0),
            ("角度45", intent_mod.PAN, 45.0),
            ("转45度", intent_mod.PAN, 45.0),
        ]
        for text, axis, value in cases:
            with self.subTest(text=text):
                it = intent_mod.parse_command(text)
                self.assertIsNotNone(it)
                self.assertEqual(it.mode, "absolute")
                self.assertEqual(it.axis, axis)
                self.assertAlmostEqual(it.value, value)

    def test_unknown_is_rejected(self):
        for text in ("", None, "你好", "打开灯", "把灯打开", "向左并抬头"):
            with self.subTest(text=text):
                self.assertIsNone(intent_mod.parse_command(text))

    def test_stop_has_priority(self):
        self.assertEqual(intent_mod.parse_command("停止然后向左转").action,
                         intent_mod.STOP)
        self.assertEqual(intent_mod.parse_command("回中并停止").action,
                         intent_mod.STOP)

    def test_chinese_number_parser(self):
        self.assertEqual(intent_mod.cn_to_int("四十五"), 45)
        self.assertEqual(intent_mod.cn_to_int("一百二十三"), 123)
        self.assertEqual(intent_mod.cn_to_int("两千零二十六"), 2026)
        self.assertEqual(intent_mod.cn_to_int("一万二千"), 12000)
        self.assertEqual(intent_mod.cn_to_int("二零二六"), 2026)

    def test_intent_serialisation(self):
        it = intent_mod.parse_command("向左30度")
        self.assertEqual(it.to_dict()["value"], -30.0)
        self.assertIn("pan", it.describe())
        self.assertEqual(intent_mod.parse_command("停").describe(), "stop")


class TestServo(unittest.TestCase):
    def setUp(self):
        self.servo = servo_mod.MockServo()

    def test_initial_state(self):
        self.assertEqual(self.servo.pan, 0.0)
        self.assertEqual(self.servo.tilt, 0.0)
        self.assertFalse(self.servo.stopped)
        self.assertEqual(self.servo.state()["name"], "mock")

    def test_move_and_state(self):
        result = self.servo.move_to(pan=30, tilt=20)
        self.assertEqual(result.pan, 30.0)
        self.assertEqual(result.tilt, 20.0)
        self.assertFalse(result.clamped)
        self.assertEqual(self.servo.pan, 30.0)
        self.assertEqual(self.servo.tilt, 20.0)
        self.assertEqual(self.servo.events[-1].kind, "move")

    def test_soft_limits_clamp(self):
        result = self.servo.move_to(pan=120)
        self.assertEqual(result.pan, 75.0)
        self.assertTrue(result.clamped)
        self.assertEqual(self.servo.events[-1].kind, "clamp")

        result = self.servo.move_to(tilt=-60)
        self.assertEqual(result.tilt, -30.0)
        self.assertTrue(result.clamped)

        result = self.servo.move_to(pan=-120, tilt=90)
        self.assertEqual(result.pan, -75.0)
        self.assertEqual(result.tilt, 60.0)
        self.assertTrue(result.clamped)

    def test_stop_latches_until_center(self):
        self.servo.move_to(pan=25)
        self.servo.stop(reason="test")
        self.assertTrue(self.servo.stopped)
        with self.assertRaises(servo_mod.ServoError):
            self.servo.move_to(pan=10)
        self.assertEqual(self.servo.pan, 25.0)

        result = self.servo.center(reason="recover")
        self.assertFalse(self.servo.stopped)
        self.assertEqual((result.pan, result.tilt), (0.0, 0.0))
        self.assertTrue(result.event.extra["recovered_from_stop"])

    def test_clear_stop(self):
        self.servo.stop()
        self.servo.clear_stop()
        self.assertFalse(self.servo.stopped)
        self.servo.move_to(pan=10)
        self.assertEqual(self.servo.pan, 10.0)

    def test_events_are_recorded(self):
        self.servo.move_to(pan=10)
        self.servo.move_to(pan=120)
        self.servo.stop()
        self.servo.center()
        kinds = [e.kind for e in self.servo.events]
        self.assertEqual(kinds, ["move", "clamp", "stop", "center"])
        payload = self.servo.events[0].as_dict()
        self.assertEqual(payload["pan"], 10.0)
        self.assertIn("seq", payload)

    def test_bad_limits_rejected(self):
        with self.assertRaises(ValueError):
            servo_mod.MockServo(pan_min=10, pan_max=10)
        with self.assertRaises(ValueError):
            servo_mod.MockServo(tilt_min=-10, tilt_max=-1)

    def test_gpio_placeholder_does_not_pretend(self):
        placeholder = servo_mod.GpioServo(pan_pin=70, tilt_pin=73)
        self.assertEqual(placeholder.state()["name"], "gpio-placeholder")
        with self.assertRaises(NotImplementedError):
            placeholder.move_to(pan=10)
        with self.assertRaises(NotImplementedError):
            placeholder.stop()

    def test_create_servo(self):
        self.assertIsInstance(servo_mod.create_servo("mock"), servo_mod.MockServo)
        self.assertIsInstance(
            servo_mod.create_servo("gpio", pan_pin=70), servo_mod.GpioServo)
        with self.assertRaises(ValueError):
            servo_mod.create_servo("nope")


class TestMetrics(unittest.TestCase):
    def test_csv_and_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "events.csv")
            jsonl_path = os.path.join(tmp, "events.jsonl")
            metrics = metrics_mod.Metrics(csv_path=csv_path, jsonl_path=jsonl_path)
            metrics.event("command", command="向左30度", state="THINK",
                          detail={"intent": "move"})
            metrics.latency("parse", 1.25, state="THINK", command="向左30度")
            metrics.close()

            with open(csv_path, encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                self.assertEqual(tuple(reader.fieldnames), metrics_mod.FIELDS)
                rows = list(reader)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["event"], "command")
            self.assertEqual(rows[0]["command"], "向左30度")
            self.assertEqual(json.loads(rows[0]["detail"])["intent"], "move")
            self.assertEqual(rows[1]["kind"], "latency")
            self.assertAlmostEqual(float(rows[1]["ms"]), 1.25)

            with open(jsonl_path, encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0]["kind"], "event")
            self.assertEqual(lines[1]["stage"], "parse")

    def test_append_does_not_duplicate_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "append.csv")
            for _ in range(2):
                metrics = metrics_mod.Metrics(csv_path=path)
                metrics.event("tick")
                metrics.close()
            with open(path, encoding="utf-8") as fh:
                lines = [line for line in fh if line.strip()]
            self.assertEqual(sum(1 for line in lines if line.startswith("ts,")), 1)
            self.assertEqual(len(lines), 3)  # header + 2 rows

    def test_memory_only_and_closed(self):
        metrics = metrics_mod.Metrics()
        metrics.event("start")
        self.assertEqual(metrics.summary()["total"], 1)
        metrics.close()
        with self.assertRaises(RuntimeError):
            metrics.event("late")

    def test_io_failure_does_not_break_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            metrics = metrics_mod.Metrics(csv_path=tmp)  # 目录，不是文件
            metrics.event("x")
            self.assertEqual(len(metrics.records), 1)
            self.assertTrue(metrics.errors)
            metrics.close()

    def test_context_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ctx.jsonl")
            with metrics_mod.Metrics(jsonl_path=path) as metrics:
                metrics.event("x")
            self.assertTrue(metrics.closed)
            self.assertTrue(os.path.exists(path))


class TestEndToEnd(unittest.TestCase):
    def _make(self):
        return servo_mod.MockServo(), RecordingFeedback(), metrics_mod.Metrics()

    def test_move_success(self):
        servo, feedback, metrics = self._make()
        result = mvp_main.run_command("向左30度", servo, feedback, metrics)
        self.assertTrue(result.ok)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.state, mvp_main.State.FEEDBACK)
        self.assertEqual(result.states, [
            mvp_main.State.IDLE, mvp_main.State.THINK,
            mvp_main.State.ACT, mvp_main.State.FEEDBACK])
        self.assertAlmostEqual(servo.pan, -30.0)
        self.assertEqual(feedback.shown[-1], "PAN-30")
        self.assertIn("已转到", feedback.said[-1])
        self.assertIsNotNone(result.first_response_ms)
        self.assertGreaterEqual(result.total_ms, result.first_response_ms)
        kinds = [r.get("event") or r.get("stage") for r in metrics.records]
        self.assertIn("command", kinds)
        self.assertIn("action", kinds)
        self.assertIn("first_response", kinds)

    def test_tilt_and_clamp(self):
        servo, feedback, metrics = self._make()
        result = mvp_main.run_command("抬头", servo, feedback, metrics)
        self.assertTrue(result.ok)
        self.assertAlmostEqual(servo.tilt, 15.0)
        self.assertEqual(feedback.shown[-1], "TILT+15")

        result = mvp_main.run_command("转到200度", servo, feedback, metrics)
        self.assertTrue(result.ok)
        self.assertAlmostEqual(servo.pan, 75.0)
        self.assertIn("限位", result.reply)
        self.assertTrue(servo.events[-1].clamped)

    def test_stop_then_center(self):
        servo, feedback, metrics = self._make()
        result = mvp_main.run_command("停", servo, feedback, metrics)
        self.assertTrue(result.ok)
        self.assertTrue(servo.stopped)
        self.assertEqual(feedback.shown[-1], "STOP")

        result = mvp_main.run_command("回中", servo, feedback, metrics)
        self.assertTrue(result.ok)
        self.assertFalse(servo.stopped)
        self.assertEqual((servo.pan, servo.tilt), (0.0, 0.0))
        self.assertEqual(feedback.shown[-1], "CENT")

    def test_unknown_command_is_safe(self):
        servo, feedback, metrics = self._make()
        result = mvp_main.run_command("打开灯", servo, feedback, metrics)
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.state, mvp_main.State.ERROR)
        self.assertNotIn(mvp_main.State.ACT, result.states)
        self.assertEqual((servo.pan, servo.tilt), (0.0, 0.0))
        self.assertEqual(feedback.shown[-1], "ERR")
        self.assertEqual(servo.events, [])

    def test_servo_error_goes_to_error(self):
        servo, feedback, metrics = self._make()
        servo.stop()
        result = mvp_main.run_command("向左30度", servo, feedback, metrics)
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.state, mvp_main.State.ERROR)
        self.assertIn("急停", result.error)
        self.assertEqual(servo.pan, 0.0)
        self.assertEqual(feedback.shown[-1], "ERR")

    def test_defaults_and_serialisation(self):
        servo = servo_mod.MockServo()
        result = mvp_main.run_command("回中", servo)
        self.assertTrue(result.ok)
        payload = result.to_dict()
        self.assertEqual(payload["state"], "FEEDBACK")
        self.assertIsNone(payload["error"])
        json.dumps(payload, ensure_ascii=False)

    def test_empty_command(self):
        servo, feedback, metrics = self._make()
        result = mvp_main.run_command("   ", servo, feedback, metrics)
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.state, mvp_main.State.ERROR)


class TestCLI(unittest.TestCase):
    def test_once_move(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = mvp_main.main(["--mock", "--once", "向左30度"])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("state=FEEDBACK", out)
        self.assertIn("PAN-30", out)

    def test_once_unknown(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = mvp_main.main(["--mock", "--once", "打开灯"])
        self.assertEqual(code, 2)
        self.assertIn("state=ERROR", buf.getvalue())

    def test_once_requires_text(self):
        code = mvp_main.main(["--mock", "--once"])
        self.assertEqual(code, 2)

    def test_without_mock_is_rejected(self):
        code = mvp_main.main(["--once", "向左30度"])
        self.assertEqual(code, 2)

    def test_csv_jsonl_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "cli.csv")
            jsonl_path = os.path.join(tmp, "cli.jsonl")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = mvp_main.main([
                    "--mock", "--once", "抬头", "--csv", csv_path,
                    "--jsonl", jsonl_path])
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(csv_path))
            self.assertTrue(os.path.exists(jsonl_path))
            with open(csv_path, encoding="utf-8") as fh:
                self.assertTrue(fh.readline().startswith("ts,"))
            with open(jsonl_path, encoding="utf-8") as fh:
                self.assertGreaterEqual(len([line for line in fh if line.strip()]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
