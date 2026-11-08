"""K1 capture, voice trigger, visual review and round state management.

Run from the assembled runtime containing app/, configs/ and scripts/board/.
Only a round with READY.json is eligible for inference. Original images stay intact.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
from vlm_runtime import run_phase
from risk_degree import parse_coarse, overall_level, LABELS, LEVELS

ADVICE_BY_KIND = {
    "movement": "建议先把挡住行走、起身或取物路线的物品移开，留出一条能顺畅经过的通道。",
    "placement": "建议先把该区域的物品归位，常用的放在顺手处，给日常操作和取物留出空间。",
    "collision": "建议把朝活动区域突出的边角物品收进去或挪开，必要时加防护，给经过时留出避让空间。",
}

# Recorded in analysis.json so a wording change can never be mistaken for a model change.
REPORTING_REVISION = {
    "date": "2026-11-06",
    "raw_model_output_changed": False,
    "reason": "Advice sentences are complete sentences joined by punctuation; fixes the run-on line in report.md.",
}

# 语音触发的门控与判定口径见 docs/语音触发与报告润色决策-20261106.md §2。
VERDICTS = ("accepted", "rejected", "voided", "uncertain")


class ResourceBusyError(RuntimeError):
    pass


def write_json(path, data):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


@contextmanager
def exclusive(path):
    """Kernel releases locks even if the process crashes. K1/Linux only."""
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ResourceBusyError(f"另一项任务正在使用资源：{path}")
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def list_sinks():
    """当前 PipeWire 的播放设备名列表。

    pw-dump 的输出不一定合法 UTF-8（设备描述里有 GBK 字节），必须按字节读再容错解码，
    否则 text=True 会直接抛 UnicodeDecodeError。
    """
    result = subprocess.run(["pw-dump"], check=True, capture_output=True, timeout=5)
    raw = result.stdout if isinstance(result.stdout, bytes) else str(result.stdout).encode("utf-8")
    nodes = json.loads(raw.decode("utf-8", "replace"))
    sinks = []
    for node in nodes:
        props = node.get("info", {}).get("props", {})
        if props.get("media.class") == "Audio/Sink":
            sinks.append(props.get("node.name", ""))
    return [name for name in sinks if name]


def resolve_sink(config):
    """按「精确 target → 同设备 profile 变体 → 名称包含 → 板载回退」找播放设备。

    只认写死的节点名时，换口/重插/换 profile 就找不到；设备名里的型号与序列号是稳定的，
    所以补一层名称包含匹配（config.audio.match.name_contains）。
    板载回退默认关闭；本机 USB 音响未接但板载 ES8326 有输出时，
    config.audio.allow_onboard_fallback=true 才允许回退，仍然优先 USB。
    """
    audio = config["audio"]
    wanted = audio.get("target")
    contains = [item.lower() for item in (audio.get("match") or {}).get("name_contains", [])]
    sinks = list_sinks()
    if wanted and wanted in sinks:
        return wanted
    if wanted:
        base = wanted.rsplit(".", 1)[0] + "."
        variants = [item for item in sinks if item.startswith(base)]
        if len(variants) == 1:
            return variants[0]
    if contains:
        exact = [item for item in sinks if all(word in item.lower() for word in contains)]
        if len(exact) == 1:
            return exact[0]
        loose = [item for item in sinks if any(word in item.lower() for word in contains)]
        if len(loose) == 1:
            return loose[0]
        if len(loose) > 1:
            raise RuntimeError(f"匹配到多个疑似音箱，无法确定：{loose}")
    if audio.get("allow_onboard_fallback"):
        onboard_words = [item.lower() for item in audio.get("onboard_contains", [])]
        onboard = [item for item in sinks
                   if any(word in item.lower() for word in onboard_words)]
        if len(onboard) == 1:
            return onboard[0]
    raise RuntimeError(f"未找到 USB 音箱；当前播放设备：{sinks or '无'}")


def usb_playback_card(config):
    """按 USB ID 找一张能播放的 USB 声卡，返回卡号字符串（如 "3"）；找不到返回 None。"""
    audio = config["audio"]
    wanted = [item.lower() for item in (audio.get("match") or {}).get("usb_ids", [])]
    if not wanted:
        return None
    for card in sorted(Path("/sys/class/sound").glob("card[0-9]*")):
        device = (card / "device").resolve()
        for parent in (device, *device.parents):
            vendor, product = parent / "idVendor", parent / "idProduct"
            if vendor.is_file() and product.is_file():
                usb_id = f"{vendor.read_text().strip()}:{product.read_text().strip()}".lower()
                if usb_id in wanted and Path(f"/proc/asound/{card.name}/pcm0p").exists():
                    return card.name[4:]
    return None


def alsa_usb_playback(config):
    """ALSA 直推的设备名（plughw:<card>,0）。

    这块 USB 音箱在 WirePlumber 里默认只有输入 profile（没有 Audio/Sink），切到
    pro-audio 才出现播放节点；profile 可能被重置，所以保留 ALSA 直推兜底。
    """
    card = usb_playback_card(config)
    return f"plughw:{card},0" if card else None


def set_player_volume(config):
    """把 USB 声卡的 PCM 混音拉到配置音量（默认 100%）。

    实测这块卡出厂 PCM 只有 40%，提示音很轻；ALSA 混音在重启后不一定保留，
    所以每次播放前设一次（失败不致命）。
    """
    audio = config["audio"]
    percent = audio.get("volume_percent")
    if percent is None:
        return None
    card = usb_playback_card(config)
    if card is None:
        return None
    try:
        subprocess.run(["amixer", "-c", card, "sset", "PCM", f"{int(percent)}%"],
                       check=True, capture_output=True, timeout=5)
        return card
    except (OSError, subprocess.SubprocessError):
        return None


def announce(config, name, events):
    """播一个提示音，返回本次事件（played / skipped / error）。

    用户 2026-11-06 定的口径：全程只有"开始工作"这一声嘀嘀。`audio.enabled_cues`
    默认就是 ["start"]；不在名单里的播报点直接跳过，不算失败，也不写 events。
    播放优先走 PipeWire 节点，没有节点时按 USB ID 直推 ALSA；**永不退回板载声卡**。
    """
    audio = config["audio"]
    if not audio.get("enabled", True):
        return None
    allowed = audio.get("enabled_cues")
    if allowed is not None and name not in allowed:
        return {"audio": name, "status": "skipped", "time": now()}
    wav = Path(audio["directory"]) / (name + ".wav")
    try:
        if not wav.is_file():
            raise FileNotFoundError(f"提示音缺失：{wav}（运行 scripts/board/make_cues.py 生成）")
        card = set_player_volume(config)
        attempts = max(1, int(audio.get("enumerate_attempts", 4)))
        interval = float(audio.get("enumerate_interval_seconds", 0.5))
        target = None
        last_error = None
        for attempt in range(attempts):
            try:
                target = resolve_sink(config)
                break
            except RuntimeError as exc:
                last_error = exc
                if attempt < attempts - 1:
                    time.sleep(interval)  # 音频图在登录/重插后需要一点时间枚举
        if target:
            pct = audio.get("volume_percent")
            if pct is not None:
                try:
                    subprocess.run(["pactl", "set-sink-volume", target, f"{int(pct)}%"],
                                   check=False, capture_output=True, timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
            onboard_words = [item.lower() for item in audio.get("onboard_contains", [])]
            if audio.get("allow_onboard_fallback") and any(word in target.lower() for word in onboard_words):
                try:
                    subprocess.run(["pactl", "set-sink-volume", target,
                                    f"{int(audio.get('onboard_volume_percent', 100))}%"],
                                   check=False, capture_output=True, timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
            subprocess.run(["pw-play", "--target", target, str(wav)],
                           check=True, capture_output=True, timeout=10)
            event = {"audio": name, "status": "played", "target": target, "time": now()}
        else:
            device = alsa_usb_playback(config)
            if not device:
                raise last_error or RuntimeError("未找到 USB 音箱")
            subprocess.run(["aplay", "-D", device, str(wav)],
                           check=True, capture_output=True, timeout=10)
            event = {"audio": name, "status": "played", "alsa": device, "time": now()}
        if card is not None:
            event["volume_card"] = card
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        event = {"audio": name, "status": "error", "error": str(exc), "time": now()}
        print(f"音响提示失败：{exc}", flush=True)
    events.append(event)
    return event


def audio_check(config, play=False):
    """设备自检：列出播放设备、给出匹配结果、写 audio_state.json，可选试播一声。"""
    inbox = Path(config["inbox"])
    state_file = inbox.parent / "audio_state.json"
    state = {"time": now(), "wav_dir": config["audio"].get("directory"), "played": False}
    try:
        state["sinks"] = list_sinks()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        state["sinks"] = []
        state["sinks_error"] = f"{type(exc).__name__}: {exc}"
    try:
        state["target"] = resolve_sink(config)
        state["ok"] = True
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        state["ok"] = False
        state["error"] = f"{type(exc).__name__}: {exc}"
        state["alsa"] = alsa_usb_playback(config)
    if play:
        state["volume_card"] = set_player_volume(config)
        events = []
        event = announce(config, "start", events)
        state["played"] = bool(event) and event.get("status") == "played"
        state["play_event"] = event
    # 退出码看"能不能出声"，不看有没有 PipeWire sink：输入-only 档位下 ALSA 直推仍然可用。
    state["playable"] = state["played"] if play else bool(state["ok"] or state.get("alsa"))
    write_json(state_file, state)
    print(f"播放设备：{state['sinks'] or '无'}")
    if state["ok"]:
        print(f"匹配结果：{state['target']}")
    else:
        fallback = state.get("alsa")
        print(f"匹配结果：不可用 — {state.get('error', '')}")
        print(f"ALSA 兜底：{fallback or '无（该 USB 设备没有可播放声卡）'}")
    if play:
        print(f"试播：{'成功（嘀嘀）' if state['played'] else '失败 — ' + str((state.get('play_event') or {}).get('error'))}")
    print(f"设备状态已写：{state_file}")
    return state


def capture_image(config, target, log_path=None):
    """Capture one JPEG through v4l2-ctl, verify it decodes, move it into place.

    Shared by the fixed/manual round and the gimbal scan so both paths use the same
    format, verification and timing. Returns {"size", "seconds"}.
    """
    from PIL import Image
    camera = config["camera"]
    target = Path(target)
    temp = target.with_suffix(".mjpg.part")
    tick = time.perf_counter()
    command = ["v4l2-ctl", "-d", camera["device"],
               f"--set-fmt-video=width={camera['width']},height={camera['height']},pixelformat=MJPG",
               "--stream-mmap", "--stream-count=1", "--stream-to=" + str(temp)]
    result = subprocess.run(command, capture_output=True, timeout=camera["timeout_seconds"])
    if log_path is not None:
        Path(log_path).write_bytes(result.stdout + result.stderr)
    result.check_returncode()
    with Image.open(temp) as probe:
        probe.verify()
    with Image.open(temp) as probe:
        size = list(probe.size)
    temp.replace(target)
    return {"size": size, "seconds": round(time.perf_counter() - tick, 3)}


def capture_round(config, views=1, manual=False, trigger=None, publish_ready=True):
    if not 1 <= views <= 6:
        raise ValueError("每轮允许 1–6 张照片")
    if views > 1 and not manual:
        raise ValueError("没有云台：多张拍摄请使用 --manual，逐张手动换方向")
    from PIL import Image
    inbox = Path(config["inbox"])
    folder = inbox / ("round_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
    originals = folder / "originals"
    originals.mkdir(parents=True)
    meta = {"version": 1, "round_id": folder.name, "status": "capturing", "created_at": now(),
            "capture_mode": "manual_views" if manual else "fixed_view", "requested_views": views,
            "coverage": "仅本轮照片可见区域；无云台、无测距、无自动覆盖保证",
            "trigger": trigger or {"kind": "manual"}, "views": [], "events": []}
    write_json(folder / "meta.json", meta)
    camera = config["camera"]
    try:
        with exclusive(inbox / ".camera.lock"):
            announce(config, "start", meta["events"])
            for i in range(1, views + 1):
                ident = f"view_{i:02d}"
                if manual:
                    answer = input(f"请调整第 {i}/{views} 个拍摄方向，保持稳定后按回车（q 取消）：")
                    if answer.strip().lower() == "q":
                        raise RuntimeError("用户取消本轮拍摄")
                time.sleep(camera["settle_seconds"])
                target = originals / (ident + ".jpg")
                captured = capture_image(config, target, folder / (ident + "_capture.log"))
                meta["views"].append({"view_id": ident, "file": target.relative_to(folder).as_posix(),
                    "size": captured["size"], "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "captured_at": now(), "seconds": captured["seconds"], "pose": None})
                announce(config, "capture_ok", meta["events"])
                write_json(folder / "meta.json", meta)
                print(f"照片已保存：{target}", flush=True)
        meta.update(status="captured", completed_at=now())
        write_json(folder / "meta.json", meta)
        # This atomic manifest is published last. Watchers never infer completeness from silence.
        # publish_ready=False defers it, so a voice trigger that is later rejected never reaches
        # the model: a round without READY.json is not eligible for inference (see §7 of the design).
        if publish_ready:
            write_manifest(folder, meta)
    except BaseException as exc:
        meta.update(status="capture_failed", error=f"{type(exc).__name__}: {exc}")
        write_json(folder / "meta.json", meta)
        raise
    return folder


def write_manifest(folder, meta):
    write_json(Path(folder) / "READY.json", {"version": 1, "round_id": Path(folder).name,
                                             "views": meta["views"]})


def publish_ready(folder):
    """Publish READY.json for an already captured round; the only way into inference."""
    folder = Path(folder)
    meta = load(folder / "meta.json")
    if meta.get("status") != "captured":
        raise ValueError(f"只有状态为 captured 的轮次才能发布 READY，当前为 {meta.get('status')}")
    write_manifest(folder, meta)
    return folder / "READY.json"


def decide_verdict(gate, accepted):
    """语音触发的判定：同一套规则，两个门控档位。门控见决策文档 §2。"""
    if gate == "verified":
        return "accepted" if accepted else "rejected"
    if gate == "optimistic":
        return "accepted" if accepted else "voided"
    raise ValueError(f"未知门控档位：{gate}")


def void_round(folder, verdict="voided", reason="未确认的语音触发"):
    """作废一轮：不发布 READY.json（因此不会被分析），照片与元数据全部保留为证据。"""
    if verdict not in ("voided", "uncertain"):
        raise ValueError("作废只会是 voided 或 uncertain；rejected 表示根本没有拍")
    folder = Path(folder)
    if (folder / "READY.json").exists():
        raise RuntimeError("本轮已发布 READY，不能作废；需要人工处理")
    meta = load(folder / "meta.json")
    trigger = dict(meta.get("trigger") or {})
    trigger.update(verdict=verdict, reason=reason, decided_at=now())
    meta.update(status="voided", trigger=trigger, voided_at=now(), void_reason=reason)
    write_json(folder / "meta.json", meta)
    return meta


def ready_views(folder):
    manifest = load(folder / "READY.json")
    views = manifest.get("views")
    if not isinstance(views, list) or not 1 <= len(views) <= 6:
        raise ValueError("READY.json 必须列出 1–6 张已写完的照片")
    seen = set()
    output = []
    for view in views:
        ident = view["view_id"]
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ident) or ident in seen:
            raise ValueError("照片编号非法或重复")
        seen.add(ident)
        image = (folder / view["file"]).resolve()
        if not image.is_relative_to(folder.resolve()) or not image.is_file():
            raise ValueError("照片必须位于本轮目录内")
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        if digest != view["sha256"]:
            raise ValueError(f"照片未完整写入或已改变：{ident}")
        output.append({**view, "path": image})
    return output


def summarize(views, capture_count, events):
    usable = [v for v in views if v.get("usable")]
    priorities = []
    for view in sorted(usable, key=lambda v: LEVELS[v["level"]], reverse=True):
        for focus in view.get("focus", []):
            if view["level"] == "low":
                continue
            # Without camera pose we cannot establish cross-view object identity.
            priorities.append({**focus, "view_id": view["view_id"], "level": view["level"],
                               "suggestion": ADVICE_BY_KIND[focus["kind"]], "suggestion_source": "rule_by_focus_kind"})
    return {"version": 1, "status": "complete" if len(usable) == capture_count else "partial" if usable else "failed",
            "overall_level": overall_level(views), "assessed_views": len(usable), "input_views": capture_count,
            "scope": "本轮可见区域的模型初评；等级为整理优先程度，不是事故概率。",
            "coverage_complete": False, "views": views, "priority_regions": priorities[:3],
            "grouping": "按照片列候选；未证明不同照片的区域互不重复", "events": events, "finished_at": now(),
            "reporting_revision": dict(REPORTING_REVISION)}


def _sentence(text):
    """Terminate a clause so two Chinese sentences never run together in report.md."""
    text = (text or "").strip()
    if not text:
        return ""
    return text if text[-1] in "。！？…；;" else text + "。"


def report(folder, result):
    write_json(folder / "analysis.json", result)
    level = LABELS[result["overall_level"]]
    lines = ["# 可见区域风险程度初评", "", f"状态：{result['status']}；有效照片 {result['assessed_views']}/{result['input_views']}。",
             f"关注程度：**{level}**。", "", result["scope"], "", "仅评价本轮照片可见区域。", ""]
    for v in result["views"]:
        lines += [f"## {v['view_id']}", "", v.get("reason") or v.get("error", "无法评估"), ""]
    if result["priority_regions"]:
        lines += ["## 整理建议（供参考）", ""]
    for p in result["priority_regions"]:
        where = (p.get("where") or "可见区域").strip()
        lines += [f"- {p['view_id']} · {where}：{_sentence(p.get('basis'))}{p['suggestion']}"]
    (folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def available_memory_mb():
    info = Path("/proc/meminfo")
    if not info.is_file():
        return None
    for line in info.read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return None


def analyze_round(config, folder):
    from PIL import Image, ImageOps
    import photo_pipeline
    folder = Path(folder).resolve()
    # One model job for all rounds, even when watchers and manual invocations overlap.
    with exclusive(Path(config["inbox"]) / ".inference.lock"):
        if (folder / "analysis.json").exists():
            return load(folder / "analysis.json")
        inputs = ready_views(folder)
        attempt = folder / ("analysis_" + uuid.uuid4().hex[:8])
        attempt.mkdir()
        events, jobs, failures = [], [], []
        write_json(folder / "analysis_status.json", {"status": "processing", "attempt": attempt.name, "time": now()})
        announce(config, "analyzing", events)
        prompt_dir = Path(config.get("prompts_dir", "configs/risk_assessment_v1_08b"))
        prompts = prompt_dir if prompt_dir.is_absolute() else ROOT / prompt_dir
        system = (prompts / "system_prompt.zh.txt").read_text(encoding="utf-8-sig")
        template = (prompts / "view_prompt.zh.txt").read_text(encoding="utf-8-sig")
        schema = load(prompts / "view_response.schema.json")
        for i, view in enumerate(inputs, 1):
            ident = view["view_id"]
            try:
                with Image.open(view["path"]) as source:
                    image = ImageOps.exif_transpose(source).convert("RGB")
                quality = photo_pipeline.quality_check(image)
                image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                processed = attempt / (ident + ".jpg")
                image.save(processed, quality=90)
                prompt = template.format(view_index=i, view_count=len(inputs), view_id=ident,
                    scene_hint="室内活动和收纳空间", detector_candidates_or_none="无；直接观察图片")
                jobs.append({"job_id": ident, "image_id": ident, "image": str(processed), "variant": "risk_degree",
                    "prompt": prompt, "system_prompt": system, "max_tokens": config["model"]["max_tokens"],
                    "timeout_seconds": config["model"]["request_timeout_seconds"], "cache_prompt": False,
                    "response_format": {"type": "json_object", "schema": schema}})
                events.append({"view_id": ident, "quality": quality.status,
                               "quality_reasons": list(quality.reasons), "policy": "advisory_only"})
            except Exception as exc:
                failures.append({"view_id": ident, "usable": False, "error": f"照片解码失败：{exc}"})
        model = config["model"]
        phase = attempt / "inference"
        phase.mkdir()
        spec = {"id": "risk_degree", "source_config": model["model_config"], "binary": model["binary"],
                "env": model["env"], "resolution": model.get("resolution", 384),
                "threads": model.get("threads", 4), "variants": ["risk_degree"],
                "reuse_server": model.get("reuse_server", True),
                "expected_model_path": model["expected_model_path"]}
        write_json(attempt / "jobs.json", jobs)
        phase_error = None
        if jobs:
            available = available_memory_mb()
            events.append({"available_memory_mb_before_vlm": available})
            if available is not None and available < config.get("minimum_available_memory_mb", 4096):
                phase_error = f"可用内存仅 {available} MB，未启动模型；请先检查占用内存的服务"
            else:
                try:
                    print(f"开始分析 {len(jobs)} 张照片，请稍候。", flush=True)
                    with (attempt / "runner.log").open("w", encoding="utf-8", buffering=1) as log, redirect_stdout(log):
                        run_phase(spec, jobs, phase, config["port"])
                except Exception as exc:
                    phase_error = f"{type(exc).__name__}: {exc}"
        raw = phase / "risk_degree/responses.jsonl"
        rows = [json.loads(line) for line in raw.read_text(encoding="utf-8").splitlines() if line.strip()] if raw.exists() else []
        by_id = {row["image_id"]: row for row in rows}
        views = list(failures)
        for job in jobs:
            row = by_id.get(job["image_id"], {})
            parsed = parse_coarse(row.get("text", "")) if row.get("ok") else {
                "usable": False, "error": row.get("error") or phase_error or "模型未返回有效结果，详见推理日志"}
            views.append({"view_id": job["image_id"], **parsed, "raw_text": row.get("text", ""),
                          "elapsed_seconds": row.get("elapsed_seconds")})
        result = summarize(views, len(inputs), events)
        result["attempt_directory"] = attempt.name
        report(folder, result)  # Commit inference results before optional speaker playback.
        if result["status"] == "partial":
            announce(config, "partial", events)
        announce(config, "result_" + (result["overall_level"] or "failed"), events)
        report(folder, result)
        write_json(folder / "analysis_status.json", {"status": result["status"], "time": now()})
        print(f"分析完成：{folder}；关注程度：{LABELS[result['overall_level']]}", flush=True)
        print(f"有效评估 {result['assessed_views']}/{result['input_views']} 张，仅针对照片可见区域。", flush=True)
        for view in result["views"]:
            print(f"{view['view_id']}：{view.get('reason') or view.get('error', '无法评估')}", flush=True)
        for item in result["priority_regions"]:
            print(f"建议：{item['where']}，{item['suggestion']}", flush=True)
        return result


def watch(config, once=False):
    inbox = Path(config["inbox"])
    inbox.mkdir(parents=True, exist_ok=True)
    with exclusive(inbox / ".watcher.lock"):
        while True:
            for marker in sorted(inbox.glob("round_*/READY.json")):
                folder = marker.parent
                if (folder / "analysis.json").exists() or (folder / "analysis_error.json").exists():
                    continue
                try:
                    analyze_round(config, folder)
                except ResourceBusyError:
                    # A manual request may own the model. Keep the round queued.
                    break
                except Exception as exc:
                    write_json(folder / "analysis_error.json", {"error": f"{type(exc).__name__}: {exc}", "time": now()})
                    print(f"本轮失败（不会自动重复）：{folder}：{exc}", flush=True)
            if once:
                return
            time.sleep(2)


def record_and_transcribe(config, folder, audio_source=None, allow_text_cue=False,
                          wait_enter=False, delay_seconds=0):
    asr = config["asr"]
    engine = asr.get("engine", "whisper")
    # Fail before recording if a recognizer/model has not been installed.
    if engine == "sensevoice":
        model_file = Path(asr["model_dir"]) / asr.get("model_name", "model_quant_optimized.onnx")
        if not model_file.is_file():
            raise FileNotFoundError(f"语音识别模型未安装：{model_file}")
    else:
        for key in ("binary", "model"):
            if not Path(asr[key]).is_file():
                raise FileNotFoundError(f"语音识别尚未安装：{asr[key]}")
    folder.mkdir(parents=True, exist_ok=False)
    audio = folder / "command.wav"
    events = []
    cue = "reused_recording"
    if audio_source:
        from shutil import copyfile
        copyfile(audio_source, audio)
    else:
        device = microphone_device(asr)
        event = announce(config, "listen", events) or {}
        played = event.get("status") == "played"
        if event.get("status") == "skipped":
            # 提示音只保留"开始工作"的嘀嘀，录音前改用文字提示（不等于音箱故障）
            allow_text_cue = True
        if config["audio"].get("enabled", True) and not played:
            if not allow_text_cue:
                write_json(folder / "command.json", {"status": "failed", "accepted": False,
                                                     "error": "录音提示未播放，本次没有开始录音", "events": events})
                raise RuntimeError("未能播放录音提示，请检查USB音箱；本次没有开始录音")
            # Explicit opt-in only: no cue is audible, so the record must say so.
            events.append({"audio": "listen", "status": "text_prompt",
                           "text": "请现在说“可以开始了”（5 秒）", "time": now()})
            cue = "text_prompt"
            print("提示音不可用：改用文字提示。", flush=True)
            if wait_enter:
                try:
                    input("准备好后按回车，然后立刻说“可以开始了”（录音 5 秒）：")
                except EOFError:
                    print("（当前不是交互终端，直接开始录音）", flush=True)
            elif delay_seconds > 0:
                for remaining in range(int(delay_seconds), 0, -1):
                    print(f"{remaining} 秒后开始录音，请准备说“可以开始了”…", flush=True)
                    time.sleep(1)
            print("正在录音 5 秒，请现在说“可以开始了”…", flush=True)
        else:
            cue = "played" if played else "disabled"
        subprocess.run(["arecord", "-D", device, "-f", "S16_LE", "-r", "16000", "-c", "1",
                        "-d", "5", str(audio)], check=True, capture_output=True, timeout=10)
    tick = time.perf_counter()
    evidence = {"audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(), "cue": cue}
    if engine == "sensevoice":
        return _transcribe_sensevoice(config, folder, audio, evidence, events, tick)

    command = [asr["binary"], "-m", asr["model"], "-f", str(audio), "-l", "zh", "-nt",
               "-t", str(asr.get("threads", 4)), "-nf", "-ac", str(asr.get("audio_context", 256)),
               "-bs", "1", "-bo", "1", "-mt", str(asr.get("max_tokens", 16)),
               "-otxt", "-of", str(folder / "transcript")]
    evidence["command"] = command
    try:
        process = subprocess.run(command, capture_output=True, timeout=asr["timeout_seconds"])
    except subprocess.TimeoutExpired as exc:
        (folder / "asr.log").write_bytes((exc.stdout or b"") + (exc.stderr or b""))
        write_json(folder / "command.json", {**evidence, "status": "failed", "accepted": False,
                                             "error": "ASR timeout", "seconds": time.perf_counter() - tick})
        raise RuntimeError("语音识别超时，本次不触发拍照") from exc
    (folder / "asr.log").write_bytes(process.stdout + process.stderr)
    runtime_file = folder / "transcript.runtime.json"
    runtime = load(runtime_file) if runtime_file.is_file() else None
    if runtime is not None:
        evidence["recognizer_runtime"] = runtime
    if process.returncode == 4 and runtime and runtime.get("near_token_limit"):
        text_file = folder / "transcript.txt"
        text = text_file.read_text(encoding="utf-8").strip() if text_file.is_file() else ""
        write_json(folder / "command.json", {**evidence, "status": "rejected", "accepted": False,
                    "reason": "recognizer_output_limit", "text": text,
                    "seconds": time.perf_counter() - tick, "events": events})
        print("识别输出达到短指令上限，本次不触发拍照。", flush=True)
        return False
    if process.returncode:
        write_json(folder / "command.json", {**evidence, "status": "failed", "accepted": False,
                                             "exit_code": process.returncode})
    process.check_returncode()
    text = (folder / "transcript.txt").read_text(encoding="utf-8").strip()
    accepted = is_start_command(text)
    write_json(folder / "command.json", {**evidence, "status": "recognized" if accepted else "rejected",
                                         "text": text, "accepted": accepted, "seconds": time.perf_counter() - tick,
                                         "events": events})
    print(f"识别文字：{text}；启动检查：{accepted}", flush=True)
    return accepted


def _transcribe_sensevoice(config, folder, audio, evidence, events, tick):
    """SenseVoice 走常驻服务：模型只加载一次，每句只等推理（见 docs §4）。"""
    import asr_client
    try:
        reply = asr_client.transcribe(config, audio)
    except asr_client.AsrUnavailable as exc:
        write_json(folder / "command.json", {**evidence, "engine": "sensevoice", "status": "failed",
                                             "accepted": False, "error": f"ASR 服务不可用：{exc}",
                                             "seconds": time.perf_counter() - tick, "events": events})
        raise RuntimeError("语音识别服务不可用，本次不触发拍照") from exc
    text = (reply.get("text") or "").strip()
    accepted = is_start_command(text)
    write_json(folder / "command.json", {**evidence, "engine": "sensevoice",
                "status": "recognized" if accepted else "rejected", "text": text, "accepted": accepted,
                "infer_seconds": reply.get("infer_seconds"),
                "model": reply.get("model") or config["asr"].get("model_name"),
                "seconds": time.perf_counter() - tick, "events": events})
    print(f"识别文字：{text}；启动检查：{accepted}", flush=True)
    return accepted


# 口语句尾/句首的填充词，ASR 经常多吐一两个字；只剥这些，不剥"吗/了"（"开始检查了吗"是提问，不是命令）。
_FILLER_PREFIX = ("那就", "现在", "帮我", "麻烦", "请", "好的", "好", "嗯")
_FILLER_SUFFIX = ("吧", "啊", "呀", "呢", "哦", "喔", "嗯", "好")
_NEGATION = re.compile(r"不|别|勿|没|不用")
_START_CORE = {"开始检查", "开始建查", "开始拍照", "开始拍摄", "检查房间", "开始检查一下", "检查一下",
               "可以开始了", "可以开始", "开始工作"}


def _strip_fillers(text):
    changed = True
    while changed:
        changed = False
        for prefix in _FILLER_PREFIX:
            if text.startswith(prefix) and len(text) > len(prefix):
                text = text[len(prefix):]
                changed = True
        for suffix in _FILLER_SUFFIX:
            if text.endswith(suffix) and len(text) > len(suffix):
                text = text[:-len(suffix)]
                changed = True
    return text


def is_start_command(text):
    normalized = re.sub(r"[^\u4e00-\u9fff]", "", text).translate(str.maketrans({"開": "开", "檢": "检", "攝": "摄", "間": "间", "請": "请"}))
    if not normalized:
        return False
    if _NEGATION.search(normalized):
        return False
    return normalized in _START_CORE or _strip_fillers(normalized) in _START_CORE


def microphone_device(asr):
    """Find the dedicated USB mic by hardware ID rather than volatile card number."""
    for card in sorted(Path("/sys/class/sound").glob("card[0-9]*")):
        device = (card / "device").resolve()
        for parent in (device, *device.parents):
            vendor, product = parent / "idVendor", parent / "idProduct"
            if vendor.is_file() and product.is_file():
                if vendor.read_text().strip() == asr["usb_vendor"] and product.read_text().strip() == asr["usb_product"]:
                    return f"plughw:{card.name[4:]},0"
    raise RuntimeError("未找到专用 USB 麦克风（08bb:2902），请检查连接")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/room_review_k1.json")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "run", "voice"):
        child = sub.add_parser(name)
        child.add_argument("--views", type=int, default=1)
        child.add_argument("--manual", action="store_true")
        if name == "capture":
            child.add_argument("--no-ready", action="store_true",
                               help="拍完先不发布 READY（配合 void 子命令验证作废路径）")
        if name == "voice":
            child.add_argument("--audio", type=Path, help="识别已保存的命令录音，跳过现场录音")
            child.add_argument("--no-cue", action="store_true",
                               help="音箱不可用时改用文字提示；仍需人工在场并说出口令")
            child.add_argument("--wait-enter", action="store_true",
                               help="文字提示后等你按回车才开始录音（交互终端推荐）")
            child.add_argument("--delay-seconds", type=int, default=0,
                               help="文字提示后等待 N 秒再录音（无交互终端时使用）")
    child = sub.add_parser("analyze")
    child.add_argument("round", type=Path)
    child = sub.add_parser("void", help="把已拍完但语音未确认的轮次标记作废：不发布 READY，不送模型")
    child.add_argument("round", type=Path)
    child.add_argument("--verdict", choices=("voided", "uncertain"), default="voided")
    child.add_argument("--reason", default="未确认的语音触发")
    child = sub.add_parser("publish", help="为已拍完但未发布的轮次补发 READY（人工确认后）")
    child.add_argument("round", type=Path)
    child = sub.add_parser("audio-check", help="检查播放设备并写出 audio_state.json")
    child.add_argument("--play", action="store_true", help="顺带试播一声提示音")
    child = sub.add_parser("transcribe", help="仅识别已保存录音，不启动相机或视觉模型")
    child.add_argument("audio", type=Path)
    child = sub.add_parser("watch")
    child.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = load(args.config)
    if args.command == "watch":
        watch(config, args.once)
    elif args.command == "transcribe":
        folder = Path(config["inbox"]) / ("voice_" + uuid.uuid4().hex)
        with exclusive(Path(config["inbox"]) / ".inference.lock"):
            record_and_transcribe(config, folder, args.audio)
        print(f"识别记录：{folder}", flush=True)
    elif args.command == "analyze":
        result = analyze_round(config, args.round)
        return 0 if result["status"] == "complete" else 2
    elif args.command == "void":
        meta = void_round(args.round, args.verdict, args.reason)
        print(f"已作废：{args.round}（状态 {meta['status']}，判定 {args.verdict}）；未发布 READY，不会被分析", flush=True)
        print(f"证据保留：{args.round / 'meta.json'}", flush=True)
        return 0
    elif args.command == "publish":
        path = publish_ready(args.round)
        print(f"已发布：{path}；watcher 下一轮会送模型", flush=True)
        return 0
    elif args.command == "audio-check":
        state = audio_check(config, play=args.play)
        return 0 if state.get("playable") else 2
    else:
        trigger = {"kind": "manual"}
        if args.command == "voice":
            gate = config.get("control", {}).get("gate", "verified")
            voice_folder = Path(config["inbox"]) / ("voice_" + uuid.uuid4().hex)
            with exclusive(Path(config["inbox"]) / ".inference.lock"):
                accepted = record_and_transcribe(config, voice_folder, args.audio,
                                                 allow_text_cue=getattr(args, "no_cue", False),
                                                 wait_enter=getattr(args, "wait_enter", False),
                                                 delay_seconds=getattr(args, "delay_seconds", 0))
            verdict = decide_verdict(gate, accepted)
            evidence = voice_folder / "command.json"
            record = load(evidence) if evidence.is_file() else {}
            trigger = {"kind": "voice", "gate": gate, "verdict": verdict,
                       "asr": {"text": record.get("text"), "latency_s": record.get("seconds")},
                       "command_evidence": str(evidence)}
            if verdict != "accepted":
                # The optimistic gate only pays off when a scan of several angles can overlap the
                # recognizer; until app/scan.py exists a spoken command still has to be accepted
                # before the camera fires. Recorded as-is rather than pretending it overlapped.
                print(f"语音判定：{verdict}；本轮不拍摄、不送模型。", flush=True)
                return 3
        folder = capture_round(config, args.views, args.manual, trigger,
                               publish_ready=not getattr(args, "no_ready", False))
        if getattr(args, "no_ready", False):
            print(f"本轮未发布 READY，不会被分析；确认后可 void 作废，或 publish 后补救：{folder}", flush=True)
        if args.command != "capture":
            result = analyze_round(config, folder)
            return 0 if result["status"] == "complete" else 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
