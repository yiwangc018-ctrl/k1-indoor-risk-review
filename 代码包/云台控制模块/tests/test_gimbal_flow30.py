"""云台 30 秒流程的离线测试：假 I2C + 假相机，不接板、不动舵机。"""
from contextlib import nullcontext
import json
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import gimbal_flow30 as flow


class FakePulse:
    def __init__(self, pan=512.7, tilt=2055.7):
        self.values = {flow.PAN_CHANNEL: pan, flow.TILT_CHANNEL: tilt}
        self.writes = []
        self.closed = False

    def read_us(self, channel):
        return self.values[channel]

    def write_us(self, channel, us):
        self.values[channel] = float(us)
        self.writes.append((channel, float(us)))

    def close(self):
        self.closed = True


def fake_capture(target, log_path):
    """写一张上红下蓝的 8x8 JPEG；旋转 180° 后应变成上蓝下红。"""
    image = Image.new("RGB", (8, 8), (255, 0, 0))
    for y in range(4, 8):
        for x in range(8):
            image.putpixel((x, y), (0, 0, 255))
    image.save(target, quality=100)
    return {"size": [8, 8], "seconds": 0.01}


class Flow30Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.temp.name) / "rounds"
        self.inbox.mkdir()
        self.config = {"inbox": str(self.inbox), "gimbal": {}}
        self.timing = flow.Timing(move_s=0, settle_s=0, hold_s=0, return_s=0, steps=2)

    def tearDown(self):
        self.temp.cleanup()

    def _run(self, pulse=None, capture=fake_capture, **kwargs):
        pulse = pulse or FakePulse()
        folder = flow.run_flow30(
            self.config, pulse=pulse, capture=capture, analyze=False,
            timing=self.timing, sleep=lambda _seconds: None, log=lambda *_args: None,
            lock=lambda _path: nullcontext(),
            **kwargs)
        return folder, pulse

    def test_captures_four_views_and_returns_home(self):
        folder, pulse = self._run()
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "captured")
        self.assertEqual(meta["views_captured"], 4)
        self.assertEqual(len(meta["views"]), 4)
        self.assertTrue((folder / "READY.json").is_file())
        self.assertEqual([view["view_id"] for view in meta["views"]],
                         ["view_01", "view_02", "view_03", "view_04"])
        for view, target in zip(meta["views"], flow.PAN_TARGETS_US):
            self.assertTrue((folder / view["file"]).is_file())
            self.assertTrue((folder / view["raw_file"]).is_file())
            self.assertAlmostEqual(view["pose"]["commanded"]["pan_us"], target, delta=1.0)
            self.assertEqual(view["pose"]["source"], "commanded")
        self.assertAlmostEqual(pulse.values[flow.PAN_CHANNEL], flow.START_PAN_US, delta=1.0)
        self.assertAlmostEqual(pulse.values[flow.TILT_CHANNEL], flow.START_TILT_US, delta=1.0)
        self.assertEqual(meta["gimbal"]["returned"]["pan_us"], round(pulse.values[flow.PAN_CHANNEL], 1))

    def test_rotation_180_flips_image(self):
        folder, _ = self._run(rotate_deg=180)
        view = json.loads((folder / "meta.json").read_text(encoding="utf-8"))["views"][0]
        with Image.open(folder / view["raw_file"]) as raw:
            self.assertEqual(raw.size, (8, 8))
            raw_top = raw.getpixel((4, 1))
        with Image.open(folder / view["file"]) as rotated:
            self.assertEqual(rotated.size, (8, 8))
            rotated_top = rotated.getpixel((4, 1))
        self.assertGreater(raw_top[0], 150)          # raw 上红
        self.assertGreater(rotated_top[2], 150)      # 旋转后上蓝

    def test_rotate_zero_keeps_orientation(self):
        folder, _ = self._run(rotate_deg=0)
        view = json.loads((folder / "meta.json").read_text(encoding="utf-8"))["views"][0]
        with Image.open(folder / view["file"]) as image:
            top = image.getpixel((4, 1))
        self.assertGreater(top[0], 150)
        self.assertEqual(view["pose"]["rotation_deg"], 0)

    def test_dry_run_does_not_move_or_capture(self):
        pulse = FakePulse()
        folder = flow.run_flow30(
            self.config, pulse=pulse, capture=fake_capture, analyze=False,
            dry_run=True, timing=self.timing, sleep=lambda _seconds: None,
            log=lambda *_args: None)
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "dry_run")
        self.assertEqual(meta["views"], [])
        self.assertEqual(pulse.writes, [])
        self.assertFalse((folder / "READY.json").exists())

    def test_partial_capture_returns_home(self):
        pulse = FakePulse()
        calls = {"n": 0}

        def flaky_capture(target, log_path):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("相机超时")
            return fake_capture(target, log_path)

        with self.assertRaises(RuntimeError):
            self._run(pulse=pulse, capture=flaky_capture)
        self.assertAlmostEqual(pulse.values[flow.PAN_CHANNEL], flow.START_PAN_US, delta=1.0)
        self.assertAlmostEqual(pulse.values[flow.TILT_CHANNEL], flow.START_TILT_US, delta=1.0)
        folder = next(self.inbox.glob("round_*"))
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "capture_failed")
        self.assertEqual(len(meta["views"]), 2)
        self.assertFalse((folder / "READY.json").exists())

    def test_targets_are_clamped(self):
        self.assertEqual(flow.clamp_us(9999, 500, 2478), 2478)
        self.assertEqual(flow.clamp_us(0, 500, 2478), 500)
        self.assertAlmostEqual(flow.pan_deg(994.4), 66.7, delta=0.1)
        self.assertAlmostEqual(flow.tilt_deg(2055.7), 75.0, delta=0.2)


if __name__ == "__main__":
    unittest.main()
