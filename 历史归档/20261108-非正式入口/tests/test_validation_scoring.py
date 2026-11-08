"""Guard against overstating model quality through parsing or abstentions."""
import importlib.util
from pathlib import Path
import unittest
import json
import tempfile

spec = importlib.util.spec_from_file_location("score_presence",
    Path(__file__).resolve().parents[1] / "scripts/validation/score_presence.py")
scoring = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scoring)


class ScoringTests(unittest.TestCase):
    def test_out_of_domain_summary_excludes_indoor_facts(self):
        labels = {"q01": {"toilet": "present", "table": "absent"},
                  "q13": {"toilet": "absent", "table": "absent"}}
        rows = [{"image_id": qid, "variant": "clean_json", "elapsed_seconds": 1,
                 "text": '{"toilet":"unknown","table":"unknown"}'} for qid in labels]
        with tempfile.TemporaryDirectory() as folder:
            phase = Path(folder) / "phase"
            phase.mkdir()
            (phase / "responses.jsonl").write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
            summary = scoring.summarize(Path(folder), labels)["phase/clean_json"]
        self.assertEqual(summary["out_of_domain_policy"]["facts"], 2)
        self.assertEqual(summary["out_of_domain_policy"]["correct"], 2)
        self.assertEqual(summary["presence_with_out_of_domain_policy"]["facts"], 4)

    def test_extra_fields_fail_declared_schema_without_erasing_facts(self):
        text = '{"toilet":"absent","table":"present","risk":"safe"}'
        self.assertFalse(scoring.unrepaired_schema_valid(text))
        self.assertEqual(scoring.parse_response(text)[0], {"toilet": "absent", "table": "present"})

    def test_enumeration_literal_is_not_presence(self):
        pred, syntax = scoring.parse_response('{"toilet":"present|absent|unknown","table":"absent"}')
        self.assertTrue(syntax)
        self.assertEqual(pred["toilet"], "invalid")

    def test_abstention_is_not_true_negative(self):
        rows = [{"image_id": "q09", "text": '{"toilet":"unknown","table":"absent"}'}]
        result = scoring.evaluate(rows, {"q09": {"toilet": "absent", "table": "absent"}})
        self.assertEqual(result["tn"], 1)
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(result["accuracy"], 0.5)
        self.assertEqual(result["negative_confident_correct_rate"], 0.5)

    def test_policy_rejection_separate_from_presence_truth(self):
        rows = [{"image_id": "q13", "text": '{"toilet":"unknown","table":"unknown"}'}]
        labels = {"q13": {"toilet": "absent", "table": "absent"}}
        self.assertEqual(scoring.evaluate(rows, labels)["correct"], 0)
        self.assertEqual(scoring.evaluate(rows, labels, policy=True)["correct"], 2)

    def test_prose_does_not_turn_into_structured_success(self):
        pred, syntax = scoring.parse_response('没有马桶，桌子可能有。')
        self.assertFalse(syntax)
        self.assertEqual(set(pred.values()), {"invalid"})

    def test_fences_preserve_fact_scoring_but_fail_strict_format(self):
        pred, strict = scoring.parse_response('```json\n{"toilet":"present","table":"absent"}\n```')
        self.assertFalse(strict)
        self.assertEqual(pred, {"toilet": "present", "table": "absent"})

    def test_boolean_and_single_object_array_are_only_format_recovery(self):
        text = '[{"toilet":false,"table":true}]'
        pred, strict = scoring.parse_response(text)
        self.assertFalse(strict)
        self.assertFalse(scoring.unrepaired_schema_valid(text))
        self.assertEqual(pred, {"toilet": "absent", "table": "present"})

    def test_abstention_on_positive_is_a_miss_for_recall(self):
        rows = [{"image_id": "q01", "text": '{"toilet":"unknown","table":"absent"}'}]
        result = scoring.evaluate(rows, {"q01": {"toilet": "present", "table": "absent"}})
        self.assertEqual(result["recall_with_abstentions_counted_as_missed"], 0)


if __name__ == "__main__":
    unittest.main()
