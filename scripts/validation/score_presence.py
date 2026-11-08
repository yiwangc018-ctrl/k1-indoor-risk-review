"""Strict scoring of saved presence responses; abstentions remain explicit."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import statistics

FIELDS = ("toilet", "table")
STATES = {"present", "absent", "unknown"}


def parse_response(text):
    strict = True
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        # Removing one enclosing Markdown fence is formatting recovery, not
        # evidence correction. Keep its strict-format failure separate.
        match = re.fullmatch(r"\s*```(?:json)?\s*\n?(.*?)\n?```\s*", text or "", re.S)
        if not match:
            return {key: "invalid" for key in FIELDS}, False
        try:
            obj = json.loads(match.group(1))
            strict = False
        except (ValueError, TypeError):
            return {key: "invalid" for key in FIELDS}, False
    if isinstance(obj, list) and len(obj) == 1 and isinstance(obj[0], dict):
        obj = obj[0]
        strict = False
    if not isinstance(obj, dict):
        return {key: "invalid" for key in FIELDS}, False
    # In this existence-only task a JSON Boolean has an unambiguous mapping.
    # Do not extend it to risk severity, confidence, or "not visible" wording.
    values = {}
    for key in FIELDS:
        value = obj.get(key)
        if type(value) is bool:
            values[key] = "present" if value else "absent"
        else:
            values[key] = value if isinstance(value, str) and value in STATES else "invalid"
    return values, strict


def unrepaired_schema_valid(text):
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return False
    return (isinstance(obj, dict) and set(obj) == set(FIELDS)
            and all(isinstance(obj.get(k), str) and obj[k] in STATES for k in FIELDS))


def evaluate(rows, labels, policy=False, indoor_only=False):
    counts = {key: 0 for key in ("facts", "correct", "tp", "fp", "tn", "fn",
        "positive_total", "negative_total", "unknown", "invalid", "decided", "policy_unknown_total")}
    for row in rows:
        qid = row["image_id"]
        if qid not in labels or (indoor_only and qid in ("q13", "q14")):
            continue
        pred, _ = parse_response(row.get("text", ""))
        for key in FIELDS:
            truth = "unknown" if policy and qid in ("q13", "q14") else labels[qid][key]
            actual = pred[key]
            counts["facts"] += 1
            counts["correct"] += actual == truth
            counts["unknown"] += actual == "unknown"
            counts["invalid"] += actual == "invalid"
            counts["decided"] += actual in ("present", "absent")
            if truth == "unknown":
                counts["policy_unknown_total"] += 1
                continue
            counts["positive_total" if truth == "present" else "negative_total"] += 1
            if actual in ("present", "absent"):
                case = ("tp" if actual == "present" else "fn") if truth == "present" else ("fp" if actual == "present" else "tn")
                counts[case] += 1
    ratio = lambda a, b: a / b if b else None
    return {**counts,
        "accuracy": ratio(counts["correct"], counts["facts"]),
        "decided_coverage": ratio(counts["decided"], counts["facts"]),
        "precision_on_positive_claims": ratio(counts["tp"], counts["tp"] + counts["fp"]),
        "recall_with_abstentions_counted_as_missed": ratio(counts["tp"], counts["positive_total"]),
        "specificity_on_decided_negatives": ratio(counts["tn"], counts["tn"] + counts["fp"]),
        "negative_confident_correct_rate": ratio(counts["tn"], counts["negative_total"])}


def summarize(root, labels):
    result = {}
    for path in sorted(root.glob("*/responses.jsonl")):
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for variant in sorted({row["variant"] for row in rows}):
            part = [row for row in rows if row["variant"] == variant]
            times = sorted(row["elapsed_seconds"] for row in part)
            item = {"requests": len(part), "ok_requests": sum(row.get("ok", False) for row in part),
                "latency_seconds": {"median": statistics.median(times), "min": min(times), "max": max(times)},
                "download_or_unpack_overlap_requests": sum(row.get("concurrent_2b_download_phase") not in (None, "ready") for row in part)}
            item["output_limit_reached"] = sum(
                row.get("response", {}).get("choices", [{}])[0].get("finish_reason") == "length" for row in part)
            if variant in ("legacy_json", "clean_json", "cache_off", "schema_clean", "schema_short"):
                item.update(raw_presence=evaluate(part, labels), indoor_presence=evaluate(part, labels, indoor_only=True),
                            presence_with_out_of_domain_policy=evaluate(part, labels, policy=True),
                            out_of_domain_policy=evaluate([r for r in part if r["image_id"] in ("q13", "q14")], labels, policy=True),
                            bare_json_objects=sum(parse_response(row.get("text", ""))[1] for row in part),
                            unrepaired_schema_valid=sum(unrepaired_schema_valid(row.get("text", "")) for row in part))
            else:
                item["scoring_status"] = "manual semantic review required; not scored by keyword matching"
            result[path.parent.name + "/" + variant] = item
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("results/k1_validation_20261103/presence"))
    parser.add_argument("--labels", type=Path, default=Path("results/vlm_eval/labels.json"))
    parser.add_argument("--out", type=Path, default=Path("results/k1_validation_20261103/presence_summary.json"))
    args = parser.parse_args()
    labels = {x["qid"]: x for x in json.loads(args.labels.read_text(encoding="utf-8"))}
    result = summarize(args.root, labels)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for group, item in result.items():
        acc = item.get("indoor_presence", {}).get("accuracy")
        print(group, "n=" + str(item["requests"]), "indoor_accuracy=" + str(acc),
              "median_s=" + str(item["latency_seconds"]["median"]))


if __name__ == "__main__":
    main()
