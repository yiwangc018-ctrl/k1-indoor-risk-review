#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PC 侧照片处理最小链路的自测。

    python tests/test_photo_pipeline.py

全部素材在临时目录里用 Pillow 现场生成，不联网、不读 results、不需要开发板。
覆盖：画质门禁（亮度/过曝/尺寸/清晰度代理）、阈值配置、预处理（旋转/缩放/
JPEG/PNG/sha256/不覆盖原图）、average hash 去重、边界裁剪、坏图/缺文件/缺依赖
的清晰错误、JSON 序列化、可替换 loader/checker 协议。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

try:
    from PIL import Image, ImageDraw
except ImportError as exc:  # pragma: no cover - 本机已知装了 Pillow
    raise SystemExit("测试需要 Pillow：pip install Pillow") from exc

import photo_pipeline as pp  # noqa: E402


class PhotoPipelineTestCase(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = self._tempdir.name

    def tearDown(self):
        self._tempdir.cleanup()

    # -- 素材 ---------------------------------------------------------------
    def path(self, name):
        return os.path.join(self.tmp, name)

    def texture(self, width=640, height=480, dark=90, light=180, block=8):
        """确定性棋盘纹理：亮度适中、无过曝、梯度方差高。"""
        image = Image.new("RGB", (width, height), (dark, dark, dark))
        draw = ImageDraw.Draw(image)
        for y in range(0, height, block):
            for x in range(0, width, block):
                if ((x // block) + (y // block)) % 2:
                    draw.rectangle(
                        [x, y, min(x + block - 1, width - 1),
                         min(y + block - 1, height - 1)],
                        fill=(light, light, light),
                    )
        return image

    def save_image(self, name, image):
        target = self.path(name)
        image.save(target)
        return target

    def make_pass(self, name="pass.png"):
        return self.save_image(name, self.texture())

    def make_overexposed(self, name="over.png", fraction=0.40):
        image = self.texture()
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            [0, 0, int(640 * fraction) - 1, 479], fill=(255, 255, 255)
        )
        return self.save_image(name, image)

    @staticmethod
    def sha256(path):
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()


class TestQuality(PhotoPipelineTestCase):
    def test_pass_on_textured_mid_brightness(self):
        result = pp.quality_check(self.make_pass())
        self.assertEqual(result.status, pp.PASS)
        self.assertTrue(result.ok)
        self.assertEqual(result.reasons, ())
        self.assertEqual(result.metrics["width"], 640)
        self.assertEqual(result.metrics["height"], 480)
        self.assertAlmostEqual(result.metrics["mean_brightness"], 135.0, places=1)
        self.assertAlmostEqual(result.metrics["overexposed_ratio"], 0.0)
        self.assertGreater(result.metrics["gradient_variance"], 80.0)

    def test_sharpness_metric_is_gradient_variance_not_laplacian(self):
        result = pp.quality_check(self.make_pass())
        self.assertIn("gradient_variance", result.metrics)
        self.assertFalse(
            any("laplacian" in str(key).lower() for key in result.metrics)
        )

    def test_too_dark_is_rejected(self):
        image = Image.new("RGB", (640, 480), (10, 10, 10))
        result = pp.quality_check(image)
        self.assertEqual(result.status, pp.REJECT)
        self.assertTrue(result.is_rejected)
        self.assertTrue(
            any("brightness_below" in reason for reason in result.reasons),
            result.reasons,
        )

    def test_too_bright_is_rejected(self):
        image = Image.new("RGB", (640, 480), (255, 255, 255))
        result = pp.quality_check(image)
        self.assertEqual(result.status, pp.REJECT)
        self.assertTrue(
            any("overexposed" in reason for reason in result.reasons),
            result.reasons,
        )

    def test_overexposed_ratio_is_rejected(self):
        result = pp.quality_check(self.make_overexposed())
        self.assertEqual(result.status, pp.REJECT)
        self.assertAlmostEqual(result.metrics["overexposed_ratio"], 0.4, places=3)
        self.assertTrue(
            any("overexposed_ratio" in reason for reason in result.reasons),
            result.reasons,
        )

    def test_flat_image_has_low_gradient_variance(self):
        image = Image.new("RGB", (640, 480), (128, 128, 128))
        result = pp.quality_check(image)
        self.assertEqual(result.status, pp.REJECT)
        self.assertAlmostEqual(result.metrics["gradient_variance"], 0.0)
        self.assertTrue(
            any("gradient_variance" in reason for reason in result.reasons),
            result.reasons,
        )

    def test_small_dimensions_are_rejected(self):
        result = pp.quality_check(self.texture(width=120, height=90))
        self.assertEqual(result.status, pp.REJECT)
        self.assertTrue(
            any("width_below_min" in reason for reason in result.reasons),
            result.reasons,
        )
        self.assertTrue(
            any("height_below_min" in reason for reason in result.reasons),
            result.reasons,
        )

    def test_near_dark_target_is_retry_not_reject(self):
        result = pp.quality_check(self.texture(dark=0, light=60))
        self.assertEqual(result.status, pp.RETRY)
        self.assertTrue(result.needs_retry)
        self.assertAlmostEqual(result.metrics["mean_brightness"], 30.0, places=1)
        self.assertTrue(
            any("brightness_below_target" in reason for reason in result.reasons),
            result.reasons,
        )

    def test_dimensions_between_min_and_recommended_are_retry(self):
        result = pp.quality_check(self.texture(width=500, height=400))
        self.assertEqual(result.status, pp.RETRY)
        self.assertTrue(
            any("width_below_recommended" in reason for reason in result.reasons),
            result.reasons,
        )

    def test_threshold_mapping_can_relax_sharpness(self):
        image = Image.new("RGB", (640, 480), (128, 128, 128))
        result = pp.quality_check(
            image, thresholds={"min_gradient_variance": 0.0}
        )
        self.assertEqual(result.status, pp.PASS)
        self.assertEqual(result.thresholds["min_gradient_variance"], 0.0)

    def test_thresholds_dataclass_can_relax_overexposure(self):
        thresholds = pp.QualityThresholds().with_overrides(
            max_overexposed_ratio=0.5
        )
        result = pp.quality_check(self.make_overexposed(), thresholds=thresholds)
        self.assertEqual(result.status, pp.PASS)
        self.assertEqual(result.thresholds["max_overexposed_ratio"], 0.5)

    def test_unknown_threshold_key_is_rejected(self):
        with self.assertRaises(pp.QualityConfigError) as ctx:
            pp.quality_check(self.make_pass(), thresholds={"no_such_key": 1})
        self.assertIn("no_such_key", str(ctx.exception))

    def test_invalid_threshold_value_is_rejected(self):
        with self.assertRaises(pp.QualityConfigError):
            pp.quality_check(self.make_pass(), thresholds={"min_width": 0})

    def test_accepts_loaded_image_and_path(self):
        loaded = pp.load_image(self.make_pass())
        try:
            by_loaded = pp.quality_check(loaded)
        finally:
            loaded.close()
        by_path = pp.quality_check(self.make_pass())
        self.assertEqual(by_loaded.status, pp.PASS)
        self.assertEqual(by_path.status, pp.PASS)

    def test_non_image_is_rejected(self):
        with self.assertRaises(pp.QualityConfigError):
            pp.quality_check(object())


class TestLoadAndPreprocess(PhotoPipelineTestCase):
    def test_load_image_records_source_metadata(self):
        target = self.make_pass()
        loaded = pp.load_image(target)
        try:
            meta = loaded.meta
            self.assertEqual(meta.source_path, os.path.abspath(target))
            self.assertEqual((meta.width, meta.height), (640, 480))
            self.assertEqual((meta.source_width, meta.source_height), (640, 480))
            self.assertEqual(meta.format, "PNG")
            self.assertEqual(meta.mode, "RGB")
            self.assertEqual(meta.original_sha256, self.sha256(target))
            self.assertEqual(meta.source_file_size, os.path.getsize(target))
            self.assertIsNone(meta.processed_sha256)
            self.assertIsNone(meta.output_path)
        finally:
            loaded.close()

    def test_missing_file_raises_clear_error(self):
        missing = self.path("missing.png")
        with self.assertRaises(pp.ImageLoadError) as ctx:
            pp.load_image(missing)
        self.assertIn("不存在", str(ctx.exception))

    def test_bad_image_bytes_raise_clear_error(self):
        bad = self.path("bad.png")
        with open(bad, "wb") as handle:
            handle.write(b"this is not an image")
        with self.assertRaises(pp.ImageLoadError) as ctx:
            pp.load_image(bad)
        self.assertTrue(
            any(word in str(ctx.exception) for word in ("无法识别", "损坏", "解码")),
            str(ctx.exception),
        )

    def test_directory_is_not_loaded(self):
        with self.assertRaises(pp.ImageLoadError) as ctx:
            pp.load_image(self.tmp)
        self.assertIn("不是普通文件", str(ctx.exception))

    def test_empty_file_raises_clear_error(self):
        empty = self.path("empty.png")
        open(empty, "wb").close()
        with self.assertRaises(pp.ImageLoadError) as ctx:
            pp.load_image(empty)
        self.assertIn("为空", str(ctx.exception))

    def test_preprocess_preserves_source_and_records_hashes(self):
        source = self.make_pass()
        original_sha = self.sha256(source)
        output_dir = self.path("out")
        meta = pp.preprocess(source, output_dir)
        self.assertEqual(self.sha256(source), original_sha)
        self.assertEqual(meta.original_sha256, original_sha)
        self.assertNotEqual(meta.output_path, os.path.abspath(source))
        self.assertTrue(os.path.isfile(meta.output_path))
        self.assertEqual(meta.processed_sha256, self.sha256(meta.output_path))
        self.assertEqual((meta.width, meta.height), (640, 480))
        self.assertEqual(meta.rotation, 0.0)
        self.assertEqual(meta.scale, 1.0)
        self.assertEqual(meta.format, "JPEG")
        self.assertEqual(meta.output_file_size, os.path.getsize(meta.output_path))

    def test_preprocess_can_emit_png(self):
        meta = pp.preprocess(
            self.make_pass(), self.path("out"), image_format="PNG"
        )
        self.assertEqual(meta.format, "PNG")
        self.assertTrue(meta.output_path.endswith(".png"))
        self.assertEqual(meta.processed_sha256, self.sha256(meta.output_path))
        with Image.open(meta.output_path) as image:
            self.assertEqual(image.format, "PNG")

    def test_preprocess_rotates_then_scales(self):
        source = self.make_pass()
        meta = pp.preprocess(
            source, self.path("out"), rotate=90, max_side=320
        )
        self.assertEqual((meta.width, meta.height), (240, 320))
        self.assertEqual(meta.rotation, 90.0)
        self.assertEqual(meta.scale, 0.5)
        self.assertEqual((meta.source_width, meta.source_height), (640, 480))

    def test_preprocess_scale_only(self):
        meta = pp.preprocess(self.make_pass(), self.path("out"), scale=0.25)
        self.assertEqual((meta.width, meta.height), (160, 120))
        self.assertEqual(meta.scale, 0.25)

    def test_preprocess_rejects_unknown_format(self):
        with self.assertRaises(pp.PreprocessError):
            pp.preprocess(self.make_pass(), self.path("out"), image_format="GIF")

    def test_preprocess_rejects_scale_and_max_side_together(self):
        with self.assertRaises(pp.PreprocessError):
            pp.preprocess(
                self.make_pass(), self.path("out"), scale=0.5, max_side=320
            )

    def test_preprocess_never_overwrites_source_or_existing_output(self):
        source = self.make_pass()
        original_sha = self.sha256(source)
        first = pp.preprocess(source, self.tmp)
        second = pp.preprocess(source, self.tmp)
        self.assertEqual(self.sha256(source), original_sha)
        self.assertNotEqual(first.output_path, os.path.abspath(source))
        self.assertNotEqual(first.output_path, second.output_path)
        self.assertTrue(os.path.isfile(first.output_path))
        self.assertTrue(os.path.isfile(second.output_path))

    def test_missing_pil_is_a_readable_dependency_error(self):
        with mock.patch.object(
            pp,
            "_import_pil",
            side_effect=pp.DependencyError("缺少 Pillow (PIL) 测试桩"),
        ):
            with self.assertRaises(pp.DependencyError) as ctx:
                pp.load_image(self.make_pass())
        self.assertIn("Pillow", str(ctx.exception))

    def test_missing_numpy_is_a_readable_dependency_error(self):
        image = self.texture()
        with mock.patch.object(
            pp,
            "_import_numpy",
            side_effect=pp.DependencyError("缺少 numpy 测试桩"),
        ):
            with self.assertRaises(pp.DependencyError) as ctx:
                pp.quality_check(image)
        self.assertIn("numpy", str(ctx.exception))


class TestHashAndCrop(PhotoPipelineTestCase):
    def test_average_hash_is_deterministic_hex(self):
        source = self.make_pass()
        first = pp.average_hash(source)
        second = pp.average_hash(source)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)
        int(first, 16)

    def test_average_hash_changes_with_visual_content(self):
        checker = self.texture()
        left_right = Image.new("RGB", (640, 480), (60, 60, 60))
        draw = ImageDraw.Draw(left_right)
        draw.rectangle([320, 0, 639, 479], fill=(200, 200, 200))
        self.assertNotEqual(pp.average_hash(checker), pp.average_hash(left_right))

    def test_average_hash_is_resize_invariant_for_same_layout(self):
        image = Image.new("RGB", (640, 480), (60, 60, 60))
        draw = ImageDraw.Draw(image)
        draw.rectangle([320, 0, 639, 479], fill=(200, 200, 200))
        small = image.resize((320, 240), Image.LANCZOS)
        self.assertEqual(pp.average_hash(image), pp.average_hash(small))

    def test_hamming_distance_and_length_checks(self):
        self.assertEqual(
            pp.hamming_distance("0000000000000000", "0000000000000001"), 1
        )
        self.assertFalse(
            pp.is_duplicate("0000000000000000", "0000000000000001")
        )
        self.assertTrue(
            pp.is_duplicate("0000000000000000", "0000000000000001", threshold=1)
        )
        with self.assertRaises(ValueError):
            pp.hamming_distance("00", "0000")
        with self.assertRaises(ValueError):
            pp.hamming_distance("zz", "00")

    def test_average_hash_rejects_bad_size(self):
        with self.assertRaises(ValueError):
            pp.average_hash(self.texture(), hash_size=0)

    def test_crop_inside_bounds_records_boxes(self):
        source = self.make_pass()
        cropped, record = pp.crop_box(source, (10, 20, 110, 120))
        try:
            self.assertEqual(cropped.size, (100, 100))
        finally:
            cropped.close()
        self.assertEqual(record.source_box, (10, 20, 110, 120))
        self.assertEqual(record.crop_box, (10, 20, 110, 120))
        self.assertEqual(record.source_size, (640, 480))
        self.assertEqual(record.output_size, (100, 100))
        self.assertFalse(record.clamped)

    def test_crop_clips_left_and_top(self):
        cropped, record = pp.crop_box(self.make_pass(), (-20, -10, 100, 80))
        try:
            self.assertEqual(cropped.size, (100, 80))
        finally:
            cropped.close()
        self.assertEqual(record.source_box, (-20, -10, 100, 80))
        self.assertEqual(record.crop_box, (0, 0, 100, 80))
        self.assertTrue(record.clamped)

    def test_crop_clips_right_and_bottom(self):
        cropped, record = pp.crop_box(self.make_pass(), (600, 440, 700, 520))
        try:
            self.assertEqual(cropped.size, (40, 40))
        finally:
            cropped.close()
        self.assertEqual(record.crop_box, (600, 440, 640, 480))

    def test_crop_fully_outside_raises(self):
        with self.assertRaises(pp.CropError) as ctx:
            pp.crop_box(self.make_pass(), (700, 600, 800, 700))
        self.assertIn("无交集", str(ctx.exception))

    def test_crop_reversed_box_raises(self):
        with self.assertRaises(pp.CropError):
            pp.crop_box(self.make_pass(), (100, 100, 50, 50))

    def test_crop_non_integer_box_raises(self):
        with self.assertRaises(pp.CropError):
            pp.crop_box(self.make_pass(), (0.5, 0, 10, 10))

    def test_crop_accepts_loaded_image(self):
        loaded = pp.load_image(self.make_pass())
        try:
            cropped, record = pp.crop_box(loaded, (0, 0, 32, 24))
            cropped.close()
        finally:
            loaded.close()
        self.assertEqual(record.output_size, (32, 24))


class TestPipeline(PhotoPipelineTestCase):
    def test_single_pass_result_is_json_serializable(self):
        output_dir = self.path("out")
        results = pp.run_file_pipeline(
            [self.make_pass()],
            output_dir=output_dir,
            crop_boxes=[(0, 0, 100, 80)],
        )
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertTrue(result.ok)
        self.assertEqual(result.status, pp.PASS)
        self.assertIsNotNone(result.frame)
        self.assertTrue(os.path.isfile(result.frame.output_path))
        self.assertEqual(len(result.crops), 1)
        self.assertEqual(result.crops[0].output_size, (100, 80))

        payload = json.loads(pp.results_to_json(results))
        self.assertEqual(payload[0]["status"], "pass")
        self.assertEqual(payload[0]["crops"][0]["source_box"], [0, 0, 100, 80])
        self.assertIsNotNone(payload[0]["frame"]["processed_sha256"])
        self.assertEqual(json.loads(result.to_json())["source"], result.source)

    def test_duplicate_is_detected_within_one_run(self):
        first = self.make_pass("first.png")
        second = self.path("second.png")
        shutil.copyfile(first, second)
        results = pp.run_file_pipeline(
            [first, second], output_dir=self.path("out")
        )
        self.assertEqual([item.status for item in results], [pp.PASS, pp.DUPLICATE])
        self.assertEqual(results[1].duplicate_of, os.path.abspath(first))
        self.assertEqual(results[1].average_hash, results[0].average_hash)
        self.assertIsNone(results[1].frame.output_path)

    def test_dedup_threshold_is_configurable(self):
        source = self.make_pass()

        def run_with(threshold, out_name):
            hashes = iter(["0000000000000000", "0000000000000001"])

            def fake_hasher(_image):
                return next(hashes)

            return pp.run_file_pipeline(
                [source, source],
                output_dir=self.path(out_name),
                dedup_threshold=threshold,
                hasher=fake_hasher,
            )

        exact = run_with(0, "out0")
        tolerant = run_with(1, "out1")
        self.assertEqual([item.status for item in exact], [pp.PASS, pp.PASS])
        self.assertEqual(
            [item.status for item in tolerant], [pp.PASS, pp.DUPLICATE]
        )

    def test_reject_and_retry_skip_preprocessing(self):
        dark = self.save_image("dark.png", Image.new("RGB", (640, 480), (10, 10, 10)))
        retry = self.path("retry.png")
        self.texture(width=500, height=400).save(retry)
        output_dir = self.path("out")
        results = pp.run_file_pipeline([dark, retry], output_dir=output_dir)
        self.assertEqual([item.status for item in results], [pp.REJECT, pp.RETRY])
        self.assertTrue(all(item.ok for item in results))
        self.assertEqual(os.listdir(output_dir), [])
        self.assertIsNone(results[0].frame.output_path)

    def test_bad_and_missing_files_do_not_stop_batch(self):
        good = self.make_pass()
        bad = self.path("bad.png")
        with open(bad, "wb") as handle:
            handle.write(b"broken")
        missing = self.path("missing.png")
        results = pp.run_file_pipeline(
            [good, bad, missing], output_dir=self.path("out")
        )
        self.assertEqual(
            [item.status for item in results], [pp.PASS, pp.ERROR, pp.ERROR]
        )
        self.assertEqual([item.ok for item in results], [True, False, False])
        self.assertEqual(results[1].error_type, "ImageLoadError")
        self.assertEqual(results[2].error_type, "ImageLoadError")
        self.assertIn("处理失败", results[1].error)
        self.assertIn("不存在", results[2].error)
        payload = json.loads(pp.results_to_json(results))
        self.assertEqual(payload[0]["status"], "pass")
        self.assertEqual(payload[2]["status"], "error")

    def test_clipped_crop_records_are_returned(self):
        results = pp.run_file_pipeline(
            [self.make_pass()],
            output_dir=self.path("out"),
            crop_boxes=[(-10, -10, 100, 80), (600, 440, 700, 520)],
        )
        result = results[0]
        self.assertEqual(result.status, pp.PASS)
        self.assertEqual(len(result.crops), 2)
        self.assertEqual(result.crops[0].source_box, (-10, -10, 100, 80))
        self.assertEqual(result.crops[0].crop_box, (0, 0, 100, 80))
        self.assertTrue(result.crops[0].clamped)
        self.assertEqual(result.crops[1].crop_box, (600, 440, 640, 480))
        payload = json.loads(pp.results_to_json(result))
        self.assertEqual(payload[0]["crops"][1]["crop_box"], [600, 440, 640, 480])

    def test_custom_loader_and_checker_are_replaceable(self):
        loaded = pp.load_image(self.make_pass())
        try:
            def fake_loader(_path):
                return loaded

            def fake_checker(_image, thresholds=None):
                return pp.QualityResult(
                    status=pp.REJECT,
                    metrics={"width": 1, "height": 1},
                    reasons=("reject:fake",),
                )

            results = pp.run_file_pipeline(
                [self.path("ignored.png")],
                output_dir=self.path("out"),
                loader=fake_loader,
                checker=fake_checker,
            )
        finally:
            loaded.close()
        self.assertEqual(results[0].status, pp.REJECT)
        self.assertEqual(results[0].quality.reasons, ("reject:fake",))
        self.assertEqual(os.listdir(self.path("out")), [])

    def test_custom_loader_failure_becomes_error_result(self):
        def broken_loader(_path):
            raise RuntimeError("loader boom")

        results = pp.run_file_pipeline(
            [self.make_pass()],
            output_dir=self.path("out"),
            loader=broken_loader,
        )
        self.assertEqual(results[0].status, pp.ERROR)
        self.assertEqual(results[0].error_type, "RuntimeError")
        self.assertIn("loader boom", results[0].error)

    def test_failed_preprocess_does_not_poison_dedup_index(self):
        source = self.make_pass()
        calls = {"count": 0}

        def flaky_preprocessor(loaded, output_dir, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise pp.PreprocessError("模拟第一次写盘失败")
            return pp.preprocess(loaded, output_dir, **kwargs)

        results = pp.run_file_pipeline(
            [source, source],
            output_dir=self.path("out"),
            preprocessor=flaky_preprocessor,
        )
        self.assertEqual(results[0].status, pp.ERROR)
        self.assertEqual(results[1].status, pp.PASS)
        self.assertIsNone(results[1].duplicate_of)

    def test_missing_pil_in_pipeline_becomes_error_not_crash(self):
        with mock.patch.object(
            pp,
            "_import_pil",
            side_effect=pp.DependencyError("缺少 Pillow (PIL) 测试桩"),
        ):
            results = pp.run_file_pipeline(
                [self.make_pass()], output_dir=self.path("out")
            )
        self.assertEqual(results[0].status, pp.ERROR)
        self.assertEqual(results[0].error_type, "DependencyError")
        self.assertIn("Pillow", results[0].error)

    def test_output_stays_in_output_dir_and_source_dir_untouched(self):
        source_dir = self.path("src")
        os.makedirs(source_dir)
        source = os.path.join(source_dir, "photo.png")
        self.texture().save(source)
        output_dir = self.path("out")
        pp.run_file_pipeline([source], output_dir=output_dir)
        self.assertEqual(os.listdir(source_dir), ["photo.png"])
        self.assertEqual(len(os.listdir(output_dir)), 1)

    def test_empty_source_list_returns_empty(self):
        self.assertEqual(pp.run_file_pipeline([]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
