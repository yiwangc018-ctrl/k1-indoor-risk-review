"""Verify copied image/response bindings; completion is separate from quality."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("results/k1_validation_20261103"))
    parser.add_argument("--require-finished", action="store_true")
    args = parser.parse_args()
    root = args.root
    remote = "/home/biandu/rv/validation_20261103/"
    hashes, missing, mismatches, counts, progress = {}, [], [], {}, {}
    stages = ["presence", "domain", "format", "text", "batch"]
    if (root / "batch_full").exists():
        stages.append("batch_full")
    for stage in stages:
        counts[stage] = 0
        file = root / stage / "progress.json"
        progress[stage] = json.loads(file.read_text(encoding="utf-8")) if file.exists() else {}
        for records in (root / stage).rglob("responses.jsonl"):
            for line in records.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                counts[stage] += 1
                if "image" not in row:
                    continue
                source = row["image"]
                if source.startswith(remote):
                    local = root / source[len(remote):]
                elif source.startswith("/home/biandu/k1demo/vlm_eval/images/"):
                    local = Path("results/vlm_eval/images") / Path(source).name
                else:
                    missing.append({"records": str(records), "source": source, "reason": "unmapped source path"})
                    continue
                key = str(local)
                if not local.is_file():
                    missing.append({"records": str(records), "source": source})
                    continue
                if key not in hashes:
                    hashes[key] = hashlib.sha256(local.read_bytes()).hexdigest()
                if hashes[key] != row["sha256"]:
                    mismatches.append({"records": str(records), "job": row.get("job_id"), "image": key})
    review = root / "domain_semantic_review.json"
    review_errors = []
    if review.exists():
        data = json.loads(review.read_text(encoding="utf-8"))
        rows = {x["job_id"]: x for x in map(json.loads,
            (root / "domain" / data["phase"] / "responses.jsonl").read_text(encoding="utf-8").splitlines())}
        for item in data["items"]:
            source = rows.get(item["job_id"], {})
            if (hashlib.sha256(source.get("text", "").encode()).hexdigest() != item["response_text_sha256"]
                    or source.get("sha256") != item["image_sha256"]):
                review_errors.append(item["job_id"])
    result = {"saved_requests": counts, "unique_image_files_verified": len(hashes),
        "missing_images": missing, "image_hash_mismatches": mismatches, "semantic_review_binding_errors": review_errors,
        "progress": {k: v.get("status", v.get("phase", "missing")) for k, v in progress.items()},
        "progress_source": "Last copied progress files, potentially stale during a network interruption.",
        "all_schedules_finished": all(v.get("status") == "schedule_finished" for v in progress.values()),
        "integrity_ok": not (missing or mismatches or review_errors),
        "scope": "Evidence integrity only. Does not accept visual accuracy, risk conclusions or physical hardware."}
    (root / "evidence_integrity.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("missing_images", "image_hash_mismatches", "scope")}, ensure_ascii=False))
    return 0 if result["integrity_ok"] and (not args.require_finished or result["all_schedules_finished"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
