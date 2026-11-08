"""PC 润色执行器：模型只填措辞槽位，事实由脚本渲染；不需要 K1、不联网。"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import polish_packet as packet
import polish_report as report


def analysis(level="medium", status="complete", priorities=2):
    views = [
        {"view_id": "view_01", "usable": True, "level": level,
         "reason": "桌面物品挤占操作空间。", "raw_text": "{}",
         "focus": [{"kind": "placement", "where": "桌面", "basis": "物品挤占操作空间"}]},
        {"view_id": "view_02", "usable": True, "level": "low",
         "reason": "通道较空。", "raw_text": "{}", "focus": []},
    ]
    regions = [{"kind": "placement", "where": "桌面", "basis": "物品挤占操作空间",
                "view_id": "view_01", "level": level,
                "suggestion": "建议先把桌面物品归位。", "suggestion_source": "rule_by_focus_kind"},
               {"kind": "collision", "where": "柜门", "basis": "柜门伸入活动位置",
                "view_id": "view_02", "level": "low",
                "suggestion": "建议收拢柜门。", "suggestion_source": "rule_by_focus_kind"}]
    return {
        "version": 1, "status": status, "overall_level": level,
        "assessed_views": 2, "input_views": 2, "coverage_complete": False,
        "scope": "本轮可见区域的模型初评；等级为整理优先程度，不是事故概率。",
        "views": views, "priority_regions": regions[:priorities],
    }


def write_round(root, **kwargs):
    round_dir = Path(root) / "round_test"
    round_dir.mkdir(parents=True)
    (round_dir / "analysis.json").write_text(
        json.dumps(analysis(**kwargs), ensure_ascii=False), encoding="utf-8")
    (round_dir / "meta.json").write_text(json.dumps({
        "round_id": "round_test", "coverage": "仅本轮照片可见区域",
        "views": [{"view_id": "view_01", "pose": {"source": "sensor"}},
                  {"view_id": "view_02", "pose": {"source": "commanded"}}],
    }, ensure_ascii=False), encoding="utf-8")
    (round_dir / "report.md").write_text("# 可见区域风险程度初评\n\n关注程度：**中**。\n",
                                         encoding="utf-8")
    return round_dir


class PolishReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.round = write_round(self.root)
        self.packet = self.root / "polish"
        packet.build_packet(self.round, self.packet)

    def tearDown(self):
        self.temp.cleanup()

    def compose(self, payload):
        source = self.root / "model_output.json"
        source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return report.cmd_compose(argparse.Namespace(
            packet=self.packet, model_output=source, tool="test-model/1", by="unittest"))

    def test_prompt_contains_regions_and_count(self):
        facts, _ = report.load_packet(self.packet)
        prompt = report.build_prompt(facts, (self.packet / "draft.md").read_text(encoding="utf-8"))
        self.assertIn("0. view_01 · 桌面", prompt)
        self.assertIn("1. view_02 · 柜门", prompt)
        self.assertIn("advice 数组长度必须等于 2", prompt)
        self.assertIn("不得改等级", prompt)

    def test_compose_renders_facts_and_passes_validation(self):
        code = self.compose({"summary": "本轮已分析可见区域，桌面较拥挤，柜门影响活动。",
                             "advice": ["建议先归位桌面物品。", "建议把柜门收拢。"]})
        self.assertEqual(code, 0)
        polished = (self.packet / "polished.md").read_text(encoding="utf-8")
        self.assertIn("关注程度：**中**", polished)
        self.assertIn("view_01 · 桌面（中）", polished)
        self.assertIn("建议先归位桌面物品", polished)
        meta = json.loads((self.packet / "polish_meta.json").read_text(encoding="utf-8"))
        facts = json.loads((self.packet / "facts.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["facts_sha256"], packet.facts_sha256(facts))
        self.assertEqual(meta["tool"], "test-model/1")

    def test_advice_count_must_match_regions(self):
        with self.assertRaisesRegex(report.PolishReportError, "advice 数量"):
            self.compose({"summary": "总体情况。", "advice": ["只有一条"]})

    def test_summary_is_required(self):
        with self.assertRaisesRegex(report.PolishReportError, "summary"):
            self.compose({"advice": ["建议甲。", "建议乙。"]})

    def test_conflicting_level_in_summary_is_rejected(self):
        code = self.compose({"summary": "整体高风险，建议立即停用。",
                             "advice": ["建议甲。", "建议乙。"]})
        self.assertEqual(code, 2)
        polished = (self.packet / "polished.md").read_text(encoding="utf-8")
        self.assertIn("关注程度：**中**", polished)

    def test_model_cannot_turn_partial_into_complete(self):
        (self.round / "analysis.json").write_text(
            json.dumps(analysis(status="partial"), ensure_ascii=False), encoding="utf-8")
        packet.build_packet(self.round, self.packet)
        code = self.compose({"summary": "本轮全部完成，覆盖完整。",
                             "advice": ["建议甲。", "建议乙。"]})
        self.assertEqual(code, 2)

    def test_template_fallback_needs_no_model_and_passes(self):
        code = report.cmd_template(argparse.Namespace(
            packet=self.packet, tool="template/v1", by="unittest"))
        self.assertEqual(code, 0)
        polished = (self.packet / "polished.md").read_text(encoding="utf-8")
        self.assertIn("关注程度：**中**", polished)
        meta = json.loads((self.packet / "polish_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["tool"], "template/v1")

    def test_template_partial_round_passes(self):
        (self.round / "analysis.json").write_text(
            json.dumps(analysis(status="partial", priorities=0), ensure_ascii=False),
            encoding="utf-8")
        packet.build_packet(self.round, self.packet)
        code = report.cmd_template(argparse.Namespace(
            packet=self.packet, tool="template/v1", by="unittest"))
        self.assertEqual(code, 0)

    def test_parse_model_output_accepts_fenced_json(self):
        parsed = report.parse_model_output('```json\n{"summary": "s", "advice": []}\n```')
        self.assertEqual(parsed["summary"], "s")

    def test_parse_model_output_rejects_text(self):
        with self.assertRaisesRegex(report.PolishReportError, "JSON"):
            report.parse_model_output("好的，这是润色稿……")

    def test_facts_digest_mismatch_is_rejected(self):
        facts_file = self.packet / "facts.json"
        facts = json.loads(facts_file.read_text(encoding="utf-8"))
        facts["overall_level"] = "low"
        facts_file.write_text(json.dumps(facts, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(report.PolishReportError, "不一致"):
            report.load_packet(self.packet)

    def test_real_board_round_composes_with_template(self):
        real = (Path(__file__).resolve().parents[1] / "results/room_review_20261106/board/rounds"
                / "round_20261106_171550_b95e0f")
        if not (real / "analysis.json").is_file():
            self.skipTest("本地没有 11-06 的板端轮次快照")
        real_packet = self.root / "real_polish"
        packet.build_packet(real, real_packet)
        self.assertEqual(report.cmd_template(argparse.Namespace(
            packet=real_packet, tool="template/v1", by="unittest")), 0)


if __name__ == "__main__":
    unittest.main()
