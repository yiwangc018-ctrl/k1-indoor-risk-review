"""买家版 HTML 报告：照片、评估、问题点；事实摘要不符时拒绝生成。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import buyer_report
import polish_packet

from PIL import Image


def make_round(root):
    round_dir = Path(root) / "round_test"
    originals = round_dir / "originals"
    originals.mkdir(parents=True)
    for index in (1, 2):
        Image.new("RGB", (160, 120), (40 * index, 80, 120)).save(originals / f"view_{index:02d}.jpg")
    analysis = {
        "version": 1, "status": "complete", "overall_level": "medium",
        "assessed_views": 2, "input_views": 2,
        "scope": "本轮可见区域的模型初评；等级为整理优先程度，不是事故概率。",
        "coverage_complete": False,
        "views": [
            {"view_id": "view_01", "usable": True, "level": "medium",
             "reason": "桌面物品堆放。", "raw_text": "{\"level\": \"medium\"}",
             "focus": [{"kind": "placement", "where": "桌面", "basis": "物品堆放"}]},
            {"view_id": "view_02", "usable": True, "level": "low",
             "reason": "通道较空。", "raw_text": "{\"level\": \"low\"}", "focus": []},
        ],
        "priority_regions": [{
            "kind": "placement", "where": "桌面", "basis": "物品堆放",
            "view_id": "view_01", "level": "medium", "suggestion": "建议归位。",
            "suggestion_source": "rule_by_focus_kind",
        }],
    }
    meta = {
        "round_id": "round_test", "created_at": "2026-11-08T06:40:43+00:00",
        "coverage": "仅本轮照片可见区域",
        "views": [
            {"view_id": "view_01", "file": "originals/view_01.jpg", "pose": None},
            {"view_id": "view_02", "file": "originals/view_02.jpg", "pose": None},
        ],
    }
    (round_dir / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")
    (round_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (round_dir / "report.md").write_text("# 报告\n\n关注程度：**中**。\n", encoding="utf-8")
    return round_dir


def add_polish(round_dir):
    packet = Path(round_dir) / "polish"
    built = polish_packet.build_packet(round_dir, packet)
    (packet / "model_output.json").write_text(json.dumps({
        "summary": "润色后的总体说明。",
        "advice": ["润色后的建议。"],
    }, ensure_ascii=False), encoding="utf-8")
    (packet / "polish_meta.json").write_text(json.dumps({
        "editor": "tool", "by": "unittest", "tool": "test-model/1",
        "ts": "2026-11-08T15:00:00+08:00",
        "facts_sha256": built["sha256"],
        "edited_fields": ["narrative", "summary"],
    }, ensure_ascii=False), encoding="utf-8")
    return built


class BuyerReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.round = make_round(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def build(self):
        out = self.root / "report.html"
        buyer_report.build(self.round, out)
        return out.read_text(encoding="utf-8")

    def test_uses_polished_summary_and_advice_with_photos(self):
        built = add_polish(self.round)
        html = self.build()
        self.assertIn("润色后的总体说明", html)
        self.assertIn("润色后的建议", html)
        self.assertIn("桌面", html)
        self.assertIn(built["sha256"], html)
        self.assertGreaterEqual(html.count("data:image/jpeg;base64,"), 3)
        self.assertNotIn("照片缺失", html)
        self.assertIn("优先处理的问题", html)
        self.assertIn("覆盖与说明", html)

    def test_falls_back_to_template_without_polish_packet(self):
        html = self.build()
        self.assertIn("本次检查共 2 张照片", html)
        self.assertIn("建议归位", html)
        self.assertNotIn("润色后的总体说明", html)

    def test_changed_facts_are_rejected(self):
        add_polish(self.round)
        analysis_file = self.round / "analysis.json"
        analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
        analysis["overall_level"] = "high"
        analysis_file.write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(buyer_report.ReportError, "摘要不匹配"):
            buyer_report.build(self.round, self.root / "report.html")

    def test_missing_photo_becomes_placeholder(self):
        (self.round / "originals" / "view_02.jpg").unlink()
        html = self.build()
        self.assertIn("照片缺失", html)

    def test_gimbal_round_does_not_claim_manual_photos(self):
        meta_file = self.round / "meta.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["capture_mode"] = "gimbal_flow30"
        meta["views"][0]["pose"] = {
            "commanded": {"pan": 67.0, "tilt": 100.1, "pan_us": 996.1, "tilt_us": 2241.2},
            "source": "commanded",
            "rotation_deg": 0,
        }
        meta_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        html = self.build()
        self.assertNotIn("人工提供", html)
        self.assertNotIn("无云台", html)
        self.assertIn("云台按固定", html)
        self.assertIn("这 2 张照片", html)
        self.assertIn("pan 67.0° / tilt 100.1°", html)

    def test_manual_round_says_manual(self):
        meta_file = self.round / "meta.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["capture_mode"] = "manual_views"
        meta_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        html = self.build()
        self.assertIn("人工逐张拍摄", html)

    def test_conflicting_summary_is_rejected(self):
        add_polish(self.round)
        output = self.round / "polish" / "model_output.json"
        data = json.loads(output.read_text(encoding="utf-8"))
        data["summary"] = "本轮整体为高风险，建议立即处理。"
        output.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(buyer_report.ReportError, "冲突的等级"):
            buyer_report.build(self.round, self.root / "report.html")

    def test_model_text_is_escaped(self):
        analysis_file = self.round / "analysis.json"
        analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
        analysis["views"][0]["reason"] = "<script>alert(1)</script>"
        analysis_file.write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")
        html = self.build()
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)


if __name__ == "__main__":
    unittest.main()

