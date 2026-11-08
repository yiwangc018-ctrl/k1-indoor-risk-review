"""Reporting must preserve partial coverage and the chosen coarse task."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("risk_reporting", Path(__file__).resolve().parents[1] / "scripts/validation/analyze_risk_degree.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RiskReportingTests(unittest.TestCase):
    def test_unassessable_is_not_low(self):
        value = module.parse_coarse('{"status":"unassessable","level":"not_assessed","reason":"空图"}')
        self.assertFalse(value["usable"])
        self.assertIsNone(module.overall_level([value]))

    def test_local_high_is_not_averaged_away(self):
        self.assertEqual(module.overall_level([{"usable": True, "level": x} for x in ["low", "low", "high", "low"]]), "high")

    def test_truncated_response_cannot_become_a_low_grade(self):
        self.assertFalse(module.parse_coarse('{"status":"assessed","level":"low",')["usable"])

    def test_bad_optional_detail_does_not_remove_valid_coarse_grade(self):
        value = module.parse_coarse('{"status":"assessed","level":"medium","reason":"局部拥挤","focus":[{"where":12}]}')
        self.assertTrue(value["usable"])
        self.assertEqual(value["level"], "medium")
        self.assertEqual(value["focus"], [])


if __name__ == "__main__":
    unittest.main()
