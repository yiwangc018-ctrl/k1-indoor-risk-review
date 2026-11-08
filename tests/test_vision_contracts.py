#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""app/vision_contracts.py 的契约测试。

    python tests/test_vision_contracts.py

只测结构、校验、序列化和文本上下文；不加载 YOLO、不读图片、不联网、
不写 results/。所有素材都是内存里的 dataclass 值。
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from dataclasses import FrozenInstanceError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import vision_contracts as vc  # noqa: E402


class ContractTestCase(unittest.TestCase):
    def detection(self, **overrides):
        values = {
            "box": (10, 20, 110, 120),
            "class_name": "clutter",
            "confidence": 0.62,
            "source": vc.SOURCE_DETECTOR,
            "image_id": "img-1",
        }
        values.update(overrides)
        return vc.Detection(**values)

    def detection_set(self, **overrides):
        values = {
            "image_id": "img-1",
            "model": "yolov8n",
            "provider": "spacemit-ort",
            "detections": (self.detection(),),
            "latency_ms": 12.5,
            "errors": (),
        }
        values.update(overrides)
        return vc.DetectionSet(**values)

    def scene_fact(self, **overrides):
        values = {
            "category": "clutter",
            "status": vc.PRESENT,
            "score": 0.62,
            "evidence_image": "img-1",
            "evidence_box": (10, 20, 110, 120),
            "needs_review": False,
        }
        values.update(overrides)
        return vc.SceneFact(**values)

    def decision(self, **overrides):
        values = {
            "level": vc.MEDIUM,
            "items": (self.scene_fact(),),
            "degraded": False,
            "provenance": {"model": "yolov8n", "provider": "spacemit-ort"},
        }
        values.update(overrides)
        return vc.Decision(**values)


class TestDetection(ContractTestCase):
    def test_to_dict_has_exact_fields_and_json_safe_values(self):
        item = self.detection()
        payload = item.to_dict()
        self.assertEqual(
            list(payload),
            ["box", "class_name", "confidence", "source", "image_id"],
        )
        self.assertEqual(payload["box"], [10, 20, 110, 120])
        self.assertIsInstance(payload["box"][0], float)
        self.assertEqual(payload["class_name"], "clutter")
        self.assertEqual(payload["confidence"], 0.62)
        self.assertEqual(payload["source"], "detector")
        self.assertEqual(payload["image_id"], "img-1")
        self.assertEqual(
            json.loads(item.to_json())["class_name"], "clutter"
        )

    def test_box_accepts_list_and_normalises_to_float_tuple(self):
        item = self.detection(box=[0.1, 0.2, 0.5, 0.6])
        self.assertEqual(item.box, (0.1, 0.2, 0.5, 0.6))
        self.assertTrue(all(isinstance(value, float) for value in item.box))

    def test_json_round_trip_preserves_object(self):
        item = self.detection()
        restored = vc.Detection.from_json(item.to_json())
        self.assertEqual(restored, item)
        self.assertEqual(restored.to_dict(), item.to_dict())

    def test_box_must_have_exactly_four_values(self):
        for box in ((), (1, 2, 3), (1, 2, 3, 4, 5)):
            with self.subTest(box=box):
                with self.assertRaises(vc.VisionContractError):
                    self.detection(box=box)

    def test_box_values_must_be_numbers_not_bool_or_string(self):
        for box in (
            (0, 0, "10", 10),
            (0, 0, None, 10),
            (0, 0, True, 10),
            "0,0,10,10",
        ):
            with self.subTest(box=box):
                with self.assertRaises(vc.VisionContractError):
                    self.detection(box=box)

    def test_box_must_be_finite(self):
        for box in (
            (0, 0, float("nan"), 10),
            (0, 0, 10, float("inf")),
            (0, 0, 10, float("-inf")),
        ):
            with self.subTest(box=box):
                with self.assertRaises(vc.VisionContractError):
                    self.detection(box=box)

    def test_box_rejects_negative_or_reversed_coordinates(self):
        for box in (
            (-1, 0, 10, 10),
            (0, -1, 10, 10),
            (10, 0, 0, 10),
            (0, 10, 10, 0),
            (0, 0, 0, 10),
        ):
            with self.subTest(box=box):
                with self.assertRaises(vc.VisionContractError):
                    self.detection(box=box)

    def test_confidence_must_be_in_unit_interval(self):
        for value in (-0.01, 1.01, 2, True, "0.5", float("nan")):
            with self.subTest(confidence=value):
                with self.assertRaises(vc.VisionContractError):
                    self.detection(confidence=value)

    def test_confidence_boundaries_are_accepted(self):
        self.assertEqual(self.detection(confidence=0).confidence, 0.0)
        self.assertEqual(self.detection(confidence=1).confidence, 1.0)

    def test_text_fields_cannot_be_empty(self):
        for field_name in ("class_name", "source", "image_id"):
            with self.subTest(field=field_name):
                with self.assertRaises(vc.VisionContractError):
                    self.detection(**{field_name: "   "})
                with self.assertRaises(vc.VisionContractError):
                    self.detection(**{field_name: 123})

    def test_from_dict_rejects_unknown_fields(self):
        payload = self.detection().to_dict()
        payload["bbox"] = payload["box"]
        with self.assertRaises(vc.VisionContractError) as ctx:
            vc.Detection.from_dict(payload)
        self.assertIn("bbox", str(ctx.exception))

    def test_from_dict_rejects_missing_required_fields(self):
        payload = self.detection().to_dict()
        del payload["class_name"]
        with self.assertRaises(vc.VisionContractError) as ctx:
            vc.Detection.from_dict(payload)
        self.assertIn("class_name", str(ctx.exception))

    def test_from_dict_requires_mapping(self):
        with self.assertRaises(vc.VisionContractError):
            vc.Detection.from_dict([1, 2, 3, 4])

    def test_from_json_rejects_broken_json(self):
        with self.assertRaises(vc.VisionContractError):
            vc.Detection.from_json("{not json")

    def test_detection_is_frozen(self):
        item = self.detection()
        with self.assertRaises(FrozenInstanceError):
            item.confidence = 0.9


class TestDetectionSet(ContractTestCase):
    def test_to_dict_has_exact_fields_and_counts(self):
        item = self.detection_set()
        payload = item.to_dict()
        self.assertEqual(
            list(payload),
            [
                "image_id",
                "model",
                "provider",
                "detections",
                "latency_ms",
                "errors",
            ],
        )
        self.assertEqual(payload["detections"][0]["class_name"], "clutter")
        self.assertEqual(payload["latency_ms"], 12.5)
        self.assertEqual(payload["errors"], [])
        self.assertEqual(item.detection_count, 1)
        self.assertTrue(item.ok)
        self.assertFalse(item.degraded)

    def test_empty_detections_with_error_is_degraded_not_ok(self):
        item = vc.DetectionSet(
            image_id="img-1",
            model="null",
            provider="null",
            detections=(),
            latency_ms=0.0,
            errors=(vc.DETECTOR_NOT_CONFIGURED,),
        )
        self.assertEqual(item.detections, ())
        self.assertTrue(item.degraded)
        self.assertFalse(item.ok)
        self.assertEqual(item.error_codes, (vc.DETECTOR_NOT_CONFIGURED,))
        self.assertEqual(
            item.to_dict()["errors"], [vc.DETECTOR_NOT_CONFIGURED]
        )

    def test_json_round_trip(self):
        item = self.detection_set()
        restored = vc.DetectionSet.from_json(item.to_json())
        self.assertEqual(restored, item)
        self.assertEqual(restored.to_dict(), item.to_dict())

    def test_latency_must_be_non_negative_and_finite(self):
        for value in (-0.1, True, "1.0", float("nan"), float("inf")):
            with self.subTest(latency=value):
                with self.assertRaises(vc.VisionContractError):
                    self.detection_set(latency_ms=value)

    def test_errors_must_be_strings(self):
        for value in ("DETECTOR_NOT_CONFIGURED", [1], [""], None):
            with self.subTest(errors=value):
                with self.assertRaises(vc.VisionContractError):
                    self.detection_set(errors=value)

    def test_detections_must_be_detection_instances(self):
        for value in (
            [self.detection().to_dict()],
            {"box": [0, 0, 1, 1]},
            "not-a-list",
            None,
        ):
            with self.subTest(detections=value):
                with self.assertRaises(vc.VisionContractError):
                    self.detection_set(detections=value)

    def test_from_dict_rejects_unknown_fields(self):
        payload = self.detection_set().to_dict()
        payload["timing_ms"] = 12.5
        with self.assertRaises(vc.VisionContractError):
            vc.DetectionSet.from_dict(payload)

    def test_from_dict_rejects_missing_identity_fields(self):
        for field_name in ("image_id", "model", "provider"):
            with self.subTest(field=field_name):
                payload = self.detection_set().to_dict()
                del payload[field_name]
                with self.assertRaises(vc.VisionContractError):
                    vc.DetectionSet.from_dict(payload)

    def test_nested_unknown_detection_field_is_rejected(self):
        payload = self.detection_set().to_dict()
        payload["detections"][0]["bbox"] = payload["detections"][0]["box"]
        with self.assertRaises(vc.VisionContractError):
            vc.DetectionSet.from_dict(payload)


class TestSceneFact(ContractTestCase):
    def test_to_dict_and_round_trip_with_evidence(self):
        fact = self.scene_fact(needs_review=True)
        payload = fact.to_dict()
        self.assertEqual(
            list(payload),
            [
                "category",
                "status",
                "score",
                "evidence_image",
                "evidence_box",
                "needs_review",
            ],
        )
        self.assertEqual(payload["status"], vc.PRESENT)
        self.assertEqual(payload["evidence_box"], [10, 20, 110, 120])
        self.assertTrue(payload["needs_review"])
        self.assertEqual(vc.SceneFact.from_json(fact.to_json()), fact)

    def test_evidence_is_optional(self):
        fact = vc.SceneFact(category="grab_bar", status=vc.UNKNOWN, score=0.0)
        self.assertIsNone(fact.evidence_image)
        self.assertIsNone(fact.evidence_box)
        self.assertFalse(fact.needs_review)
        self.assertFalse(fact.has_evidence)
        self.assertEqual(fact.to_dict()["evidence_box"], None)

    def test_status_must_be_enum(self):
        for value in ("present_or_absent", "PRESENT", "", None):
            with self.subTest(status=value):
                with self.assertRaises(vc.VisionContractError):
                    self.scene_fact(status=value)

    def test_all_status_values_are_accepted(self):
        for value in vc.SCENE_STATUSES:
            with self.subTest(status=value):
                self.assertEqual(self.scene_fact(status=value).status, value)

    def test_score_must_be_in_unit_interval(self):
        for value in (-0.1, 1.1, True, "0.5", float("nan")):
            with self.subTest(score=value):
                with self.assertRaises(vc.VisionContractError):
                    self.scene_fact(score=value)

    def test_evidence_box_uses_same_box_validation(self):
        for value in ((0, 0, 10), (10, 0, 0, 10), (0, 0, "10", 10)):
            with self.subTest(box=value):
                with self.assertRaises(vc.VisionContractError):
                    self.scene_fact(evidence_box=value)

    def test_evidence_image_must_be_non_empty_when_present(self):
        with self.assertRaises(vc.VisionContractError):
            self.scene_fact(evidence_image="")
        with self.assertRaises(vc.VisionContractError):
            self.scene_fact(evidence_image=123)

    def test_needs_review_must_be_bool(self):
        for value in (1, 0, "yes", None):
            with self.subTest(value=value):
                with self.assertRaises(vc.VisionContractError):
                    self.scene_fact(needs_review=value)

    def test_from_dict_rejects_unknown_and_missing_fields(self):
        payload = self.scene_fact().to_dict()
        payload["confidence"] = payload["score"]
        with self.assertRaises(vc.VisionContractError):
            vc.SceneFact.from_dict(payload)
        for field_name in ("category", "status", "score"):
            with self.subTest(field=field_name):
                data = self.scene_fact().to_dict()
                del data[field_name]
                with self.assertRaises(vc.VisionContractError):
                    vc.SceneFact.from_dict(data)


class TestDecision(ContractTestCase):
    def test_to_dict_and_round_trip(self):
        decision = self.decision(
            items=(
                self.scene_fact(),
                self.scene_fact(
                    category="grab_bar",
                    status=vc.UNKNOWN,
                    score=0.0,
                    evidence_image=None,
                    evidence_box=None,
                    needs_review=True,
                ),
            ),
            provenance={
                "model": "yolov8n",
                "provider": "spacemit-ort",
                "errors": [vc.DETECTOR_NOT_CONFIGURED],
                "nested": {"threshold": 0.7},
            },
        )
        payload = decision.to_dict()
        self.assertEqual(
            list(payload), ["level", "items", "degraded", "provenance"]
        )
        self.assertEqual(payload["level"], vc.MEDIUM)
        self.assertEqual(len(payload["items"]), 2)
        self.assertFalse(payload["degraded"])
        self.assertEqual(payload["provenance"]["nested"]["threshold"], 0.7)
        self.assertEqual(vc.Decision.from_json(decision.to_json()), decision)

    def test_level_must_be_enum(self):
        for value in ("critical", "HIGH", "", None, 3):
            with self.subTest(level=value):
                with self.assertRaises(vc.VisionContractError):
                    self.decision(level=value)

    def test_all_levels_are_accepted(self):
        for value in vc.DECISION_LEVELS:
            with self.subTest(level=value):
                self.assertEqual(self.decision(level=value).level, value)

    def test_items_must_be_scene_fact_instances(self):
        for value in (
            [self.scene_fact().to_dict()],
            ["clutter"],
            "not-a-list",
            None,
        ):
            with self.subTest(items=value):
                with self.assertRaises(vc.VisionContractError):
                    self.decision(items=value)

    def test_from_dict_builds_nested_scene_facts(self):
        payload = self.decision().to_dict()
        restored = vc.Decision.from_dict(payload)
        self.assertIsInstance(restored.items[0], vc.SceneFact)
        self.assertEqual(restored, self.decision())

    def test_from_dict_rejects_nested_unknown_fact_field(self):
        payload = self.decision().to_dict()
        payload["items"][0]["confidence"] = payload["items"][0]["score"]
        with self.assertRaises(vc.VisionContractError):
            vc.Decision.from_dict(payload)

    def test_degraded_must_be_bool(self):
        for value in (1, 0, "yes", None):
            with self.subTest(value=value):
                with self.assertRaises(vc.VisionContractError):
                    self.decision(degraded=value)

    def test_provenance_must_be_json_object(self):
        for value in ([], "detector", 1, None):
            with self.subTest(provenance=value):
                with self.assertRaises(vc.VisionContractError):
                    self.decision(provenance=value)

    def test_provenance_rejects_non_json_values(self):
        for value in ({1: "x"}, {"image_bytes": b"raw"}, {"x": object()}):
            with self.subTest(provenance=value):
                with self.assertRaises(vc.VisionContractError):
                    self.decision(provenance=value)

    def test_provenance_is_copied_and_normalised(self):
        source = {"scores": [0.1, 0.2], "ok": True}
        decision = self.decision(provenance=source)
        self.assertEqual(decision.provenance["scores"], [0.1, 0.2])
        source["scores"].append(0.3)
        self.assertEqual(decision.provenance["scores"], [0.1, 0.2])

    def test_derived_review_fields(self):
        decision = self.decision(
            items=(
                self.scene_fact(needs_review=True),
                self.scene_fact(category="grab_bar", status=vc.UNKNOWN, score=0.0),
            )
        )
        self.assertTrue(decision.needs_review)
        self.assertEqual(decision.unknown_categories, ("grab_bar",))

    def test_from_dict_rejects_unknown_and_missing_level(self):
        payload = self.decision().to_dict()
        payload["overall"] = {"level": vc.MEDIUM}
        with self.assertRaises(vc.VisionContractError):
            vc.Decision.from_dict(payload)
        data = self.decision().to_dict()
        del data["level"]
        with self.assertRaises(vc.VisionContractError):
            vc.Decision.from_dict(data)


class TestNullDetector(ContractTestCase):
    def test_returns_empty_detections_and_error_code(self):
        detector = vc.NullDetector()
        result = detector.detect(object(), "img-1", {"room": "bathroom"})
        self.assertIsInstance(result, vc.DetectionSet)
        self.assertEqual(result.detections, ())
        self.assertEqual(result.detection_count, 0)
        self.assertEqual(result.errors, (vc.DETECTOR_NOT_CONFIGURED,))
        self.assertEqual(result.error_codes, (vc.DETECTOR_NOT_CONFIGURED,))
        self.assertTrue(result.degraded)
        self.assertFalse(result.ok)
        self.assertEqual(result.image_id, "img-1")
        self.assertEqual(result.model, "null")
        self.assertEqual(result.provider, "null")
        self.assertEqual(result.latency_ms, 0.0)

    def test_never_fabricates_detections(self):
        result = vc.NullDetector().detect(
            object(), "img-1", {"hint": "there should be a person"}
        )
        self.assertEqual(result.to_dict()["detections"], [])
        self.assertNotIn("confidence", json.dumps(result.to_dict()))

    def test_implements_detector_protocol(self):
        detector = vc.NullDetector()
        self.assertIsInstance(detector, vc.Detector)
        self.assertFalse(detector.configured)

        class FakeDetector:
            def detect(self, image, image_id, metadata=None):
                return vc.DetectionSet(
                    image_id=image_id,
                    model="fake",
                    provider="test",
                    detections=(),
                    latency_ms=0.0,
                    errors=(),
                )

        self.assertIsInstance(FakeDetector(), vc.Detector)

    def test_missing_image_id_becomes_explicit_unknown(self):
        result = vc.NullDetector().detect()
        self.assertEqual(result.image_id, "unknown")
        self.assertEqual(result.errors, (vc.DETECTOR_NOT_CONFIGURED,))


class TestTextContext(ContractTestCase):
    def test_returns_text_json_without_image_fields(self):
        context = vc.build_text_context(
            self.detection_set(),
            {"room": "bathroom", "pose": {"pan_deg": -60.0}},
        )
        self.assertIsInstance(context, str)
        payload = json.loads(context)
        self.assertEqual(payload["schema"], vc.TEXT_CONTEXT_SCHEMA)
        self.assertEqual(payload["modality"], "text")
        self.assertIs(payload["contains_image"], False)
        self.assertNotIn("image", payload)
        self.assertNotIn("image_path", payload)
        self.assertNotIn("image_base64", context)
        self.assertNotIn("data:image/", context)
        self.assertEqual(payload["image_id"], "img-1")
        self.assertEqual(payload["model"], "yolov8n")
        self.assertEqual(payload["provider"], "spacemit-ort")
        self.assertEqual(payload["detection_count"], 1)
        self.assertEqual(payload["metadata"]["room"], "bathroom")
        self.assertFalse(payload["degraded"])
        self.assertEqual(payload["errors"], [])

    def test_detection_fields_are_preserved(self):
        payload = json.loads(
            vc.build_text_context(self.detection_set(), {"room": "bathroom"})
        )
        detection = payload["detections"][0]
        self.assertEqual(detection["box"], [10, 20, 110, 120])
        self.assertEqual(detection["class_name"], "clutter")
        self.assertEqual(detection["confidence"], 0.62)
        self.assertEqual(detection["source"], "detector")
        self.assertEqual(detection["image_id"], "img-1")

    def test_null_detector_context_keeps_error_and_degraded(self):
        result = vc.NullDetector().detect(object(), "img-1")
        payload = json.loads(vc.build_text_context(result, {"room": "bathroom"}))
        self.assertEqual(payload["detections"], [])
        self.assertEqual(payload["errors"], [vc.DETECTOR_NOT_CONFIGURED])
        self.assertTrue(payload["degraded"])
        self.assertTrue(
            any("DETECTOR_NOT_CONFIGURED" in item for item in payload["warnings"])
        )

    def test_empty_detection_without_error_still_warns(self):
        empty = vc.DetectionSet(
            image_id="img-1",
            model="yolov8n",
            provider="spacemit-ort",
            detections=(),
            latency_ms=3.0,
            errors=(),
        )
        payload = json.loads(vc.build_text_context(empty))
        self.assertFalse(payload["degraded"])
        self.assertEqual(payload["detections"], [])
        self.assertTrue(payload["warnings"])
        self.assertIn("未看到", payload["warnings"][0])

    def test_rejects_image_metadata_at_any_depth(self):
        for metadata in (
            {"image": "photo.jpg"},
            {"image_path": "photo.jpg"},
            {"image_base64": "AAAA"},
            {"nested": {"image_bytes": "AAAA"}},
            {"items": [{"pixels": [1, 2, 3]}]},
        ):
            with self.subTest(metadata=metadata):
                with self.assertRaises(vc.VisionContractError):
                    vc.build_text_context(self.detection_set(), metadata)

    def test_rejects_inline_data_uri(self):
        with self.assertRaises(vc.VisionContractError):
            vc.build_text_context(
                self.detection_set(),
                {"note": "data:image/png;base64,AAAA"},
            )

    def test_metadata_must_be_mapping(self):
        for metadata in ([], "room", 3):
            with self.subTest(metadata=metadata):
                with self.assertRaises(vc.VisionContractError):
                    vc.build_text_context(self.detection_set(), metadata)

    def test_metadata_must_be_json_serialisable(self):
        for metadata in (
            {"blob": b"raw"},
            {"value": float("nan")},
            {1: "x"},
            {"value": object()},
        ):
            with self.subTest(metadata=metadata):
                with self.assertRaises(vc.VisionContractError):
                    vc.build_text_context(self.detection_set(), metadata)

    def test_accepts_sequence_of_detections(self):
        second = self.detection(
            class_name="person", image_id="img-1", box=(20, 30, 40, 60)
        )
        payload = json.loads(
            vc.build_text_context(
                [self.detection(), second],
                {"image_id": "img-1", "room": "bathroom"},
            )
        )
        self.assertEqual(payload["image_id"], "img-1")
        self.assertEqual(payload["detection_count"], 2)
        self.assertEqual(
            [item["class_name"] for item in payload["detections"]],
            ["clutter", "person"],
        )

    def test_sequence_rejects_non_detection_items(self):
        for value in ("not-a-list", [{"class_name": "clutter"}], [object()]):
            with self.subTest(value=value):
                with self.assertRaises(vc.VisionContractError):
                    vc.build_text_context(value)

    def test_context_is_json_round_trippable(self):
        context = vc.build_text_context(self.detection_set(), {"room": "bathroom"})
        first = json.loads(context)
        second = json.loads(json.dumps(first, ensure_ascii=False))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main(verbosity=2)
