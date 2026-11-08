"""K1 production VLM request runner, extracted from the historical study runner.

Only terminates the llama-server subprocess it creates. Raw responses and
resource/timing observations are committed after each request.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request


def write_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def read_memory(pid):
    result = {}
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in ("VmRSS", "VmHWM"):
                result[key + "_kib"] = int(value.split()[0])
    except (OSError, ValueError):
        pass
    return result


def system_snapshot():
    result = {"time": datetime.now(timezone.utc).isoformat()}
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith(("MemAvailable:", "MemFree:")):
            key, value = line.split(":", 1)
            result[key + "_kib"] = int(value.split()[0])
    result["temperature_millic"] = {
        str(p): int(p.read_text()) for p in Path("/sys/class/thermal").glob("thermal_zone*/temp")}
    return result


def http_json(url, payload=None, timeout=5):
    request = urllib.request.Request(url, data=None if payload is None else
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def prepare_config(spec, phase_dir):
    if "source_config" in spec:
        source = Path(spec["source_config"])
    else:
        roots = Path(spec["discover_dir"])
        candidates = sorted(roots.rglob("config.json"), key=lambda p: len(p.parts))
        if not candidates:
            raise FileNotFoundError("No model config under " + str(roots))
        source = candidates[0]
    config = json.loads(source.read_text())
    resolution = spec["resolution"]
    for key in ("vision_model", "text_model"):
        part = config[key]
        original = Path(part["model_path"])
        path = original if original.is_absolute() else (source.parent / original).resolve()
        if not path.exists():
            matches = list(source.parent.rglob(original.name))
            if len(matches) != 1:
                raise FileNotFoundError(str(path))
            path = matches[0]
        if key == "vision_model":
            current = str(part.get("input_size", 384))
            path = path.with_name(path.name.replace("-" + current + "-", "-" + str(resolution) + "-"))
            part["input_size"] = resolution
            if spec.get("modern_ep_config"):
                for old_key in ("spacemit_ep_intra_thread_num", "spacemit_ep_inter_thread_num"):
                    part.pop(old_key, None)
                part["ep_config"] = {"SPACEMIT_EP_INTRA_THREAD_NUM": "4",
                    "SPACEMIT_EP_INTER_THREAD_NUM": "1", "SPACEMIT_EP_INTRA_THREAD_AFFINITY": "0;1;2;3"}
        if not path.is_file():
            raise FileNotFoundError(str(path))
        part["model_path"] = str(path)
    write_json(phase_dir / "config.json", config)
    return source, config


def run_phase(spec, jobs, out, port):
    spec = dict(spec)
    phase_dir = out / spec["id"]
    phase_dir.mkdir(exist_ok=True)
    completed = set()
    records = phase_dir / "responses.jsonl"
    if records.exists():
        for line in records.read_text(encoding="utf-8").splitlines():
            if line.strip():
                completed.add(json.loads(line)["job_id"])
    selected = [j for j in jobs if j["variant"] in spec["variants"] and j["job_id"] not in completed]
    if not selected:
        return
    source, config = prepare_config(spec, phase_dir)
    url = f"http://127.0.0.1:{port}"
    identity = None
    reuse = False
    if spec.get("reuse_server") and spec.get("expected_model_path"):
        try:
            candidate = http_json(url + "/v1/models", timeout=3)
            if any(item.get("id") == spec["expected_model_path"] for item in candidate.get("data", [])):
                identity, reuse = candidate, True
        except (OSError, ValueError):
            reuse = False
    if not reuse:
        # Bind only the study's loopback port. Do not reuse or stop other services.
        import socket
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
    threads = str(spec.get("threads", 4))
    command = [spec.get("binary", "llama-server"), "-m", config["text_model"]["model_path"],
        "--media-backend", "smt", "--smt-config-dir", str(phase_dir),
        "-t", threads, "-tb", threads, "-c", "2048", "-np", "1",
        "--no-warmup", "--reasoning", "off", "--host", "127.0.0.1", "--port", str(port)]
    if spec.get("device"):
        command += ["--device", spec["device"]]
    else:
        command += ["-ngl", "0"]
    command += spec.get("extra_args", [])
    metadata = {"spec": spec, "source_config": str(source), "command": command,
        "before": system_snapshot(), "cache_policy": "explicit per request; default disabled",
        "text_model_bytes": Path(config["text_model"]["model_path"]).stat().st_size,
        "vision_model_bytes": Path(config["vision_model"]["model_path"]).stat().st_size}
    env = dict(os.environ, OMP_NUM_THREADS="4", OPENBLAS_NUM_THREADS="1", LC_ALL="C")
    env.update(spec.get("env", {}))
    proc = None
    stop_sampling = threading.Event()
    peak = {}

    def sample():
        while not stop_sampling.wait(0.5):
            for key, value in read_memory(proc.pid).items():
                peak[key] = max(peak.get(key, 0), value)

    start = time.perf_counter()
    with (phase_dir / "server.log").open("ab") as log:
        try:
            if reuse:
                metadata["reused_server"] = True
                metadata["startup_seconds"] = 0.0
                metadata["models_endpoint"] = identity
                write_json(phase_dir / "metadata.json", metadata)
            else:
                proc = subprocess.Popen(command, cwd=str(source.parent), stdout=log, stderr=log,
                                        env=env, start_new_session=True)
                (phase_dir / "owned_server.pid").write_text(str(proc.pid))
                sampler = threading.Thread(target=sample, daemon=True)
                sampler.start()
                while time.perf_counter() - start < 180:
                    if proc.poll() is not None:
                        raise RuntimeError(f"Server exited {proc.returncode}; inspect server.log")
                    try:
                        http_json(url + "/health")
                        break
                    except (OSError, ValueError):
                        time.sleep(1)
                else:
                    raise TimeoutError("VLM startup exceeded 180 s")
                metadata["startup_seconds"] = round(time.perf_counter() - start, 3)
                metadata["models_endpoint"] = http_json(url + "/v1/models")
                if spec.get("expected_model_path"):
                    reported = metadata["models_endpoint"].get("data", [])
                    if not any(item.get("id") == spec["expected_model_path"] for item in reported):
                        raise RuntimeError("Unexpected model identity: " + json.dumps(metadata["models_endpoint"]))
                write_json(phase_dir / "metadata.json", metadata)
            consecutive_errors = 0
            budget_limited = False
            for index, job in enumerate(selected):
                if spec.get("phase_budget_seconds") and time.perf_counter() - start >= spec["phase_budget_seconds"]:
                    budget_limited = True
                    break
                progress = {"phase": spec["id"], "job": job["job_id"],
                    "remaining_in_phase": len(selected) - index, "updated": system_snapshot()}
                write_json(out / "progress.json", progress)
                raw_image = Path(job["image"]).read_bytes()
                payload = {"model": "vlm", "temperature": 0, "stream": False,
                    "max_tokens": job.get("max_tokens", 64),
                    "messages": [{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," +
                            base64.b64encode(raw_image).decode("ascii")}},
                        {"type": "text", "text": job["prompt"]}]}]}
                if job.get("system_prompt"):
                    payload["messages"].insert(0, {"role": "system", "content": job["system_prompt"]})
                if job.get("cache_prompt") is not None:
                    payload["cache_prompt"] = job["cache_prompt"]
                if job.get("response_format"):
                    payload["response_format"] = job["response_format"]
                before = system_snapshot()
                tick = time.perf_counter()
                row = {"job_id": job["job_id"], "image_id": job["image_id"],
                    "variant": job["variant"], "prompt": job["prompt"],
                    "image": job["image"], "sha256": hashlib.sha256(raw_image).hexdigest(),
                    "max_tokens": payload["max_tokens"], "cache_prompt": job.get("cache_prompt"),
                    "response_format": job.get("response_format"),
                    "system_prompt": job.get("system_prompt"),
                    "before": before}
                try:
                    timeout = job.get("timeout_seconds", 300)
                    if spec.get("hard_phase_deadline"):
                        timeout = min(timeout, max(0.1, spec["phase_budget_seconds"] - (time.perf_counter() - start)))
                    response = http_json(url + "/v1/chat/completions", payload, timeout=timeout)
                    row["response"] = response
                    row["text"] = response["choices"][0]["message"].get("content", "")
                    row["ok"] = bool(row["text"].strip())
                    consecutive_errors = 0 if row["ok"] else consecutive_errors + 1
                except Exception as exc:
                    row.update(ok=False, error=type(exc).__name__ + ": " + str(exc))
                    consecutive_errors += 1
                row["elapsed_seconds"] = round(time.perf_counter() - tick, 3)
                row["after"] = system_snapshot()
                row["server_memory"] = read_memory(proc.pid) if proc is not None else {}
                with records.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                print(json.dumps({"phase": spec["id"], "job": job["job_id"],
                    "seconds": row["elapsed_seconds"], "ok": row["ok"],
                    "text": row.get("text", "")}, ensure_ascii=False), flush=True)
                if consecutive_errors >= 2 or (proc is not None and proc.poll() is not None):
                    raise RuntimeError("Repeated request failure or server exit; phase stopped")
            metadata["status"] = "budget_limited" if budget_limited else "complete"
        except Exception as exc:
            metadata.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
            print(json.dumps({"phase": spec["id"], "error": metadata["error"]}), flush=True)
        finally:
            if proc is not None and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
            stop_sampling.set()
            metadata.update(peak_server_memory=peak, after=system_snapshot(),
                            total_phase_seconds=round(time.perf_counter() - start, 3))
            write_json(phase_dir / "metadata.json", metadata)
