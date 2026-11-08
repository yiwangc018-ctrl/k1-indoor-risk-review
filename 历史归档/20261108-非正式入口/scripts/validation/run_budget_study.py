"""Measure offline 4/6-photo computation with one detector and owned VLM.

Inputs are public photos from different scenes, not a real room scan. No camera
or servo is accessed. Model descriptions remain unverified candidate evidence.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def detect_worker(root, manifest, output):
    import cv2
    import numpy as np
    import onnxruntime as ort
    import spacemit_ort
    sys.path.insert(0, str(root / "source_scripts"))
    from k1_detect_image import letterbox, decode, COCO
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    options.enable_profiling = True
    options.profile_file_prefix = str(output / "yolo_profile")
    tick = time.perf_counter()
    model = "/home/biandu/k1demo/models/yolov8n.q.onnx"
    session = ort.InferenceSession(model, sess_options=options, providers=["SpaceMITExecutionProvider"])
    result = {"model": model, "init_seconds": time.perf_counter() - tick,
              "providers": session.get_providers(), "items": []}
    for photo in manifest:
        tick = time.perf_counter()
        image = cv2.imread(photo["image"])
        canvas, meta = letterbox(image)
        tensor = np.ascontiguousarray((cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255)
                                     .transpose(2, 0, 1)[None])
        before = time.perf_counter()
        output_tensor = session.run(None, {session.get_inputs()[0].name: tensor})[0]
        after = time.perf_counter()
        detections = [{"class": COCO[int(c)], "confidence": float(s), "box_xyxy": list(map(float, b))}
                      for c, s, b in decode(output_tensor, meta)]
        result["items"].append({"id": photo["id"], "detections": detections,
            "pre_seconds": before - tick, "inference_seconds": after - before,
            "total_seconds": time.perf_counter() - tick})
    profile = Path(session.end_profiling())
    providers = {}
    for event in json.loads(profile.read_text()):
        provider = event.get("args", {}).get("provider")
        if provider:
            providers[provider] = providers.get(provider, 0) + 1
    result["node_provider_events"] = providers
    save(output / "detector.json", result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--detector-worker", type=Path)
    parser.add_argument("--worker-out", type=Path)
    parser.add_argument("--output-name", default="batch")
    parser.add_argument("--inputs-manifest", type=Path)
    parser.add_argument("--require-all-usable", action="store_true")
    args = parser.parse_args()
    root = args.root
    if args.detector_worker:
        return detect_worker(root, json.loads(args.detector_worker.read_text()), args.worker_out)
    if Path(args.output_name).name != args.output_name or args.output_name in (".", ".."):
        parser.error("output-name must be one directory name")
    out = root / args.output_name
    out.mkdir(exist_ok=True)
    save(out / "progress.json", {"status": "waiting_for_text_study"})
    deadline = time.monotonic() + 21600
    while time.monotonic() < deadline:
        p = root / "text/progress.json"
        if p.exists() and json.loads(p.read_text()).get("status") == "schedule_finished":
            break
        time.sleep(5)
    else:
        save(out / "progress.json", {"status": "blocked", "reason": "prior study unfinished"})
        return
    sys.path.insert(0, str(root / "source_app"))
    from photo_pipeline import run_file_pipeline
    from run_board_study_followup import run_phase
    # The domain phase metadata contains the actual runtime selected on controls.
    model_meta = root / "domain/q2_selected_relations/metadata.json"
    if not model_meta.exists():
        save(out / "progress.json", {"status": "blocked", "reason": "no selected 2B runtime metadata"})
        return
    spec = json.loads(model_meta.read_text())["spec"]
    for key in ("runtime_candidates", "labels_file", "after_status_file"):
        spec.pop(key, None)
    plan = json.loads((args.inputs_manifest or root / "budget_inputs.json").read_text())
    summary = []
    for case, count, cap in (("four_all", 4, 4), ("six_all", 6, 6), ("six_cap_two", 6, 2)):
        folder = out / case
        folder.mkdir(exist_ok=True)
        save(out / "progress.json", {"status": "running", "case": case})
        start = time.perf_counter()
        photos = run_file_pipeline([x["image"] for x in plan[:count]], output_dir=str(folder / "preprocessed"))
        quality_seconds = time.perf_counter() - start
        usable = [{"id": plan[i]["id"], "image": photo.output_path} for i, photo in enumerate(photos) if photo.status == "pass"]
        save(folder / "photo_results.json", [photo.to_dict() for photo in photos])
        save(folder / "usable.json", usable)
        blocked = [{"id": plan[i]["id"], "status": photo.status,
                    "reasons": photo.to_dict().get("quality", {}).get("reasons", [])}
                   for i, photo in enumerate(photos) if photo.status != "pass"]
        if args.require_all_usable and len(usable) != count:
            save(out / "progress.json", {"status": "failed", "case": case,
                "reason": "Full-workload precondition failed; no VLM calls dispatched", "quality_blocked": blocked})
            return
        env = dict(os.environ, PYTHONPATH="/home/biandu/k1demo/pylib", OMP_NUM_THREADS="4", OPENBLAS_NUM_THREADS="1")
        tick = time.perf_counter()
        worker = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--root", str(root),
            "--detector-worker", str(folder / "usable.json"), "--worker-out", str(folder)],
            env=env, capture_output=True, text=True, timeout=180)
        detector_seconds = time.perf_counter() - tick
        (folder / "detector_stdout.txt").write_text(worker.stdout)
        (folder / "detector_stderr.txt").write_text(worker.stderr)
        # Budget cap is explicit; omitted images remain not_evaluated. This is
        # not claimed to be validated automatic selection or scene coverage.
        selected = usable if cap >= len(usable) else usable[-cap:]
        prompt = ("指出图片里最明显的一处物品摆放线索，描述具体物体和位置关系。"
                  "看不清就说无法确认，不猜尺寸、材质、重量或稳定性。最多两句。")
        jobs = [{"job_id": photo["id"], "image_id": photo["id"], "image": photo["image"],
            "variant": "budget", "prompt": prompt, "max_tokens": 64, "cache_prompt": False} for photo in selected]
        phase = dict(spec, id=case + "_vlm", variants=["budget"])
        tick = time.perf_counter()
        run_phase(phase, jobs, out, 8097)
        vlm_seconds = time.perf_counter() - tick
        responses = out / phase["id"] / "responses.jsonl"
        rows = [json.loads(line) for line in responses.read_text().splitlines()] if responses.exists() else []
        item = {"case": case, "input_images": count, "quality_passed": len(usable),
            "vlm_call_cap": cap, "actual_vlm_responses": len(rows), "quality_seconds": quality_seconds,
            "detector_process_seconds": detector_seconds, "detector_exit": worker.returncode,
            "selected_image_ids": [photo["id"] for photo in selected],
            "quality_blocked": blocked,
            "not_evaluated_due_to_cap": [photo["id"] for photo in usable if photo not in selected],
            "vlm_phase_seconds_including_startup_and_shutdown": vlm_seconds,
            "risk_assessment": "unknown; observations unverified",
            "scope": "offline images from different scenes; excludes camera, servo movement and SSH upload"}
        lines = ["# 离线照片计算预算验证", "", item["scope"], "",
            "风险判断：未验收。以下是模型候选观察，不能当作已确认风险。", ""]
        for row in rows:
            lines += ["## " + row["image_id"], "", row.get("text", row.get("error", "")), ""]
        unprocessed = [photo["id"] for photo in usable if photo not in selected]
        lines += ["未进入 VLM 的图片：" + str(unprocessed), ""]
        lines += ["画质或去重未通过：" + json.dumps(blocked, ensure_ascii=False), ""]
        (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")
        item["total_compute_and_report_seconds"] = time.perf_counter() - start
        save(folder / "summary.json", item)
        summary.append(item)
        save(out / "summary.json", summary)
        print(json.dumps(item), flush=True)
    save(out / "progress.json", {"status": "schedule_finished", "cases": len(summary)})


if __name__ == "__main__":
    main()
