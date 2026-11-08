#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PC 侧离线恢复包生成器单测。

用 tempfile 造一个最小假仓库，不依赖真实大文件；两种运行方式都支持：
    python -m unittest tests.test_offline_bundle
    python tests/test_offline_bundle.py
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

# 用 importlib 直接加载脚本，避免依赖 cwd / sys.path（脚本在 scripts/pc/ 下，
# 同目录还有依赖 paramiko 的 k1.py，不能把它们加进 sys.path）。
MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/pc/make_offline_bundle.py"
_spec = importlib.util.spec_from_file_location("make_offline_bundle", MODULE_PATH)
bundle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bundle)
sys.modules.setdefault("make_offline_bundle", bundle)

REVIEW = "/home/biandu/rv/room_review"

FAKE_REPO_FILES = {
    "app/main.py": b"# main\nprint('hi')\n",
    "app/room_review.py": b"# room review\n",
    "app/config.yaml": b"model: qwen3.5-0.8b\n",
    # 以下三个不应进包：非 .py/config.yaml 的 app 文件、字节码、app 子目录外的说明
    "app/photo_detect_config.json": b'{"should": "not be collected"}\n',
    "app/README.md": b"# docs, not collected\n",
    "app/__pycache__/main.cpython-311.pyc": b"\x00pyc",
    "configs/room_review_k1.json": b'{"k": 1}\n',
    "configs/risk_assessment_v1/system_prompt.zh.txt": "中文提示词\n".encode("utf-8"),
    "configs/risk_assessment_v1_08b/view_prompt.zh.txt": "08b 提示词\n".encode("utf-8"),
    "scripts/board/start_room_review.sh": b"#!/bin/sh\necho start\n",
    "scripts/board/RoomReview.desktop": b"[Desktop Entry]\nName=RoomReview\n",
    "scripts/board/room_asr_command.cpp": b"int main(){return 0;}\n",
    "scripts/board/room_asr_rvv.h": b"#pragma once\n",
    "scripts/board/test_room_asr_rvv.c": b"int main(void){return 0;}\n",
    "scripts/board/notes.txt": b"not collected\n",
    "scripts/validation/run_board_study.py": (
        b"from score_presence import evaluate\n\n# study\n"
    ),
    "scripts/validation/score_presence.py": b"def evaluate(*args, **kwargs):\n    return 1\n",
    "scripts/validation/analyze_risk_degree.py": b"# analyze\n",
    "gimbal/config.json": b'{"servo": 0}\n',
    "gimbal/cli.py": b"# cli\n",
    "gimbal/README.md": "# gimbal\n".encode("utf-8"),
    "results/room_review_20261104/audio/start.wav": b"RIFFfake-start",
    "results/room_review_20261104/audio/listen.wav": b"RIFFfake-listen",
    "results/room_review_20261104/asr_downloads/ggml-tiny.bin": b"tiny-model",
    "results/room_review_20261104/asr_downloads/ggml-base.bin": b"base-model",
    "results/room_review_20261104/asr_downloads/whisper-v1.5.5.tar.gz": b"whisper-tar",
    "results/k1_validation_20261103/runtime_archives/llama_017.tar.gz": b"llama-runtime",
    "results/k1_validation_20261103/runtime_archives/ort_206.tar.gz": b"ort-runtime",
}

BRINGUP_REL = "scripts/board/60_new_board_bringup.sh"


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def _make_repo(base, with_bringup=True, with_analysis=True, with_audio=True):
    """在 base/repo 下造一个最小假仓库，返回仓库根。"""
    root = Path(base) / "repo"
    for rel, data in FAKE_REPO_FILES.items():
        _write(root / rel, data)
    if with_bringup:
        _write(root / BRINGUP_REL, b"#!/bin/sh\necho bringup\n")
    if not with_analysis:
        (root / "scripts/validation/analyze_risk_degree.py").unlink()
    if not with_audio:
        shutil.rmtree(root / "results/room_review_20261104/audio")
    (root / ".cache/newboard").mkdir(parents=True, exist_ok=True)
    return root


def _entry_map(manifest):
    return {entry["target"]: entry for entry in manifest["entries"]}


class OfflineBundleTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.temp = Path(self._temp.name)

    # -- 辅助 ---------------------------------------------------------------

    def _build(self, **kwargs):
        repo = kwargs.pop("repo", None) or _make_repo(self.temp)
        kwargs.setdefault("out", self.temp / "out")
        return repo, bundle.build(root=repo, **kwargs)

    def _run_main(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = bundle.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    # -- manifest / payload 正确性 ------------------------------------------

    def test_manifest_targets_payloads_and_hashes(self):
        repo = _make_repo(self.temp)
        manifest = bundle.build(root=repo, out=self.temp / "out")
        entries = _entry_map(manifest)

        cases = {
            "app/main.py": f"{REVIEW}/app/main.py",
            "app/config.yaml": f"{REVIEW}/app/config.yaml",
            "configs/risk_assessment_v1/system_prompt.zh.txt": (
                f"{REVIEW}/configs/risk_assessment_v1/system_prompt.zh.txt"
            ),
            "scripts/board/start_room_review.sh": (
                f"{REVIEW}/scripts/board/start_room_review.sh"
            ),
            "scripts/board/test_room_asr_rvv.c": (
                f"{REVIEW}/scripts/board/test_room_asr_rvv.c"
            ),
            "scripts/validation/run_board_study.py": (
                f"{REVIEW}/scripts/validation/run_board_study.py"
            ),
            "scripts/validation/score_presence.py": (
                f"{REVIEW}/scripts/validation/score_presence.py"
            ),
            "gimbal/cli.py": "/home/biandu/rv/gimbal/cli.py",
            "results/room_review_20261104/audio/start.wav": f"{REVIEW}/audio/start.wav",
            "results/room_review_20261104/asr_downloads/ggml-base.bin": (
                f"{REVIEW}/asr/ggml-base.bin"
            ),
            "results/k1_validation_20261103/runtime_archives/llama_017.tar.gz": (
                "/home/biandu/rv/validation_20261103/runtime_archives/llama_017.tar.gz"
            ),
        }
        for rel, target in cases.items():
            self.assertIn(target, entries, rel)
            entry = entries[target]
            self.assertEqual(entry["payload"], "payload" + target)
            source_bytes = (repo / rel).read_bytes()
            self.assertEqual(entry["bytes"], len(source_bytes), rel)
            self.assertEqual(entry["sha256"], hashlib.sha256(source_bytes).hexdigest(), rel)
            self.assertTrue(entry["required"])
            payload_path = self.temp / "out" / entry["payload"]
            self.assertTrue(payload_path.is_file(), rel)
            self.assertEqual(payload_path.read_bytes(), source_bytes, rel)

        # kind 抽查
        self.assertEqual(entries[f"{REVIEW}/audio/start.wav"]["kind"], "audio")
        self.assertEqual(entries[f"{REVIEW}/asr/ggml-base.bin"]["kind"], "whisper")
        self.assertEqual(
            entries["/home/biandu/rv/validation_20261103/runtime_archives/llama_017.tar.gz"][
                "kind"
            ],
            "runtime",
        )
        self.assertEqual(
            entries[f"{REVIEW}/scripts/validation/run_board_study.py"]["kind"], "repo"
        )
        # 不应收录
        self.assertNotIn(f"{REVIEW}/app/README.md", entries)
        self.assertNotIn(f"{REVIEW}/app/photo_detect_config.json", entries)
        self.assertNotIn(f"{REVIEW}/scripts/board/notes.txt", entries)
        self.assertFalse(
            any("__pycache__" in entry["payload"] or entry["payload"].endswith(".pyc") for entry in manifest["entries"])
        )
        # bringup 脚本每次重新扫描 → 存在就收
        self.assertIn(f"{REVIEW}/scripts/board/60_new_board_bringup.sh", entries)

        # totals 与 entries 一致
        self.assertEqual(manifest["totals"]["files"], len(manifest["entries"]))
        self.assertEqual(
            manifest["totals"]["bytes"], sum(entry["bytes"] for entry in manifest["entries"])
        )
        self.assertEqual(manifest["version"], 1)
        self.assertIn("created", manifest)
        self.assertIn("root", manifest)

    def test_payload_mirrors_absolute_paths(self):
        repo, manifest = self._build()
        out = self.temp / "out"
        for entry in manifest["entries"]:
            self.assertTrue(entry["target"].startswith("/"), entry["target"])
            self.assertEqual(entry["payload"], "payload" + entry["target"])
            self.assertTrue((out / entry["payload"]).is_file(), entry["target"])
        # payload 下没有多余文件
        payload_files = {
            path.relative_to(out).as_posix()
            for path in (out / "payload").rglob("*")
            if path.is_file()
        }
        self.assertEqual(payload_files, {entry["payload"] for entry in manifest["entries"]})

    def test_sha256sums_can_be_checked_independently(self):
        repo, manifest = self._build()
        out = self.temp / "out"
        sums_path = out / "SHA256SUMS"
        raw = sums_path.read_bytes()
        self.assertNotIn(b"\r", raw, "SHA256SUMS 必须是 LF 换行")
        lines = raw.decode("utf-8").splitlines()
        self.assertEqual(len(lines), len(manifest["entries"]))

        payload_rel = [line.split("  ", 1)[1] for line in lines]
        self.assertEqual(payload_rel, sorted(payload_rel), "必须按路径排序")
        # 路径相对包根（payload/...），因此校验命令在包根执行
        self.assertTrue(all(rel.startswith("payload/") for rel in payload_rel))

        seen = set()
        for line in lines:
            digest, rel = line.split("  ", 1)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            actual = hashlib.sha256((out / rel).read_bytes()).hexdigest()
            self.assertEqual(actual, digest, rel)
            seen.add(rel)
        self.assertEqual(seen, {entry["payload"] for entry in manifest["entries"]})
        # SHA256SUMS 能被 sha256sum -c 识别的格式：两个空格分隔
        for line in lines:
            self.assertIn("  ", line)

    def test_manifest_json_on_disk_matches_result(self):
        repo, manifest = self._build()
        out = self.temp / "out"
        on_disk = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk, manifest)
        self.assertNotIn("\r", (out / "manifest.json").read_bytes().decode("utf-8"))

    # -- missing 大模型 ------------------------------------------------------

    def test_missing_lists_qwen_and_sensevoice_with_urls(self):
        repo, manifest = self._build()
        missing = {item["target"]: item for item in manifest["missing"]}
        self.assertIn("/home/biandu/.cache/models/vlm/Qwen3.5-0.8B", missing)
        self.assertEqual(
            missing["/home/biandu/.cache/models/vlm/Qwen3.5-0.8B"]["url"],
            "http://archive.spacemit.com/spacemit-ai/model_zoo/vlm/Qwen3.5-0.8B.tar.gz",
        )
        self.assertGreater(missing["/home/biandu/.cache/models/vlm/Qwen3.5-0.8B"]["bytes_estimate"], 0)
        self.assertIn("/home/biandu/rv/asr/sensevoice", missing)
        self.assertEqual(
            missing["/home/biandu/rv/asr/sensevoice"]["url"],
            "https://archive.spacemit.com/spacemit-ai/openwebui/sensevoice.tar.gz",
        )
        self.assertIn("/home/biandu/.cache/models/vlm/Qwen3.5-2B", missing)
        self.assertTrue(missing["/home/biandu/.cache/models/vlm/Qwen3.5-2B"].get("optional"))
        # models-dir 为空时不应有 model 条目
        self.assertNotIn(
            "/home/biandu/rv/offline/models/Qwen3.5-0.8B.tar.gz",
            {entry["target"] for entry in manifest["entries"]},
        )

    def test_models_dir_tarballs_enter_entries_and_leave_missing(self):
        repo = _make_repo(self.temp)
        models = self.temp / "models"
        _write(models / "Qwen3.5-0.8B.tar.gz", b"fake-qwen-archive")
        _write(models / "sensevoice.tar.gz", b"fake-sensevoice-archive")
        manifest = bundle.build(root=repo, out=self.temp / "out", models_dir=models)
        entries = _entry_map(manifest)
        for target, local in (
            ("/home/biandu/rv/offline/models/Qwen3.5-0.8B.tar.gz", models / "Qwen3.5-0.8B.tar.gz"),
            ("/home/biandu/rv/offline/models/sensevoice.tar.gz", models / "sensevoice.tar.gz"),
        ):
            self.assertIn(target, entries)
            self.assertEqual(entries[target]["kind"], "model")
            self.assertTrue(entries[target]["required"])
            payload = self.temp / "out" / entries[target]["payload"]
            self.assertEqual(payload.read_bytes(), local.read_bytes())
        # 解压位置与 new_board_bringup.py 的约定一致
        self.assertEqual(entries["/home/biandu/rv/offline/models/Qwen3.5-0.8B.tar.gz"]["unpack_to"], "/home/biandu/.cache/models/vlm")
        self.assertEqual(entries["/home/biandu/rv/offline/models/Qwen3.5-0.8B.tar.gz"]["model_dir"], "/home/biandu/.cache/models/vlm/Qwen3.5-0.8B")
        self.assertEqual(entries["/home/biandu/rv/offline/models/sensevoice.tar.gz"]["unpack_to"], "/home/biandu/rv/asr/sensevoice")
        self.assertEqual(entries["/home/biandu/rv/offline/models/sensevoice.tar.gz"]["model_dir"], "/home/biandu/rv/asr/sensevoice/sensevoice")

        missing_targets = {item["target"] for item in manifest["missing"]}
        self.assertNotIn("/home/biandu/.cache/models/vlm/Qwen3.5-0.8B", missing_targets)
        self.assertNotIn("/home/biandu/rv/asr/sensevoice", missing_targets)
        # 可选的 2B 没有本地包，仍应保留在 missing
        self.assertIn("/home/biandu/.cache/models/vlm/Qwen3.5-2B", missing_targets)

    # -- check-only ---------------------------------------------------------

    def test_check_only_creates_nothing(self):
        repo = _make_repo(self.temp)
        out = self.temp / "check_out"
        before = sorted(path.relative_to(self.temp).as_posix() for path in self.temp.rglob("*"))
        manifest = bundle.build(root=repo, out=out, check_only=True)
        after = sorted(path.relative_to(self.temp).as_posix() for path in self.temp.rglob("*"))
        self.assertEqual(before, after, "--check-only 不得创建任何文件/目录")
        self.assertFalse(out.exists())
        self.assertFalse(Path(str(out) + ".zip").exists())
        self.assertGreater(manifest["totals"]["files"], 0)
        self.assertTrue(manifest["check_only"])
        # 清单仍然完整
        self.assertTrue(manifest["entries"])
        self.assertTrue(manifest["missing"])

    def test_check_only_cli_does_not_write(self):
        repo = _make_repo(self.temp)
        out = self.temp / "cli_check_out"
        code, stdout, _ = self._run_main(
            ["--root", str(repo), "--out", str(out), "--check-only"]
        )
        self.assertEqual(code, 0)
        self.assertIn("check-only", stdout)
        self.assertFalse(out.exists())

    # -- zip ----------------------------------------------------------------

    def test_zip_bundle_contains_manifest_and_payload(self):
        repo = _make_repo(self.temp)
        manifest = bundle.build(root=repo, out=self.temp / "zout", zip_bundle=True)
        zip_path = Path(manifest["zip"])
        self.assertTrue(zip_path.is_file())
        self.assertEqual(zip_path, self.temp / "zout.zip")
        with zipfile.ZipFile(zip_path) as archive:
            names = set(archive.namelist())
            self.assertIn("manifest.json", names)
            self.assertIn("SHA256SUMS", names)
            self.assertIn("README-恢复说明.txt", names)
            payload_name = "payload" + f"{REVIEW}/app/main.py"
            self.assertIn(payload_name, names)
            self.assertEqual(
                archive.read(payload_name), (repo / "app/main.py").read_bytes()
            )
            # manifest.json 可从 zip 内解析
            packed = json.loads(archive.read("manifest.json").decode("utf-8"))
            self.assertEqual(packed, json.loads((self.temp / "zout/manifest.json").read_text(encoding="utf-8")))
            # 压缩方式与可执行位
            info = archive.getinfo(payload_name)
            self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
            script = "payload" + f"{REVIEW}/scripts/board/start_room_review.sh"
            self.assertEqual((archive.getinfo(script).external_attr >> 16) & 0o777, 0o755)

    # -- json ---------------------------------------------------------------

    def test_json_mode_prints_one_parseable_summary(self):
        repo = _make_repo(self.temp)
        out = self.temp / "jout"
        code, stdout, _ = self._run_main(["--root", str(repo), "--out", str(out), "--json"])
        self.assertEqual(code, 0)
        data = json.loads(stdout)  # 必须是单个可解析 JSON
        self.assertEqual(data["totals"]["files"], len(data["entries"]))
        self.assertEqual(data["out"], str(out))
        self.assertNotIn("新板离线恢复包", stdout)

    def test_json_check_only_is_parseable(self):
        repo = _make_repo(self.temp)
        code, stdout, _ = self._run_main(["--root", str(repo), "--check-only", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(stdout)
        self.assertTrue(data["check_only"])
        self.assertFalse((repo / "dist").exists())

    # -- 缺文件只警告，不中断 ------------------------------------------------

    def test_missing_source_only_warns_and_still_builds(self):
        repo = _make_repo(self.temp, with_analysis=False, with_audio=False)
        manifest = bundle.build(root=repo, out=self.temp / "wout")
        text = "\n".join(manifest["warnings"])
        self.assertIn("scripts/validation/analyze_risk_degree.py", text)
        self.assertIn("results/room_review_20261104/audio", text)
        targets = {entry["target"] for entry in manifest["entries"]}
        self.assertNotIn(f"{REVIEW}/scripts/validation/analyze_risk_degree.py", targets)
        self.assertGreater(manifest["totals"]["files"], 0)
        # 通过 CLI 跑也是 0，不中断
        code, _, _ = self._run_main(
            ["--root", str(repo), "--out", str(self.temp / "wout2"), "--json"]
        )
        self.assertEqual(code, 0)

    def test_bringup_script_is_optional_and_rescanned(self):
        repo = _make_repo(self.temp, with_bringup=False)
        manifest = bundle.build(root=repo, out=self.temp / "no_bringup")
        self.assertIn(
            "scripts/board/60_new_board_bringup.sh", "\n".join(manifest["warnings"])
        )
        self.assertFalse(
            any("60_new_board_bringup.sh" in entry["target"] for entry in manifest["entries"])
        )
        # 创建后再跑，应被收录（不缓存“不存在”结论）
        _write(repo / BRINGUP_REL, b"#!/bin/sh\necho bringup\n")
        manifest = bundle.build(root=repo, out=self.temp / "with_bringup")
        self.assertIn(
            f"{REVIEW}/scripts/board/60_new_board_bringup.sh",
            _entry_map(manifest),
        )

    def test_validation_local_imports_are_collected(self):
        """run_board_study.py 这类脚本 import 的同目录模块必须在包里。"""
        repo, manifest = self._build()
        targets = {entry["target"] for entry in manifest["entries"]}
        script = repo / "scripts/validation/run_board_study.py"
        self.assertIn(b"from score_presence import evaluate", script.read_bytes())
        local_modules = {"score_presence"}
        for name in local_modules:
            self.assertIn(f"{REVIEW}/scripts/validation/{name}.py", targets)
        self.assertIn(
            f"{REVIEW}/scripts/validation/score_presence.py", _entry_map(manifest)
        )

    # -- README -------------------------------------------------------------

    def test_readme_is_short_and_mentions_restore_commands(self):
        repo, manifest = self._build()
        readme = (self.temp / "out" / "README-恢复说明.txt").read_text(encoding="utf-8")
        self.assertLessEqual(len(readme.splitlines()), 40)
        self.assertIn("cp -a payload/. /", readme)
        # SHA256SUMS 路径相对包根 → 校验命令在包根执行
        self.assertIn("cd <包目录> && sha256sum -c SHA256SUMS", readme)
        self.assertNotIn("../SHA256SUMS", readme)
        # 一键恢复由脚本自己调 sudo，不能 sudo 运行（HOME 会变 /root）
        self.assertIn("install --bundle", readme)
        self.assertIn("new_board_bringup.py verify", readme)
        self.assertNotIn("sudo bash", readme)
        self.assertNotIn("37_verify.sh", readme)
        # 模型解压后的最终目录
        self.assertIn("~/.cache/models/vlm/Qwen3.5-0.8B/", readme)
        self.assertIn("~/rv/asr/sensevoice/sensevoice/", readme)
        self.assertIn("Qwen3.5-0.8B", readme)
        self.assertIn("sensevoice.tar.gz", readme)
        self.assertNotIn("\r", readme)

    # -- 参数错误 -----------------------------------------------------------

    def test_bad_root_returns_2(self):
        code, _, stderr = self._run_main(
            ["--root", str(self.temp / "does-not-exist"), "--out", str(self.temp / "o")]
        )
        self.assertEqual(code, 2)
        self.assertIn("--root", stderr)
        self.assertFalse((self.temp / "o").exists())

    def test_non_empty_out_is_rejected(self):
        repo = _make_repo(self.temp)
        out = self.temp / "used"
        out.mkdir()
        _write(out / "keep.txt", b"keep")
        code, _, stderr = self._run_main(["--root", str(repo), "--out", str(out)])
        self.assertEqual(code, 2)
        self.assertIn("非空", stderr)
        self.assertEqual((out / "keep.txt").read_bytes(), b"keep")

    def test_default_out_uses_dist_and_local_date(self):
        repo = _make_repo(self.temp)
        manifest = bundle.build(root=repo)
        expected = repo / "dist" / f"newboard_bundle_{datetime.now().strftime('%Y%m%d')}"
        self.assertEqual(Path(manifest["out"]), expected)
        self.assertTrue((expected / "manifest.json").is_file())
        # 清理默认输出，避免 TemporaryDirectory 之外残留（这里仍在其内）
        shutil.rmtree(expected, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
