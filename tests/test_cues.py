"""提示音测试：只生成“嘀嘀”；listen.wav 是部署的语音提示，不由脚本覆盖。"""
from pathlib import Path
import sys
import tempfile
import unittest
import wave

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/board"))
import make_cues


class CueTests(unittest.TestCase):
    def test_only_start_is_generated(self):
        self.assertEqual(sorted(make_cues.CUE_SPECS), ["start"])

    def test_build_does_not_overwrite_listen_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            prompt = Path(temp) / "listen.wav"
            prompt.write_bytes(b"spoken prompt")
            make_cues.build(temp)
            self.assertEqual(prompt.read_bytes(), b"spoken prompt")
            self.assertFalse((Path(temp) / "listen_beep.wav").exists())

    def test_start_is_two_beeps_at_the_same_pitch(self):
        segments = make_cues.CUE_SPECS["start"]
        tones = [frequency for frequency, _ in segments if frequency > 0]
        self.assertEqual(len(tones), 2)
        self.assertEqual(tones[0], tones[1])
        self.assertLessEqual(sum(milliseconds for _, milliseconds in segments), 400)

    def test_build_writes_valid_16bit_mono_wavs(self):
        with tempfile.TemporaryDirectory() as temp:
            written = make_cues.build(temp)
            self.assertEqual(sorted(name for name, _, _ in written), ["start"])
            for name in ("start",):
                with wave.open(str(Path(temp) / f"{name}.wav")) as handle:
                    self.assertEqual(handle.getframerate(), make_cues.SAMPLE_RATE)
                    self.assertEqual(handle.getnchannels(), 1)
                    self.assertEqual(handle.getsampwidth(), 2)
                    duration = handle.getnframes() / handle.getframerate()
                self.assertLessEqual(duration, 0.4, name)

    def test_waveform_is_bounded_and_click_free(self):
        for name in make_cues.CUE_SPECS:
            waveform = make_cues.render(make_cues.CUE_SPECS[name])
            self.assertTrue(np.isfinite(waveform).all())
            self.assertLessEqual(float(np.abs(waveform).max()), make_cues.AMPLITUDE + 1e-3)
            self.assertLess(abs(float(waveform[0])), 1e-3)   # 淡入，避免爆音
            self.assertLess(abs(float(waveform[-1])), 1e-3)  # 淡出


if __name__ == "__main__":
    unittest.main()
