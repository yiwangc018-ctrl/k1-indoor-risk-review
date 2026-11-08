"""云台四角采集接 room_review：不需要 K1、相机或舵机。"""
import contextlib
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import room_review as review
import gimbal_scan


class FakeGimbal:
    """只实现 run_scan 需要的接口；mpu=None 表示没有角度传感器。"""

    def __init__(self, cfg, fail_gate=False):
        self.cfg = cfg
        self.mpu = None
        self.moves = []
        self.started = False
        self.stopped = None
        self.home_calls = 0
        self.fail_gate = fail_gate

    def start(self, **kwargs):
        self.started = True

    def stop(self, relax=True):
        self.stopped = {"relax": relax}

    def goto(self, pan=None, tilt=None, blocking=True, timeout=30.0, settle=True):
        self.moves.append((pan, tilt))
        return True

    def level_gate(self, **kwargs):
        return {"ok": True, "reason": "ok", "roll": None, "pitch": None}

    def capture_gate(self):
        return (False, ["未到位"]) if self.fail_gate else (True, [])

    def home(self, blocking=True):
        self.home_calls += 1
        return True


class ScanRoundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = review.load(review.ROOT / "configs/room_review_k1.json")
        self.config["inbox"] = str(self.root)
        self.config["audio"]["enabled"] = False
        self.config["gimbal"]["state_file"] = str(self.root / "display_state.json")
        # 板端配置写的是 /home/biandu/rv/...；PC 上换成仓库内的同一份文件。
        self.config["gimbal"]["config"] = str(review.ROOT / "gimbal/config.json")
        self.gcfg, _ = gimbal_scan.load_gimbal_config(self.config)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def fake_capture_image(config, target, log_path=None):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 48), "gray").save(target, format="JPEG")
        return {"size": [64, 48], "seconds": 0.01}

    def run_round(self, gimbal=None, capture=None, **kwargs):
        gimbal = gimbal or FakeGimbal(self.gcfg)
        capture = capture or self.fake_capture_image
        with patch.object(review, "exclusive", contextlib.nullcontext), \
             patch.object(review, "capture_image", side_effect=capture):
            return gimbal_scan.capture_round_scan(self.config, gimbal=gimbal,
                                                  gcfg=self.gcfg, **kwargs)

    def test_four_views_with_pose_and_ready(self):
        gimbal = FakeGimbal(self.gcfg)
        folder = self.run_round(gimbal)
        meta = review.load(folder / "meta.json")
        self.assertEqual(meta["status"], "captured")
        self.assertEqual(meta["requested_views"], 4)
        self.assertEqual([v["view_id"] for v in meta["views"]],
                         ["view_01", "view_02", "view_03", "view_04"])
        # 预设 0/90/180/270 会被软限位夹到 2/90/180/267；记录的是夹紧后的命令值。
        self.assertEqual([v["pose"]["commanded"]["pan"] for v in meta["views"]],
                         [2.0, 90.0, 180.0, 267.0])
        self.assertEqual({v["pose"]["commanded"]["tilt"] for v in meta["views"]}, {-30.0})
        self.assertTrue(all(v["pose"]["source"] == "commanded" for v in meta["views"]))
        self.assertTrue(all(v["pose"]["measured"] is None for v in meta["views"]))
        self.assertEqual(len(review.ready_views(folder)), 4)
        self.assertTrue((folder / "scan_manifest.json").is_file())
        self.assertEqual(gimbal.home_calls, 1)
        self.assertEqual(gimbal.stopped, {"relax": False})
        self.assertEqual(len([m for m in gimbal.moves if m[0] is not None]), 4)

    def test_state_updated_per_view_and_shared_state_file(self):
        folder = self.run_round()
        state = review.load(self.root / "display_state.json")
        self.assertEqual(state["phase"], "done")
        self.assertEqual(state["round_id"], folder.name)
        self.assertFalse(state["analyzed"])
        self.assertIn("已拍 4/4", state["message"])
        with_photos = review.load(folder / "state.json")
        self.assertEqual(with_photos["total"], 4)

    def test_ready_views_carry_pose_for_analysis(self):
        folder = self.run_round()
        manifest = review.load(folder / "READY.json")
        self.assertEqual(len(manifest["views"]), 4)
        self.assertEqual(manifest["views"][0]["pose"]["commanded"]["pan"], 2.0)
        self.assertEqual(manifest["views"][2]["pose"]["commanded"]["pan"], 180.0)

    def test_analyze_is_optional_and_reported(self):
        result = {"overall_level": "medium", "status": "complete"}
        with patch.object(review, "analyze_round", return_value=result) as analyze:
            folder = self.run_round(analyze=True)
        analyze.assert_called_once()
        state = review.load(self.root / "display_state.json")
        self.assertEqual(state["phase"], "done")
        self.assertTrue(state["analyzed"])
        self.assertEqual(state["level"], "medium")

    def test_no_photo_means_no_ready_and_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "没有拍到照片"):
            self.run_round(gimbal=FakeGimbal(self.gcfg, fail_gate=True))
        folders = list(self.root.glob("round_*"))
        self.assertEqual(len(folders), 1)
        meta = review.load(folders[0] / "meta.json")
        self.assertEqual(meta["status"], "capture_failed")
        self.assertFalse((folders[0] / "READY.json").exists())
        self.assertEqual(review.load(self.root / "display_state.json")["phase"], "error")

    def test_dry_run_touches_no_photo(self):
        folder = self.run_round(dry_run=True)
        meta = review.load(folder / "meta.json")
        self.assertEqual(meta["status"], "dry_run")
        self.assertEqual(meta["views"], [])
        self.assertFalse((folder / "READY.json").exists())

    def test_one_failed_photo_leaves_a_partial_round(self):
        calls = {"n": 0}

        def flaky(config, target, log_path=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("模拟相机失败")
            return self.fake_capture_image(config, target, log_path)

        folder = self.run_round(capture=flaky)
        meta = review.load(folder / "meta.json")
        self.assertEqual(meta["status"], "partial_capture")
        self.assertEqual(len(meta["views"]), 3)
        self.assertEqual(len(review.ready_views(folder)), 3)
        self.assertEqual(review.load(self.root / "display_state.json")["phase"], "done")


if __name__ == "__main__":
    unittest.main()
