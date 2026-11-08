"""Board-local four-view risk-degree trial; raw answers remain unchanged."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def save(path, obj):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root
    out = root / "inference"
    out.mkdir(exist_ok=False)
    config = json.loads((root / "assets/runtime.example.json").read_text())
    manifest = json.loads((root / "assets/inputs.current_scene.json").read_text())
    save(root / "progress.json", {"status": "preprocessing", "started_utc": datetime.now(timezone.utc).isoformat()})
    from PIL import Image, ImageOps
    old = Path("/home/biandu/rv/validation_20261103")
    sys.path.insert(0, str(old / "source_app"))
    from photo_pipeline import quality_check
    prepared = []
    start = time.perf_counter()
    (root / "processed").mkdir()
    for view in manifest["views"]:
        source = root / "originals" / (view["view_id"] + ".jpeg")
        image_bytes = source.read_bytes()
        with Image.open(source) as probe:
            original_size = list(probe.size)
            orientation = probe.getexif().get(274, 1)
            image = ImageOps.exif_transpose(probe).convert("RGB")
        oriented_size = list(image.size)
        quality = quality_check(image)
        image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        target = root / "processed" / (view["view_id"] + ".jpg")
        image.save(target, quality=90)
        prepared.append({"id": view["view_id"], "image": str(target),
            "original_sha256": hashlib.sha256(image_bytes).hexdigest(),
            "processed_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "original_size": original_size, "exif_orientation": orientation,
            "oriented_size": oriented_size, "processed_size": list(image.size),
            "quality_status_advisory_only": quality.status,
            "quality_metrics": quality.metrics, "quality_reasons": quality.reasons})
    save(root / "preprocess.json", prepared)
    save(root / "usable.json", prepared)
    preprocess_seconds = time.perf_counter() - start
    save(root / "progress.json", {"status": "detector", "views": len(prepared)})
    (root / "detector").mkdir()
    env = dict(os.environ, PYTHONPATH="/home/biandu/k1demo/pylib", OMP_NUM_THREADS="4", OPENBLAS_NUM_THREADS="1")
    tick = time.perf_counter()
    worker = subprocess.run([sys.executable, str(old / "run_budget_study_full.py"), "--root", str(old),
        "--detector-worker", str(root / "usable.json"), "--worker-out", str(root / "detector")],
        env=env, capture_output=True, text=True, timeout=120)
    detector_seconds = time.perf_counter() - tick
    (root / "detector/stdout.txt").write_text(worker.stdout)
    (root / "detector/stderr.txt").write_text(worker.stderr)
    # Grade the images directly; do not leak human reference or detector labels.
    system = (root / "assets/system_prompt.zh.txt").read_text()
    prompt_template = (root / "assets/view_prompt.zh.txt").read_text()
    schema = json.loads((root / "assets/view_response.schema.json").read_text())
    jobs = []
    for index, photo in enumerate(prepared, 1):
        prompt = prompt_template.format(view_index=index, view_count=len(prepared), view_id=photo["id"],
            scene_hint=manifest["scene_hint"], detector_candidates_or_none="本次不提供自动候选，请直接观察图片")
        jobs.append({"job_id": photo["id"], "image_id": photo["id"], "image": photo["image"],
            "variant": "risk_degree", "prompt": prompt, "system_prompt": system,
            "max_tokens": config["model"]["max_tokens"], "timeout_seconds": config["model"]["request_timeout_seconds"],
            "cache_prompt": False, "response_format": {"type": "json_object", "schema": schema}})
    save(root / "jobs.json", jobs)
    model = config["model"]
    original_config = json.loads(Path(model["model_config"]).read_text())
    remaining = config["inference"]["wall_budget_seconds"][str(len(prepared))] - (time.perf_counter() - start)
    if remaining <= 0:
        save(root / "progress.json", {"status": "failed", "reason": "budget exhausted before VLM"})
        return
    spec = {"id": "q2_risk_degree_v1", "source_config": model["model_config"],
        "expected_model_path": original_config["text_model"]["model_path"],
        "binary": model["binary"], "env": model["env"], "resolution": 384, "threads": 4,
        "variants": ["risk_degree"], "phase_budget_seconds": remaining, "hard_phase_deadline": True}
    save(root / "schedule.json", [spec])
    save(root / "progress.json", {"status": "inference", "views": len(jobs)})
    from run_board_study import run_phase
    tick = time.perf_counter()
    run_phase(spec, jobs, out, 8099)
    model_seconds = time.perf_counter() - tick
    meta = json.loads((out / spec["id"] / "metadata.json").read_text())
    responses = out / spec["id"] / "responses.jsonl"
    rows = [json.loads(x) for x in responses.read_text().splitlines()] if responses.exists() else []
    summary = {"task_version": "risk_degree_v1", "model": "Qwen3.5-2B", "vision_input_size": 384,
        "input_views": len(prepared), "saved_responses": len(rows), "phase_status": meta["status"],
        "preprocess_seconds": preprocess_seconds, "detector_seconds": detector_seconds,
        "detector_exit": worker.returncode, "vlm_seconds_including_startup_shutdown": model_seconds,
        "total_compute_seconds": time.perf_counter() - start,
        "model_source": "actual models endpoint and command in inference metadata",
        "scope": "One real room, four supplied phone views; excludes camera, gimbal and upload. No human grade or detector label supplied to VLM."}
    save(root / "summary.json", summary)
    save(root / "progress.json", {"status": "finished", "summary": summary})
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
