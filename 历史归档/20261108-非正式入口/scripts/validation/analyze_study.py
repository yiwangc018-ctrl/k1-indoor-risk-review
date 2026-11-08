"""Build an evidence summary; never infer risk truth from dataset safety labels."""
import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics

here = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("presence_scoring", here / "score_presence.py")
scorer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scorer)


def load(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def rows(path):
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                result.append(json.loads(line))
            except ValueError:
                # A live append may be caught mid-line; score only full records.
                continue
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("results/k1_validation_20261103"))
    args = parser.parse_args()
    root = args.root
    labels = {x["qid"]: x for x in load(Path("results/vlm_eval/labels.json"), [])}
    result = {"presence": {}, "controls_and_relations": {}, "format": {}, "text": {},
              "phases": {}, "progress": {}, "notes": []}
    intervals = []
    for model in ("2b", "4b"):
        download = load(root / ("models_" + model) / "download_status.json", {})
        if download.get("phase") == "ready":
            intervals.append((model, download["started"], download["updated"]))
    for directory, key in (("presence", "presence"), ("domain", "controls_and_relations"), ("format", "format")):
        result[key] = scorer.summarize(root / directory, labels)
        for folder in sorted((root / directory).glob("*")):
            if not folder.is_dir():
                continue
            metadata = load(folder / "metadata.json", {})
            records = rows(folder / "responses.jsonl")
            if metadata:
                result["phases"][directory + "/" + folder.name] = {
                    "status": metadata.get("status"), "error": metadata.get("error"),
                    "startup_seconds": metadata.get("startup_seconds"),
                    "total_phase_seconds": metadata.get("total_phase_seconds"),
                    "peak_server_memory": metadata.get("peak_server_memory"),
                    "runtime_winner": metadata.get("spec", {}).get("runtime_selection", {}).get("winner")}
            for variant in {r["variant"] for r in records}:
                part = [r for r in records if r["variant"] == variant]
                clean = []
                for row in part:
                    before = datetime.fromisoformat(row["before"]["time"]).timestamp()
                    after = datetime.fromisoformat(row["after"]["time"]).timestamp()
                    if not any(after >= start and before <= end for _, start, end in intervals):
                        clean.append(row["elapsed_seconds"])
                item = result[key][folder.name + "/" + variant]
                item["no_model_download_overlap_latency"] = {"n": len(clean),
                    "median_seconds": statistics.median(clean) if clean else None}
    semantic = load(root / "text_semantic_reference.json", {})
    inputs = {x["id"]: x for x in load(root / "text/inputs.json", [])}
    text_rows = rows(root / "text/responses.jsonl")
    for mode in sorted({x["mode"] for x in text_rows}):
        for source in ("controlled_text_fixture", "actual_vlm_prose"):
            part = [x for x in text_rows if x["mode"] == mode and x["source"] == source]
            if not part:
                continue
            counts = dict(facts=0, correct=0, invalid=0, explicit_known=0,
                          explicit_known_preserved=0, unknown_expected=0, unknown_preserved=0)
            per_case = []
            for row in part:
                expected = row.get("expected")
                if expected is None and row["id"] in semantic:
                    reference = semantic[row["id"]]
                    text = inputs[row["id"]]["text"]
                    if hashlib.sha256(text.encode()).hexdigest() != reference["text_sha256"]:
                        raise ValueError("Source text changed: " + row["id"])
                    expected = reference["expected"]
                if expected is None:
                    continue
                pred, _ = scorer.parse_response(row.get("text", ""))
                for field in scorer.FIELDS:
                    counts["facts"] += 1
                    counts["correct"] += pred[field] == expected[field]
                    counts["invalid"] += pred[field] == "invalid"
                    kind = "unknown" if expected[field] == "unknown" else "known"
                    counts["unknown_expected" if kind == "unknown" else "explicit_known"] += 1
                    counts["unknown_preserved" if kind == "unknown" else "explicit_known_preserved"] += pred[field] == expected[field]
                per_case.append({"id": row["id"], "expected": expected, "predicted": pred,
                                 "seconds": row["elapsed_seconds"]})
            result["text"][mode + "/" + source] = {**counts, "requests": len(part),
                "fidelity": counts["correct"] / counts["facts"] if counts["facts"] else None,
                "strict_schema_valid": sum(scorer.unrepaired_schema_valid(x.get("text", "")) for x in part),
                "fidelity_definition": "Contract field equality under the declared parser; invalid enums count as failures. Not a claim that every invalid Chinese enum has wrong semantic meaning.",
                "median_wall_seconds": statistics.median(x["elapsed_seconds"] for x in part),
                "ollama_internal_median_seconds": {key: statistics.median(values) if values else None
                    for key in ("load_duration", "prompt_eval_duration", "eval_duration")
                    for values in [[x["response"][key] / 1e9 for x in part if key in x.get("response", {})]]},
                "per_case": per_case,
                "scope": "Text preservation only; preserving a false VLM claim is not visual correction."}
    for directory in ("presence", "domain", "format", "text", "batch", "batch_full"):
        if not (root / directory).exists():
            continue
        result["progress"][directory] = load(root / directory / "progress.json", {})
    result["batch"] = load(root / "batch/summary.json", [])
    result["batch_full"] = load(root / "batch_full/summary.json", [])
    review = load(root / "domain_semantic_review.json")
    if review:
        source_rows = {r["job_id"]: r for r in rows(root / "domain" / review["phase"] / "responses.jsonl")}
        for item in review["items"]:
            source = source_rows[item["job_id"]]
            if (hashlib.sha256(source.get("text", "").encode()).hexdigest() != item["response_text_sha256"]
                    or source["sha256"] != item["image_sha256"]):
                raise ValueError("Semantic review source changed: " + item["job_id"])
        result["domain_semantic_review"] = {"phase": review["phase"], "reviewed_requests": len(review["items"]),
            "saved_requests": len(source_rows), "response_bindings_valid": True, "scope": review["scope"]}
    result["notes"] = [
        "Public samples and repeated transformations are not independent household tests.",
        "Original out-of-room labels are absent; the prompt rejection policy expects unknown. Both are reported separately.",
        "One Markdown fence, one-object array and Boolean presence values may be normalized for fact scoring; strict schema validity is separate.",
        "Domain relations require visible-evidence review; author safe/unsafe labels are not physical stability truth.",
        "Hardware peripherals were not ready; no camera acquisition, servo movement or physical coverage validated."]
    (root / "analysis_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# K1 视觉验证进度与证据摘要", "", "此文件按已保存记录自动汇总，测试未全部结束时不是最终验收。", "",
             "## 基础事实题（格式修复与事实准确性分开）", "",
             "| 配置/变体 | 请求数 | 室内正确/总项 | 明确误报 | unknown | 单图中位秒 | 原始 schema 合规数 |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for key in ("presence", "controls_and_relations", "format"):
        for name, item in result[key].items():
            facts = item.get("indoor_presence", {})
            if facts.get("facts"):
                lines.append(f"| {name} | {item['requests']} | {facts['correct']}/{facts['facts']} | {facts['fp']} | {facts['unknown']} | {item['latency_seconds']['median']:.2f} | {item['unrepaired_schema_valid']} |")
    lines += ["", "## 调度状态", ""]
    lines += [f"- {name}: `{state.get('status', state.get('phase','pending'))}`" for name, state in result["progress"].items()]
    if result["text"]:
        lines += ["", "## 文本抽取保真度（字段契约）", ""]
        for name, item in result["text"].items():
            lines.append(f"- {name}: {item['correct']}/{item['facts']}，明确结论保留 {item['explicit_known_preserved']}/{item['explicit_known']}，未知保留 {item['unknown_preserved']}/{item['unknown_expected']}，墙钟中位 {item['median_wall_seconds']:.2f}s。")
        lines += ["", "按声明解析器核对字段；非法枚举计契约失败，不代表每个中文枚举的语义都错。严格格式与Ollama加载/预填充/生成耗时见JSON明细；本轮keep_alive=0，墙钟包含每次加载。"]
    if result["batch"]:
        lines += ["", "## 原始图片批处理（画质门禁影响覆盖）", ""]
        for item in result["batch"]:
            lines.append(f"- {item['case']}: {item['input_images']} 张输入、{item['quality_passed']} 张通过画质、{item['actual_vlm_responses']} 次视觉回答，总计算/报告 {item['total_compute_and_report_seconds']:.2f}s；不含相机或云台。")
    if result["batch_full"]:
        lines += ["", "## 全部输入通过原门禁的计算负载补测", "",
                  "预先按画质筛选的公开图，只测计算预算；不是独立准确率或真实房间覆盖。"]
        for item in result["batch_full"]:
            lines.append(f"- {item['case']}: {item['input_images']} 张输入、{item['quality_passed']} 张通过画质、{item['actual_vlm_responses']} 次视觉回答，总计算/报告 {item['total_compute_and_report_seconds']:.2f}s；未因门禁减少计划负载。")
    lines += ["", "## 边界", "", "- 结果是小样本诊断，不是风险级泛化准确率。", "- unknown 不等于不存在或安全。", "- 已保存原始 JSONL、启动配置和资源记录；完整明细见 analysis_summary.json。", ""]
    (root / "progress_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"phases": len(result["phases"]), "text_groups": len(result["text"]),
                      "batch_cases": len(result["batch"]), "report": str(root / "progress_report.md")}))


if __name__ == "__main__":
    main()
