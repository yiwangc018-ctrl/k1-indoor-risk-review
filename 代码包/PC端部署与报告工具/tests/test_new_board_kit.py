#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新板恢复工具链的离线单测：不需要板子、不联网、不写仓库。

覆盖：
* scripts/board/new_board_bringup.py：哈希、md5 解析、SMT 配置生成、离线包发现、
  步骤选择、离线缺件行为、常量与本机兜底；
* scripts/pc/accept_new_board.py：各检查项的纯解析函数、dry-run、命令表完整性。
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bringup = load("new_board_bringup", "scripts/board/new_board_bringup.py")
accept = load("accept_new_board", "scripts/pc/accept_new_board.py")


class HashTests(unittest.TestCase):
    def test_sha256_and_md5(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.bin"
            path.write_bytes(b"hello")
            self.assertEqual(bringup.sha256_file(path),
                             "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")
            self.assertEqual(bringup.md5_file(path), "5d41402abc4b2a76b9719d911017c592")

    def test_parse_md5_file(self):
        self.assertEqual(
            bringup.parse_md5_file("e780db6364024d9315eaad50e613421d  ./Qwen3.5-0.8B.tar.gz\n"),
            "e780db6364024d9315eaad50e613421d")
        self.assertEqual(bringup.parse_md5_file("\n\n"), "")
        self.assertEqual(bringup.parse_md5_file(""), "")


class SMTConfigTests(unittest.TestCase):
    def test_write_smt_config_rewrites_paths_and_size(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qwen = root / "Qwen3.5-0.8B"
            qwen.mkdir()
            (qwen / "config.json").write_text(json.dumps({
                "vision_model": {"model_path": "./qwen3_5vl_0.8b-vision-384-op23.f16.onnx",
                                 "input_size": 384},
                "text_model": {"model_path": "./qwen3_5vl_0.8b-text-q41.gguf"},
            }), encoding="utf-8")
            room = root / "room_review"
            with mock.patch.object(bringup, "QWEN_DIR", qwen), \
                 mock.patch.object(bringup, "ROOM", room):
                self.assertTrue(bringup.write_smt_config())
            out = room / "configs/vlm_smt_0.8b_224/config.json"
            cfg = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(cfg["vision_model"]["input_size"], 224)
            self.assertTrue(cfg["vision_model"]["model_path"].endswith(
                "qwen3_5vl_0.8b-vision-224-op23.f16.onnx"))
            self.assertTrue(Path(cfg["vision_model"]["model_path"]).is_absolute())
            self.assertTrue(Path(cfg["text_model"]["model_path"]).is_absolute())

    def test_write_smt_config_without_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(bringup, "QWEN_DIR", root / "missing"), \
                 mock.patch.object(bringup, "ROOM", root):
                self.assertFalse(bringup.write_smt_config())


class BundleAndStepTests(unittest.TestCase):
    def test_find_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = root / "payload"
            payload.mkdir()
            (root / "manifest.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(bringup, "OFFLINE", root / "nope"), \
                 mock.patch.object(bringup, "BOARD", root / "nope2"), \
                 mock.patch("pathlib.Path.cwd", return_value=root / "elsewhere"):
                self.assertEqual(bringup.find_bundle(str(root)), root)
                self.assertIsNone(bringup.find_bundle(str(root / "missing")))

    def test_selected_steps_only_and_skip(self):
        args = mock.Mock(only="models,vnc", skip=None)
        names = [name for name, _ in bringup.selected_steps(args)]
        self.assertEqual(names, ["models", "vnc"])
        args = mock.Mock(only=None, skip="apt,verify")
        names = [name for name, _ in bringup.selected_steps(args)]
        self.assertNotIn("apt", names)
        self.assertNotIn("verify", names)
        self.assertIn("models", names)
        with self.assertRaises(SystemExit):
            bringup.selected_steps(mock.Mock(only="nope", skip=None))

    def test_ensure_tar_offline_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "missing.tar.gz"
            bringup.RESULTS.clear()
            self.assertFalse(bringup.ensure_tar(missing, "http://example.invalid/x", offline=True))
            self.assertEqual(bringup.RESULTS[-1]["status"], bringup.FAIL)

    def test_ensure_tar_existing_checks_sha(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ok.tar.gz"
            path.write_bytes(b"hello")
            bringup.RESULTS.clear()
            self.assertTrue(bringup.ensure_tar(
                path, "http://example.invalid/x",
                sha256="2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"))
            self.assertEqual(bringup.RESULTS[-1]["status"], bringup.OK)
            self.assertFalse(bringup.ensure_tar(
                path, "http://example.invalid/x",
                sha256="0" * 64, offline=True))

    def test_write_user_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a/b/c.txt"
            bringup.DRY = False
            bringup.write_user_file(path, "内容")
            self.assertEqual(path.read_text(encoding="utf-8"), "内容")


class ConstantsTests(unittest.TestCase):
    def test_required_files_cover_models_and_runtime(self):
        text = "\n".join(str(p) for p in bringup.REQUIRED_FILES).replace("\\", "/")
        for token in ("Qwen3.5-0.8B", "model_quant_optimized.onnx",
                      "spacemit-llama.cpp.riscv64.0.1.7", "start.wav",
                      "room_review_k1.json", "gimbal/config.json"):
            self.assertIn(token, text)

    def test_vnc_and_udev_constants(self):
        self.assertIn("vncserver", bringup.VNC_UNIT)
        self.assertIn("SecurityTypes None", bringup.VNC_UNIT)
        self.assertIn("startxfce4", bringup.XSTARTUP)
        self.assertNotIn("startlxqt", bringup.XSTARTUP)
        self.assertIn("Hidden=true", bringup.LIGHT_LOCKER_AUTOSTART)
        self.assertIn("color-manager", bringup.POLKIT_COLOR_RULE)
        self.assertEqual(set(bringup.UDEV_RULES), {"90-k1-gpio.rules", "99-i2c.rules"})
        self.assertIn("MemoryMax=512M", bringup.PIPEWIRE_LIMIT)

    def test_identity_has_fallback_keys(self):
        info = bringup.identity()
        for key in ("arch", "kernel", "board", "os", "python", "cpu", "mem_mb", "disk"):
            self.assertIn(key, info)
        self.assertTrue(info["arch"])
        self.assertTrue(info["python"])


class AcceptanceEvalTests(unittest.TestCase):
    def test_eval_identity(self):
        out = ("ARCH=riscv64\nMODEL=SpacemiT K1 MUSE Pi Pro\n"
               "OS=Bianbu 2.3.5\nKERNEL=6.6.63\nPY=Python 3.12.3\n"
               "MEM_MB=15800\nDISK=42G\n")
        checks = accept.eval_identity(out)
        names = {c["name"]: c["status"] for c in checks}
        self.assertEqual(names["架构 riscv64"], accept.OK)
        self.assertEqual(names["板型 MUSE Pi Pro / K1"], accept.OK)
        self.assertEqual(names["系统 Bianbu"], accept.OK)
        self.assertEqual(names["Python 3"], accept.OK)
        self.assertEqual(names["内存 ≥ 7.5GB"], accept.OK)

    def test_eval_identity_bad_arch(self):
        checks = accept.eval_identity("ARCH=x86_64\nPY=Python 3.11\nMEM_MB=100\n")
        names = {c["name"]: c["status"] for c in checks}
        self.assertEqual(names["架构 riscv64"], accept.FAIL)
        self.assertEqual(names["板型 MUSE Pi Pro / K1"], accept.WARN)
        self.assertEqual(names["内存 ≥ 7.5GB"], accept.WARN)

    def test_eval_files(self):
        checks = accept.eval_files("OK|/a\nMISS|/b\n")
        self.assertEqual([c["status"] for c in checks], [accept.OK, accept.FAIL])
        self.assertIn("/b", checks[1]["name"])

    def test_eval_tools(self):
        checks = accept.eval_tools("python3=/usr/bin/python3\nvncserver=\n")
        self.assertEqual(checks[0]["status"], accept.OK)
        self.assertEqual(checks[1]["status"], accept.WARN)

    def test_eval_devices_and_audio(self):
        devices = accept.eval_devices(
            "VIDEO=/dev/video0 \nI2C=/dev/i2c-4 \nGPIO=/dev/gpiochip0 \nUSB=0c45:636b 08bb:2902\n")
        statuses = {c["name"]: c["status"] for c in devices}
        self.assertEqual(statuses["摄像头 /dev/video*"], accept.OK)
        self.assertEqual(statuses["USB 摄像头 0c45:636b"], accept.OK)
        self.assertEqual(statuses["USB 音箱 4c4a:4155"], accept.WARN)
        audio = accept.eval_audio("CAPTURE=1\nPLAYBACK=2\n")
        self.assertEqual([c["status"] for c in audio], [accept.OK, accept.OK])

    def test_eval_services(self):
        checks = accept.eval_services(
            "VNC=active\nPORT5901=yes\nPIPEWIRE_MAX=536870912\n")
        self.assertTrue(all(c["status"] == accept.OK for c in checks))
        checks = accept.eval_services("VNC=inactive\nPORT5901=no\nPIPEWIRE_MAX=\n")
        self.assertTrue(all(c["status"] == accept.WARN for c in checks))

    def test_eval_simple(self):
        self.assertEqual(accept.eval_simple("x", True, "")[0]["status"], accept.OK)
        self.assertEqual(accept.eval_simple("x", False, "")[0]["status"], accept.WARN)

    def test_command_table_has_required_groups(self):
        for name in ("identity", "files", "tools", "devices", "audio",
                     "services", "vlm_health", "imports", "gimbal", "audio_check", "capture"):
            self.assertIn(name, accept.COMMANDS)

    def test_dry_run(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = accept.main(["--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("identity", buffer.getvalue())
        self.assertIn("capture", buffer.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
