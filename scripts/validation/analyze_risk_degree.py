"""Summarize actual risk-degree responses without treating failures as low."""
import argparse
from html import escape
import hashlib
import json
from pathlib import Path
import re

LEVELS = {"low": 0, "medium": 1, "high": 2}
LABELS = {"low": "低", "medium": "中", "high": "高", None: "无法评估"}


def parse_coarse(text):
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        match = re.fullmatch(r"\s*```(?:json)?\s*\n?(.*?)\n?```\s*", text or "", re.S)
        if not match:
            return {"usable": False, "error": "invalid JSON"}
        try:
            obj = json.loads(match.group(1))
        except ValueError:
            return {"usable": False, "error": "invalid JSON"}
    if not isinstance(obj, dict) or not isinstance(obj.get("reason"), str) or not obj["reason"].strip():
        return {"usable": False, "error": "missing core fields"}
    if obj.get("status") == "unassessable" and obj.get("level") == "not_assessed":
        return {"usable": False, "error": "model unassessable", "reason": obj["reason"]}
    if obj.get("status") != "assessed" or obj.get("level") not in LEVELS:
        return {"usable": False, "error": "invalid status/level"}
    focus = obj.get("focus", [])
    valid = [x for x in focus if isinstance(x, dict) and x.get("kind") in ("movement", "placement", "collision")
             and all(isinstance(x.get(k), str) and x[k].strip() for k in ("where", "basis"))] if isinstance(focus, list) else []
    return {"usable": True, "level": obj["level"], "reason": obj["reason"], "focus": valid[:1]}


def overall_level(views):
    return max((x["level"] for x in views if x.get("usable")), key=LEVELS.get, default=None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path)
    args = parser.parse_args()
    root = args.run or Path(Path("results/risk_degree/latest_run.txt").read_text(encoding="utf-8"))
    manifest = json.loads((root / "assets/inputs.current_scene.json").read_text(encoding="utf-8"))
    summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    prep = {x["id"]: x for x in json.loads((root / "preprocess.json").read_text(encoding="utf-8"))}
    all_rows = []
    for prefix in ("inference", "recovery"):
        for file in sorted((root / prefix).glob("*/responses.jsonl")):
            for line in file.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                row["record_source"] = str(file.relative_to(root))
                assert hashlib.sha256((root / "processed" / (row["image_id"] + ".jpg")).read_bytes()).hexdigest() == row["sha256"]
                all_rows.append(row)
    views = []
    schema = json.loads((root / "assets/view_response.schema.json").read_text(encoding="utf-8"))
    from jsonschema import Draft202012Validator
    validator = Draft202012Validator(schema)
    for view in manifest["views"]:
        ident = view["view_id"]
        assert hashlib.sha256((root / "originals" / (ident + ".jpeg")).read_bytes()).hexdigest() == prep[ident]["original_sha256"]
        attempts = [r for r in all_rows if r["image_id"] == ident]
        usable = [(r, parse_coarse(r.get("text", ""))) for r in attempts if r.get("ok")]
        usable = [(r, p) for r, p in usable if p["usable"]]
        if usable:
            # Recovery only fills a missing valid view; never pick among grades.
            assert len(usable) == 1, "Multiple valid outputs require an explicit selection policy"
            row, parsed = usable[0]
            try:
                strict = not list(validator.iter_errors(json.loads(row["text"])))
            except ValueError:
                strict = False
        else:
            row = attempts[-1] if attempts else {}
            parsed = parse_coarse(row.get("text", ""))
            strict = False
        views.append({"view_id": ident, **parsed, "strict_schema_valid": strict,
            "attempts": len(attempts), "selected_source": row.get("record_source"),
            "elapsed_seconds": row.get("elapsed_seconds"), "raw_text": row.get("text", ""),
            "api_error": row.get("error"), "attempt_history": [{"ok": a.get("ok"),
                "seconds": a["elapsed_seconds"], "error": a.get("error"), "source": a["record_source"]} for a in attempts]})
    groups = []
    for view in views:
        if not view["usable"]:
            continue
        for focus in view.get("focus", []):
            matches = [g for g in groups if g["kind"] == focus["kind"] and g["where"] == focus["where"]
                and any(set(g["view_ids"] + [view["view_id"]]).issubset(set(region["view_ids"])) for region in manifest["region_groups"])]
            if matches:
                g = matches[0]
                g["view_ids"].append(view["view_id"])
                g["level"] = max([g["level"], view["level"]], key=LEVELS.get)
            else:
                groups.append({**focus, "level": view["level"], "view_ids": [view["view_id"]]})
    priorities = sorted(groups, key=lambda x: LEVELS[x["level"]], reverse=True)[:3]
    recovery = root / "recovery/summary.json"
    rec = json.loads(recovery.read_text(encoding="utf-8")) if recovery.exists() else None
    result = {"task_version": "risk_degree_v1", "scope": "One real room, four views; no accuracy or generalization estimate",
        "overall_level": overall_level(views), "assessed_views": sum(v["usable"] for v in views),
        "input_views": len(views), "inference_attempts": len(all_rows), "views": views,
        "priority_candidates": priorities, "grouping_method": "exact focus text within preset overlapping-view group; not verified object tracking",
        "first_round_compute_seconds": summary["total_compute_seconds"], "recovery": rec,
        "first_round_failed_attempts": sum(not x.get("ok", False) for x in all_rows if x["record_source"].replace("\\", "/").startswith("inference/")),
        "image_bindings_verified": True}
    (root / "assessment.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 四张真实场景照片：2B风险程度初评实测", "",
        f"模型：Qwen3.5-2B / 384 / runtime017。有效粗评：{result['assessed_views']}/{len(views)}；总推理尝试{len(all_rows)}次。", "",
        f"程序按最高有效等级汇总：**{LABELS[result['overall_level']]}**。这是模型粗评，不是人工改写结论或事故概率。", "",
        "| 图片 | 模型等级 | 模型主要理由 | 本次有效回答耗时 |", "|---|---|---|---:|"]
    for v in views:
        lines.append(f"| {v['view_id']} | {LABELS.get(v.get('level'), '未完成')} | {v.get('reason') or v.get('api_error') or v.get('error')} | {v.get('elapsed_seconds')}s |")
    lines += ["", f"首轮计算耗时{summary['total_compute_seconds']:.2f}s，包含预处理、YOLO及VLM启动/结束。首轮失败尝试{result['first_round_failed_attempts']}次。", ""]
    if rec:
        lines += [f"第四张独立补测耗时{rec['total_seconds']:.2f}s（含重新启动模型）。补测不计入首轮300s内完成，原超时记录保留。", ""]
    lines += ["不含拍照、云台、上传及开发者整理报告的等待时间。四张是同一个场景，不能计算风险准确率或泛化能力。", "",
              "每张原始回答、每次尝试和图片见 report.html / assessment.json；提示与schema见assets，原始API记录见inference及recovery。", ""]
    (root / "report.md").write_text("\n".join(lines), encoding="utf-8")
    html = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>2B真实场景风险初评实测</title><style>body{font:16px/1.7 system-ui,"Microsoft YaHei";margin:25px auto;padding:0 20px;max-width:1200px;background:#f3f5f8;color:#20304a}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px}article{background:white;padding:20px;border-radius:10px}img{width:100%;height:330px;object-fit:contain;background:#e7ecf2}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf1f6;padding:12px;font-size:14px}small{color:#52627a}</style>']
    html += [f'<h1>2B真实场景风险程度初评：{LABELS[result["overall_level"]]}</h1>',
        f'<p>有效粗评 {result["assessed_views"]}/4；共{len(all_rows)}次尝试。原图和模型原文保持可查，人工复核见下方独立说明。</p>',
        f'<p>首轮 {summary["total_compute_seconds"]:.2f}s；第四张补测 {rec["total_seconds"]:.2f}s。</p>' if rec else '<p>首轮预算已到，尚未有有效补测结果。</p>',
        '<p>等级是本轮已评估区域的关注程度，不是事故概率；一个房间不能证明泛化准确率。点击图片查看完整输入。</p><div class="grid">']
    for v in views:
        ident = v["view_id"]
        html += [f'<article><h2>{ident} · {LABELS.get(v.get("level"), "未完成")}</h2>',
            f'<a href="processed/{ident}.jpg" target="_blank"><img src="processed/{ident}.jpg"></a>',
            f'<p>{escape(v.get("reason", v.get("api_error") or v.get("error", "")))}</p>',
            f'<small>尝试{v["attempts"]}次；有效回答来源：{escape(v.get("selected_source") or "无")}</small>',
            '<h3>2B原始回答</h3><pre>' + escape(v["raw_text"]) + '</pre></article>']
    html += ['</div><h2>测试说明</h2><p>首轮300秒总预算使第四张只剩约38秒而超时，前三张没有重跑，只补第四张。补测沿用相同图片、提示、模型与输出限制，没有向模型提供人工等级。</p>']
    review = root / "semantic_review.json"
    if review.exists():
        html += ['<h2>人工复核说明（与模型原文分开）</h2><pre>' + escape(json.dumps(json.loads(review.read_text(encoding="utf-8")), ensure_ascii=False, indent=2)) + '</pre>']
    (root / "report.html").write_text("\n".join(html) + '</html>', encoding="utf-8")
    print(json.dumps({"overall_level": result["overall_level"], "assessed_views": result["assessed_views"],
        "attempts": len(all_rows), "first_round_seconds": summary["total_compute_seconds"], "recovery": rec}, ensure_ascii=False))


if __name__ == "__main__":
    main()
