"""K1 single-photo pipeline. Model descriptions remain unverified evidence.

python3 app/photo_detect.py photo.jpg --out results/photo_001
Exit: 0 complete; 2 error/partial; 3 quality gate requests another photo.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import subprocess
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from photo_pipeline import run_file_pipeline


def clip_box(box, width, height):
    if len(box) != 4 or not all(math.isfinite(float(v)) for v in box):
        return None
    x1, y1, x2, y2 = map(float, box)
    result = [max(0, min(width, math.floor(x1))), max(0, min(height, math.floor(y1))),
              max(0, min(width, math.ceil(x2))), max(0, min(height, math.ceil(y2)))]
    return result if result[2] > result[0] and result[3] > result[1] else None


def describe(path, config):
    payload = {
        "model": config["vlm_model"], "temperature": 0, "stream": False,
        "max_tokens": config["vlm_max_tokens"],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": config["vlm_prompt"]},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," +
                base64.b64encode(Path(path).read_bytes()).decode("ascii")}},
        ]}],
    }
    request = urllib.request.Request(config["vlm_url"],
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    start = time.perf_counter()
    with urllib.request.urlopen(request, timeout=config["vlm_timeout"]) as response:
        raw = json.load(response)
    choice = raw["choices"][0]
    content = (choice["message"].get("content") or "").strip()
    if not content:
        raise ValueError("VLM returned no visible content")
    return {"status": "unverified", "text": content,
            "finish_reason": choice.get("finish_reason"), "usage": raw.get("usage"),
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 2)}


def detect(path, out, config):
    # Vendor runtime exists only on the board. Keep quality checks usable on PC.
    import cv2
    import numpy as np
    import onnxruntime as ort
    import spacemit_ort  # registers the vendor execution provider
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from k1_detect_image import COCO, letterbox, decode

    image = cv2.imread(str(path))
    if image is None:
        raise ValueError("Cannot decode preprocessed image")
    height, width = image.shape[:2]
    options = ort.SessionOptions()
    options.intra_op_num_threads = config["threads"]
    options.inter_op_num_threads = 1
    options.enable_profiling = True
    options.profile_file_prefix = str(out / "ort_profile")
    start = time.perf_counter()
    session = ort.InferenceSession(config["model"], sess_options=options,
                                  providers=["SpaceMITExecutionProvider"])
    init_ms = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    canvas, meta = letterbox(image)
    tensor = np.ascontiguousarray((cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
                                  .astype(np.float32) / 255).transpose(2, 0, 1)[None])
    before_infer = time.perf_counter()
    output = session.run(None, {session.get_inputs()[0].name: tensor})[0]
    after_infer = time.perf_counter()
    if output.shape != (1, 84, 8400):
        raise ValueError(f"Expected COCO YOLOv8 (1,84,8400), got {output.shape}")
    detections = []
    drawn = image.copy()
    for cls, score, box in sorted(decode(output, meta, config["confidence"], config["iou"]),
                                  key=lambda item: -item[1]):
        box = clip_box(box, width, height)
        if box is None:
            continue
        ident = f"det_{len(detections) + 1:03d}"
        det = {"id": ident, "class_id": cls, "label": COCO[cls],
               "confidence": round(score, 6), "box_xyxy": box,
               "status": "candidate", "coordinate_space": "processed.jpg"}
        if len(detections) < config["max_crops"]:
            x1, y1, x2, y2 = box
            padding = config["crop_padding"]
            crop_box = clip_box([x1-(x2-x1)*padding, y1-(y2-y1)*padding,
                                 x2+(x2-x1)*padding, y2+(y2-y1)*padding], width, height)
            a, b, c, d = crop_box
            crop_name = f"crop_{ident}.jpg"
            if not cv2.imwrite(str(out / crop_name), image[b:d, a:c]):
                raise OSError("Failed to write crop")
            det.update(crop=crop_name, crop_box_xyxy=crop_box)
        detections.append(det)
        x1, y1, x2, y2 = box
        cv2.rectangle(drawn, (x1, y1), (x2, y2), (0, 0, 255), 2)
        cv2.putText(drawn, f"{ident} {COCO[cls]} {score:.2f}", (x1, max(y1-6, 14)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
    end = time.perf_counter()
    if not cv2.imwrite(str(out / "annotated.jpg"), drawn):
        raise OSError("Failed to write annotated image")
    profile = Path(session.end_profiling())
    events = json.loads(profile.read_text())
    providers = {}
    for event in events:
        provider = event.get("args", {}).get("provider")
        if provider:
            providers[provider] = providers.get(provider, 0) + 1
    return {"model": config["model"],
            "model_sha256": hashlib.sha256(Path(config["model"]).read_bytes()).hexdigest(),
            "session_providers": session.get_providers(), "profile_node_events": providers,
            "output_shape": list(output.shape), "detections": detections,
            "timing_ms": {"session_init": round(init_ms, 2),
                "preprocess": round((before_infer-start)*1000, 2),
                "inference": round((after_infer-before_infer)*1000, 2),
                "decode_crop_draw": round((end-after_infer)*1000, 2)}}


def write_report(out, result):
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 照片检测链路结果", "", f"运行状态：{result['status']}", "",
             "风险结论：无法确认。检测框为候选目标；VLM 描述尚未核验。未检出不代表不存在或安全。", "",
             "当前使用 COCO 80 类预训练模型，尚未训练墙面开关、线缆等项目专用类别。", "",
             f"画质门禁：{result.get('photo', {}).get('status', 'error')}", ""]
    if result.get("detector"):
        lines += ["![检测框](annotated.jpg)", "", "## YOLO 候选目标", ""]
        for det in result["detector"]["detections"]:
            lines.append(f"- {det['id']}：{det['label']}，置信度 {det['confidence']:.3f}，框 {det['box_xyxy']}")
        if not result["detector"]["detections"]:
            lines.append("未检出达到阈值的候选目标。")
    lines += ["", "## VLM 原始描述（待核验）", ""]
    for observation in result["observations"]:
        lines += [f"### {observation['image']}", "", observation.get("text", observation.get("error", "未运行")), ""]
    if result["errors"]:
        lines += ["## 错误", ""] + [f"- {error}" for error in result["errors"]]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(image, out, config, skip_vlm=False, start_vlm=False):
    out = Path(out)
    # A run never overwrites previous evidence.
    out.mkdir(parents=True, exist_ok=False)
    start = time.perf_counter()
    result = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "status": "error", "risk_assessment": {"status": "unknown",
              "reason": "No independently verified risk facts or calibrated measurements"},
              "config": config, "observations": [], "errors": []}
    code = 2
    pid_file = out / "vlm_owned.pid"
    try:
        photo = run_file_pipeline([image], output_dir=str(out / "preprocessed"),
                                  thresholds=config["quality"])[0]
        result["photo"] = photo.to_dict()
        if photo.status != "pass":
            result["status"] = "quality_blocked" if photo.status in ("retry", "reject") else "error"
            code = 3 if result["status"] == "quality_blocked" else 2
        else:
            from shutil import copyfile
            copyfile(photo.output_path, out / "processed.jpg")
            config_path = out / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            # The vendor allocator holds TCM for the lifetime of a process.
            # Exit the YOLO worker before launching the VLM service.
            worker = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                str(out / "processed.jpg"), "--out", str(out), "--config", str(config_path),
                "--detect-worker"], capture_output=True, text=True)
            (out / "detector_stdout.txt").write_text(worker.stdout, encoding="utf-8")
            (out / "detector_stderr.txt").write_text(worker.stderr, encoding="utf-8")
            if worker.returncode:
                raise RuntimeError(f"YOLO worker exit {worker.returncode}: {worker.stderr[-1200:]}")
            result["detector"] = json.loads((out / "detector.json").read_text())
            result["vlm_model"] = config["vlm_model"]
            if not skip_vlm:
                if start_vlm:
                    startup = subprocess.run(["bash", str(Path(__file__).resolve().parents[1] / "ensure_vlm.sh"),
                        str(pid_file.resolve())], capture_output=True, text=True, timeout=55)
                    (out / "vlm_startup.txt").write_text(startup.stdout + startup.stderr, encoding="utf-8")
                    if startup.returncode:
                        raise RuntimeError("VLM startup failed; see vlm_startup.txt")
                images = ["processed.jpg"] + [d["crop"] for d in result["detector"]["detections"] if "crop" in d]
                for name in images:
                    print(f"VLM: {name}", flush=True)
                    try:
                        observation = describe(out / name, config)
                    except Exception as exc:
                        observation = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                        result["errors"].append(f"VLM {name}: {observation['error']}")
                    result["observations"].append({"image": name, **observation})
            result["status"] = "partial" if skip_vlm or result["errors"] else "complete"
            code = 0 if result["status"] == "complete" else 2
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        # Only stop the process created for this run, never another user's server.
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), 15)
            except ProcessLookupError:
                pass
    result["elapsed_ms"] = round((time.perf_counter()-start)*1000, 2)
    write_report(out, result)
    print(json.dumps({"status": result["status"], "out": str(out), "elapsed_ms": result["elapsed_ms"]}))
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--out", required=True)
    parser.add_argument("--config", default=str(Path(__file__).with_name("photo_detect_config.json")))
    parser.add_argument("--skip-vlm", action="store_true")
    parser.add_argument("--start-vlm", action="store_true", help="Start VLM after YOLO exits; stop the owned server afterwards")
    parser.add_argument("--detect-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.detect_worker:
        output = detect(args.image, Path(args.out), config)
        (Path(args.out) / "detector.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    return run(args.image, args.out, config, args.skip_vlm, args.start_vlm)


if __name__ == "__main__":
    raise SystemExit(main())
