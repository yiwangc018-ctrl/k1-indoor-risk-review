"""Test existing schema-constrained decoding without a second text model."""
import argparse
import json
from pathlib import Path
import time


def save(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root
    out = root / "format"
    out.mkdir(exist_ok=True)
    save(out / "progress.json", {"status": "waiting_for_domain_study"})
    deadline = time.monotonic() + 18000
    while time.monotonic() < deadline:
        p = root / "domain/progress.json"
        if p.exists() and json.loads(p.read_text()).get("status") == "schedule_finished":
            break
        time.sleep(5)
    else:
        save(out / "progress.json", {"status": "schedule_finished", "error": "prior study unfinished; no tests run"})
        return
    from run_board_study_format import run_phase
    presence = json.loads((root / "jobs_presence.json").read_text())
    domain = json.loads((root / "jobs_followup.json").read_text())
    mapping = json.loads((root / "domain_blind_map.json").read_text())
    schema = {"type": "object", "properties": {key: {"type": "string", "enum": ["present", "absent", "unknown"]}
              for key in ("toilet", "table")}, "required": ["toilet", "table"], "additionalProperties": False}
    response_format = {"type": "json_object", "schema": schema}
    jobs = []
    for job in presence:
        if job["variant"] == "clean_json":
            jobs.append(dict(job, job_id="schema_clean_" + job["image_id"], variant="schema_clean", response_format=response_format))
        if job["variant"] == "legacy_json":
            jobs.append(dict(job, job_id="schema_short_" + job["image_id"], variant="schema_short",
                prompt="图中能看到马桶吗？能看到桌子吗？厨房操作台不算桌子。看不清或不是室内场景时写unknown。",
                max_tokens=48, cache_prompt=False, response_format=response_format))
    spatial_schema = {"type": "object", "properties": {"observation": {"type": "string"},
        "status": {"type": "string", "enum": ["signal", "none", "unknown"]}},
        "required": ["observation", "status"], "additionalProperties": False}
    target_ids = {key for key, value in mapping.items() if value["eval_id"].startswith(("trip_01", "stack_04"))}
    for job in domain:
        if job["variant"] == "domain_relation" and job["image_id"] in target_ids:
            jobs.append(dict(job, job_id="schema_relation_" + job["image_id"], variant="schema_relation",
                prompt=job["prompt"] + " 用observation简述可见依据；status只表达可见线索(signal)、检查范围未见明显线索(none)或证据不足(unknown)，不表示房间安全。",
                response_format={"type": "json_object", "schema": spatial_schema}))
    save(out / "jobs.json", jobs)
    issues = []
    for model, variants in (("q2", ["schema_clean", "schema_short", "schema_relation"]), ("q08", ["schema_clean"])):
        source = root / "domain" / (model + "_selected_relations") / "metadata.json"
        if not source.exists():
            issues.append(model + ": no selected runtime metadata")
            continue
        spec = json.loads(source.read_text())["spec"]
        for key in ("runtime_candidates", "labels_file", "after_status_file"):
            spec.pop(key, None)
        spec.update(id=model + "_schema", resolution=384, variants=variants)
        try:
            run_phase(spec, jobs, out, 8098)
        except Exception as exc:
            issues.append(model + ": " + type(exc).__name__ + ": " + str(exc))
    # Bounded larger-model ceiling check. No further model sizes are scheduled.
    source = root / "domain/q2_selected_relations/metadata.json"
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        statefile = root / "models_4b/download_status.json"
        state = json.loads(statefile.read_text()) if statefile.exists() else {}
        if state.get("phase") in ("ready", "failed"):
            break
        save(out / "progress.json", {"status": "waiting_for_4b_model"})
        time.sleep(5)
    if source.exists() and state.get("phase") == "ready":
        spec = json.loads(source.read_text())["spec"]
        for key in ("runtime_candidates", "labels_file", "after_status_file", "source_config", "discover_dir"):
            spec.pop(key, None)
        spec.update(id="q4_schema", resolution=384, variants=["schema_clean", "schema_relation"],
                    discover_dir=str(root / "models_4b/extracted"), phase_budget_seconds=480)
        four = [dict(job, timeout_seconds=180) for job in jobs if job["variant"] in spec["variants"]]
        priority = {"schema_clean_q01":0,"schema_clean_q09":1}
        # Include an obvious placement pair before spending the rest on facts.
        for job in four:
            ident = job["image_id"]
            if ident in mapping and mapping[ident]["eval_id"].startswith("trip_01"):
                priority[job["job_id"]] = 2 if not mapping[ident]["dataset_safe_label"] else 3
            if ident in mapping and mapping[ident]["eval_id"].startswith("stack_04"):
                priority[job["job_id"]] = 4 if not mapping[ident]["dataset_safe_label"] else 5
        four.sort(key=lambda job: priority.get(job["job_id"], 6))
        try:
            run_phase(spec, four, out, 8098)
        except Exception as exc:
            issues.append("q4: " + type(exc).__name__ + ": " + str(exc))
    else:
        issues.append("q4: model unavailable; " + str(state.get("error", state.get("phase"))))
    save(out / "progress.json", {"status": "schedule_finished", "issues": issues})


if __name__ == "__main__":
    main()
