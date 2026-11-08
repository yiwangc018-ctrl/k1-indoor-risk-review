"""SenseVoice 前端与解码的纯函数测试：不需要 onnx 模型、不上板。"""
import math
from pathlib import Path
import sys
import tempfile
import unittest
import wave

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import asr_sensevoice as sv


def write_wav(path, samples, rate=16000, channels=1, width=2):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(samples)


class FrontendTests(unittest.TestCase):
    def test_frame_count_matches_kaldi_snip_edges(self):
        wave_ = np.zeros(16000, dtype=np.float32)  # 1 s
        feature = sv.compute_fbank(wave_)
        self.assertEqual(feature.shape, (1 + (16000 - 400) // 160, 80))
        self.assertEqual(feature.dtype, np.float32)

    def test_short_audio_yields_no_frames(self):
        self.assertEqual(sv.compute_fbank(np.zeros(100, dtype=np.float32)).shape, (0, 80))

    def test_lfr_uses_7_by_6_and_560_dims(self):
        feature = sv.compute_fbank(np.zeros(16000, dtype=np.float32))
        lfr = sv.apply_lfr(feature)
        self.assertEqual(lfr.shape[1], 560)
        self.assertEqual(lfr.shape[0], math.ceil(feature.shape[0] / 6))

    def test_silence_is_finite_and_floored(self):
        feature = sv.compute_fbank(np.zeros(8000, dtype=np.float32))
        self.assertTrue(np.isfinite(feature).all())
        self.assertTrue((feature <= math.log(sv.FLOAT_EPS) + 1e-3).all())

    def test_tone_peaks_in_a_middle_mel_band(self):
        t = np.arange(8000) / 16000.0
        tone = (0.5 * np.sin(2 * math.pi * 1000 * t)).astype(np.float32)
        feature = sv.compute_fbank(tone)
        mean_by_band = feature.mean(axis=0)
        self.assertGreater(mean_by_band.argmax(), 5)
        self.assertLess(mean_by_band.argmax(), 75)


class DecodeTests(unittest.TestCase):
    def test_ctc_collapses_repeats_and_blank(self):
        logits = np.zeros((6, 5), dtype=np.float32)
        for frame, token in enumerate([1, 1, 0, 2, 2, 3]):
            logits[frame, token] = 1.0
        self.assertEqual(sv.ctc_greedy(logits, 6), [1, 2, 3])

    def test_token_table_decodes_and_strips_special_tokens(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tokens.txt"
            path.write_text("▁开\t0\n检查\t1\n<|zh|>\t0\n<|withitn|>\t0\n", encoding="utf-8")
            table = sv.TokenTable(path)
            self.assertEqual(len(table), 4)
            self.assertEqual(table.decode([0, 1]), "开检查")
            self.assertEqual(table.decode([2, 3]), "")
            self.assertEqual(table.decode([99]), "")


class WavReadTests(unittest.TestCase):
    def test_rejects_wrong_sample_rate(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.wav"
            write_wav(path, b"\x00\x00" * 800, rate=8000)
            with self.assertRaisesRegex(ValueError, "16000"):
                sv.read_wav(path)

    def test_rejects_stereo(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "stereo.wav"
            write_wav(path, b"\x00\x00" * 1600, channels=2)
            with self.assertRaisesRegex(ValueError, "单声道"):
                sv.read_wav(path)

    def test_reads_16k_mono(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ok.wav"
            write_wav(path, b"\x10\x00" * 1600)  # little-endian int16 = 16
            audio = sv.read_wav(path)
            self.assertEqual(audio.shape, (1600,))
            self.assertAlmostEqual(float(audio[0]), 16 / 32768.0, places=6)


if __name__ == "__main__":
    unittest.main()
