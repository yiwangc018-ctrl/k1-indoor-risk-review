"""PolishPacket：事实锁、润色标注与等级一致性；不需要 K1、不联网。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import polish_packet as packet


def analysis(level="medium", status="complete"):
    return {
        "version": 1, "status": status, "overall_level": level,
        "assessed_views": 1, "input_views": 1, "coverage_complete": False,
        "scope": "本轮可见区域的模型初评；等级为整理优先程度，不是事故概率。",
        "views": [{
            "view_id": "view_01", "usable": True, "level": level,
            "reason": "桌面物品挤占操作空间，影响日常活动。",
            "raw_text": "{\"status\": \"assessed\"}",
            "focus": [{"kind": "placement", "where": "桌面", "basis": "物品挤占操作空间"}],
        }],
        "priority_regions": [{
            "kind": "placement", "where": "桌面", "basis": "物品挤占操作空间",
            "view_id": "view_01", "level": level,
            "suggestion": "建议先把该区域的物品归位。",
            "suggestion_source": "rule_by_focus_kind",
        }],
    }


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.round = self.root / "round_test"
        self.round.mkdir()
        self.packet = self.root / "polish"
        (self.round / "analysis.json").write_text(json.dumps(analysis(), ensure_ascii=False), encoding="utf-8")
        (self.round / "meta.json").write_text(json.dumps({
            "round_id": "round_test", "coverage": "仅本轮照片可见区域",
            "views": [{"view_id": "view_01", "pose": {"source": "sensor"}}],
        }, ensure_ascii=False), encoding="utf-8")
        (self.round / "report.md").write_text("# 可见区域风险程度初评\n\n关注程度：**中**。\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def build(self):
        built = packet.build_packet(self.round, self.packet)
        return built

    def polish(self, text="# 报告\n\n人工后处理。关注程度：**中**。\n", meta=None, digest=None):
        built = self.build()
        (self.packet / "polished.md").write_text(text, encoding="utf-8")
        base = {"editor": "human", "by": "jim", "ts": "2026-11-06T16:00:00+08:00",
                "facts_sha256": digest or built["sha256"], "edited_fields": ["narrative"]}
        (self.packet / "polish_meta.json").write_text(
            json.dumps({**base, **(meta or {})}, ensure_ascii=False), encoding="utf-8")
        return built

    def test_build_freezes_facts_and_keeps_machine_draft(self):
        built = self.build()
        self.assertTrue((self.packet / "facts.json").is_file())
        self.assertTrue((self.packet / "draft.md").is_file())
        self.assertEqual(built["facts"]["overall_level"], "medium")
        self.assertEqual(built["facts"]["views"][0]["focus"][0]["where"], "桌面")
        self.assertEqual(built["facts"]["pose_source"], ["sensor"])
        self.assertEqual((self.packet / "facts.sha256").read_text(encoding="utf-8").strip(), built["sha256"])

    def test_build_refuses_round_without_analysis(self):
        (self.round / "analysis.json").unlink()
        with self.assertRaisesRegex(packet.PolishPacketError, "analysis.json"):
            packet.build_packet(self.round, self.packet)

    def test_untouched_facts_validate(self):
        self.polish()
        self.assertEqual(packet.validate_packet(self.packet), [])

    def test_tampered_facts_are_rejected(self):
        self.polish()
        facts = json.loads((self.packet / "facts.json").read_text(encoding="utf-8"))
        facts["overall_level"] = "low"
        (self.packet / "facts.json").write_text(json.dumps(facts, ensure_ascii=False), encoding="utf-8")
        problems = packet.validate_packet(self.packet)
        self.assertTrue(any("事实文件被改过" in p for p in problems), problems)

    def test_conflicting_level_in_polish_is_rejected(self):
        self.polish(text="# 报告\n\n人工后处理。关注程度：**低**。\n")
        problems = packet.validate_packet(self.packet)
        self.assertTrue(any("冲突的等级" in p for p in problems), problems)

    def test_human_edit_must_be_labelled(self):
        self.polish(text="# 报告\n\n关注程度：**中**。\n")
        problems = packet.validate_packet(self.packet)
        self.assertTrue(any("人工后处理" in p for p in problems), problems)

    def test_partial_round_cannot_be_polished_into_complete(self):
        (self.round / "analysis.json").write_text(
            json.dumps(analysis(status="partial"), ensure_ascii=False), encoding="utf-8")
        self.polish(text="# 报告\n\n人工后处理。关注程度：**中**。本轮完整完成。\n")
        problems = packet.validate_packet(self.packet)
        self.assertTrue(any("部分完成" in p for p in problems), problems)

    def test_meta_digest_must_match_facts(self):
        self.polish(digest="0" * 64)
        problems = packet.validate_packet(self.packet)
        self.assertTrue(any("facts_sha256" in p for p in problems), problems)

    def test_uneditable_fields_and_tool_traceability(self):
        self.polish(meta={"editor": "tool", "tool": "", "edited_fields": ["narrative", "level"]})
        problems = packet.validate_packet(self.packet)
        self.assertTrue(any("不可润色的字段" in p for p in problems), problems)
        self.assertTrue(any("tool" in p for p in problems), problems)

    def test_real_board_round_builds_when_present(self):
        real = (Path(__file__).resolve().parents[1] / "results/room_review_20261105/board/rounds"
                / "round_20261105_200000_4a8db9")
        if not (real / "analysis.json").is_file():
            self.skipTest("本地没有 11-05 的板端轮次快照")
        built = packet.build_packet(real, self.packet)
        self.assertEqual(built["facts"]["overall_level"], "medium")
        self.assertEqual(built["facts"]["round_id"], "round_20261105_200000_4a8db9")


if __name__ == "__main__":
    unittest.main()
