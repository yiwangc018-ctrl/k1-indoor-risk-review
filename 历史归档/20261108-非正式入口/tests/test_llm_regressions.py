#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 配置与 --check 网络失败的回归测试。

    python tests/test_llm_regressions.py

覆盖:
* 显式配置路径不存在 -> ConfigError, ask.main 非零退出
* 显式配置存在但缺 PyYAML -> ConfigError, ask.main 非零退出
* 自动查找配置且缺 PyYAML -> 保持默认值降级
* LLM._get / LLM.check 遇到 URLError / TimeoutError / OSError -> (False, 原因)
* --check 遇到超时 -> 不抛 traceback, 退出码 2

不访问真实网络, 不写 results 文件.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import ask  # noqa: E402
import llm  # noqa: E402


def _llm_cfg(backend="openai", url="http://127.0.0.1:9", timeout=0.1):
    return {"llm": {"backend": backend, "url": url, "timeout": timeout}}


class _Response:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


class TestExplicitConfig(unittest.TestCase):
    def test_missing_explicit_path_raises_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "does-not-exist.yaml")
            with self.assertRaises(llm.ConfigError) as ctx:
                llm.load_config(path)
            self.assertIn("不存在", str(ctx.exception))

    def test_missing_explicit_path_cli_returns_nonzero_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "does-not-exist.yaml")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = ask.main([
                    "--config", path, "--mock", "--no-display", "输出数字123"])
            self.assertEqual(code, ask.EXIT_CONFIG)
            self.assertIn("配置错误", err.getvalue())
            self.assertIn("不存在", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())
            self.assertNotIn("Traceback", out.getvalue())

    def test_explicit_config_without_pyyaml_raises_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ok.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("llm:\n  backend: mock\n")
            with mock.patch.dict(sys.modules, {"yaml": None}):
                with self.assertRaises(llm.ConfigError) as ctx:
                    llm.load_config(path)
            self.assertIn("pyyaml", str(ctx.exception).lower())

    def test_explicit_config_without_pyyaml_cli_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ok.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("llm:\n  backend: mock\n")
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(sys.modules, {"yaml": None}):
                with redirect_stdout(out), redirect_stderr(err):
                    code = ask.main([
                        "--config", path, "--mock", "--no-display", "输出数字123"])
            self.assertEqual(code, ask.EXIT_CONFIG)
            self.assertIn("配置错误", err.getvalue())
            self.assertIn("pyyaml", err.getvalue().lower())
            self.assertNotIn("Traceback", err.getvalue())

    def test_auto_config_without_pyyaml_degrades_to_defaults(self):
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"yaml": None}):
            with redirect_stdout(out):
                cfg = llm.load_config()
        self.assertEqual(cfg["llm"]["backend"], "llamacpp")
        self.assertEqual(int(cfg["display"]["bus"]), 4)
        self.assertIn("未装 pyyaml", out.getvalue())

    def test_valid_explicit_config_loads_and_merges_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ok.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("llm:\n  backend: mock\n  timeout: 1.0\n")
            cfg = llm.load_config(path)
        self.assertEqual(cfg["llm"]["backend"], "mock")
        self.assertEqual(cfg["llm"]["timeout"], 1.0)
        self.assertEqual(int(cfg["display"]["bus"]), 4)

    def test_bad_yaml_raises_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("llm: [\n")
            with self.assertRaises(llm.ConfigError) as ctx:
                llm.load_config(path)
        self.assertIn("YAML", str(ctx.exception))

    def test_non_mapping_root_raises_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "list.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("- a\n- b\n")
            with self.assertRaises(llm.ConfigError) as ctx:
                llm.load_config(path)
        self.assertIn("根节点", str(ctx.exception))

    def test_invalid_value_in_explicit_config_returns_config_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad-value.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("llm:\n  timeout: not-a-number\n")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = ask.main([
                    "--config", path, "--mock", "--no-display", "输出数字123"])
            self.assertEqual(code, ask.EXIT_CONFIG)
            self.assertIn("配置错误", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())


class TestGetAndCheck(unittest.TestCase):
    def setUp(self):
        self.cli = llm.LLM(_llm_cfg())

    def _get_with(self, exc):
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            return self.cli._get("/v1/models")

    def test_get_urlerror_returns_false(self):
        ok, detail = self._get_with(urllib.error.URLError("boom"))
        self.assertFalse(ok)
        self.assertIn("127.0.0.1:9", detail)
        self.assertIn("boom", detail)

    def test_get_urlerror_with_timeout_reason_returns_timeout(self):
        ok, detail = self._get_with(urllib.error.URLError(TimeoutError("timed out")))
        self.assertFalse(ok)
        self.assertIn("超时", detail)

    def test_get_timeout_returns_false(self):
        ok, detail = self._get_with(TimeoutError("timed out"))
        self.assertFalse(ok)
        self.assertIn("超时", detail)
        self.assertIn("timed out", detail)

    def test_get_oserror_returns_false(self):
        ok, detail = self._get_with(OSError("broken pipe"))
        self.assertFalse(ok)
        self.assertIn("IO", detail)
        self.assertIn("broken pipe", detail)

    def test_get_httperror_returns_false(self):
        exc = urllib.error.HTTPError(
            "http://127.0.0.1:9/v1/models", 503, "busy", None, None)
        ok, detail = self._get_with(exc)
        self.assertFalse(ok)
        self.assertIn("503", detail)

    def test_get_success_returns_tuple(self):
        with mock.patch("urllib.request.urlopen", return_value=_Response(b"ok")):
            ok, body = self.cli._get("/v1/models")
        self.assertTrue(ok)
        self.assertEqual(body, "ok")

    def test_check_returns_false_for_all_network_errors(self):
        cases = [
            urllib.error.URLError("boom"),
            TimeoutError("timed out"),
            OSError("broken pipe"),
        ]
        for exc in cases:
            with self.subTest(exc=type(exc).__name__):
                with mock.patch("urllib.request.urlopen", side_effect=exc):
                    ok, detail = self.cli.check()
                self.assertFalse(ok)
                self.assertIsInstance(detail, str)
                self.assertTrue(detail)

    def test_check_cli_timeout_returns_2_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "check.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    "llm:\n"
                    "  backend: openai\n"
                    "  url: http://127.0.0.1:9\n"
                    "  timeout: 0.1\n")
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("x")):
                with redirect_stdout(out), redirect_stderr(err):
                    code = ask.main(["--check", "--config", path])
            self.assertEqual(code, ask.EXIT_LLM)
            self.assertIn("不可用", out.getvalue())
            self.assertIn("超时", out.getvalue())
            self.assertNotIn("Traceback", out.getvalue())
            self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
