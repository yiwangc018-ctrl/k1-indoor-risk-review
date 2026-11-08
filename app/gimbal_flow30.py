"""云台 30 秒流程采集（2026-11-08 实机口径）。

为什么单独一个模块：
    `gimbal_scan.py` 走 `Gimbal/Hardware`，构造 `Hardware(cfg)` 时
    `PCA9685._init_chip()` 会把 16 路全部置 0（卸力）。当前实机姿态是
    pan=0°端、tilt≈+75°，卸力会让相机下坠。本模块只用 raw I2C 读写
    CH15/CH11，不构造 `Hardware`、不改其他通道。

流程（与 docs/云台30秒流程与接入-20261108.md 一致）：
    初始（不拍）→ 上移 25° → 四个 pan 角各停 2s 拍一张 → 双轴返回初始
    每张保存 raw + 旋转后的正式图；4 张齐后写 READY，再调 analyze_round。
    交接包写“相机反转 180°”，但 2026-11-08 16:50 实测 raw 画面是正的，
    所以默认 rotate=0；只有现场确认倒装时才用 --rotate 180。

用法（板端）：
    python3 app/gimbal_flow30.py read                 # 只读当前脉宽，不动
    python3 app/gimbal_flow30.py run --dry-run        # 只读+打印计划，不动
    python3 app/gimbal_flow30.py run --no-analyze     # 拍 4 张，不送模型
    python3 app/gimbal_flow30.py run                  # 拍 4 张 + 0.8B 分析

安全：
    * 动之前确认行程无遮挡、相机固定在云台上；
    * tilt 目标不超过 2241µs，pan 不超过 2478µs；
    * 任何异常都会在 finally 里把两轴尽量送回起始姿态，并保持力矩。
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import signal
import sys
import time
import uuid

import room_review as rr

# ---------------------------------------------------------------- 实机标定值
PCA_BUS = 4
PCA_ADDR = 0x40

PAN_CHANNEL = 15
TILT_CHANNEL = 11

# flow30_return2.py 最后一次执行值（交接包 16:24）
START_PAN_US = 512.7
START_TILT_US = 2055.7
TILT_UP_US = 2240.7
PAN_TARGETS_US = (994.4, 1488.9, 1983.3, 2477.8)

PAN_MIN_US = 500.0
PAN_MAX_US = 2478.0
TILT_MIN_US = 500.0
TILT_MAX_US = 2241.2

PULSE_MIN_US = 500.0
PULSE_MAX_US = 2500.0
TRAVEL_DEG = 270.0


@dataclass(frozen=True)
class Timing:
    """每段运动的时长；测试时传全 0 避免真等待。"""

    move_s: float = 2.9
    settle_s: float = 0.2
    hold_s: float = 2.0
    return_s: float = 5.2
    steps: int = 100


DEFAULT_TIMING = Timing()


# ---------------------------------------------------------------- 脉宽映射
def pulse_to_deg(us: float) -> float:
    """500–2500µs ↔ 0–270°。只用于报告里的近似角，不是实测绝对角。"""
    us = max(PULSE_MIN_US, min(PULSE_MAX_US, float(us)))
    return (us - PULSE_MIN_US) * TRAVEL_DEG / (PULSE_MAX_US - PULSE_MIN_US)


def pan_deg(us: float) -> float:
    return round(pulse_to_deg(us), 1)


def tilt_deg(us: float) -> float:
    # 该云台 tilt 的 1500µs 对应 135°，2055.7µs 约 +75°。
    return round(pulse_to_deg(us) - 135.0, 1)


def clamp_us(us: float, low: float, high: float) -> float:
    return max(low, min(high, float(us)))


# ---------------------------------------------------------------- 包路径
def ensure_gimbal_importable(config) -> None:
    """让 `import gimbal` 可用：仓库根目录、板端 package_parent 或 /home/biandu/rv。"""
    if "gimbal" in sys.modules:
        return
    try:
        import gimbal  # noqa: F401
        return
    except ImportError:
        pass
    candidates = [(config.get("gimbal") or {}).get("package_parent"),
                  "/home/biandu/rv", str(rr.ROOT)]
    for candidate in candidates:
        if not candidate:
            continue
        root = Path(candidate)
        if (root / "gimbal" / "__init__.py").is_file():
            sys.path.insert(0, str(root))
            import gimbal  # noqa: F401
            return
    raise RuntimeError(
        "找不到 gimbal 包：把 gimbal/ 部署到 /home/biandu/rv/gimbal，"
        "或在 configs/room_review_k1.json 的 gimbal.package_parent 里指明父目录")


# ---------------------------------------------------------------- PCA 脉宽总线
class PcaPulseBus:
    """只操作 PCA9685 的 LEDn 寄存器，不初始化芯片、不碰其他通道。"""

    def __init__(self, bus_num: int = PCA_BUS, addr: int = PCA_ADDR):
        from gimbal.i2c import I2CBus

        self.bus = I2CBus(bus_num)
        self.addr = addr

    def read_us(self, channel: int) -> float:
        data = self.bus.read_regs(self.addr, 0x06 + 4 * channel, 4)
        ticks = data[2] | ((data[3] & 0x0F) << 8)
        return ticks * 20000.0 / 4096.0

    def write_us(self, channel: int, us: float) -> None:
        ticks = int(round(clamp_us(us, 0.0, 4095.0 * 20000.0 / 4096.0) * 4096.0 / 20000.0))
        ticks = max(0, min(4095, ticks))
        reg = 0x06 + 4 * channel
        for offset, value in enumerate((0, 0, ticks & 0xFF, (ticks >> 8) & 0x0F)):
            self.bus.write_reg(self.addr, reg + offset, value)

    def close(self) -> None:
        self.bus.close()


# ---------------------------------------------------------------- 采集与旋转
def _rotate_image(raw: Path, target: Path, degrees: int) -> None:
    from PIL import Image

    with Image.open(raw) as image:
        if degrees % 360 == 180:
            image = image.transpose(Image.ROTATE_180)
        elif degrees % 360 != 0:
            image = image.rotate(degrees, expand=True)
        image.convert("RGB").save(target, quality=92)


def _default_capture(config, target: Path, log_path: Path):
    return rr.capture_image(config, target, log_path)


# ---------------------------------------------------------------- 状态文件
def _state_file(config):
    target = (config.get("gimbal") or {}).get("state_file")
    if not target:
        return None
    path = Path(target)
    return path if path.is_absolute() else rr.ROOT / path


def write_state(config, folder: Path, **state) -> None:
    payload = {"round_id": Path(folder).name, "round_dir": str(folder), "updated_at": rr.now()}
    payload.update(state)
    rr.write_json(Path(folder) / "state.json", payload)
    shared = _state_file(config)
    if shared is not None:
        rr.write_json(shared, payload)


# ---------------------------------------------------------------- 运动
def _ramp(pulse, channel: int, target: float, duration: float, steps: int,
          settle: float, sleep, label: str, log) -> float:
    start = pulse.read_us(channel)
    target = clamp_us(target, PAN_MIN_US if channel == PAN_CHANNEL else TILT_MIN_US,
                      PAN_MAX_US if channel == PAN_CHANNEL else TILT_MAX_US)
    if duration <= 0 or steps <= 1:
        pulse.write_us(channel, target)
    else:
        for index in range(1, steps + 1):
            pulse.write_us(channel, start + (target - start) * index / steps)
            sleep(duration / steps)
    if settle > 0:
        sleep(settle)
    actual = pulse.read_us(channel)
    log(f"{label} CH{channel} {start:.1f}→{actual:.1f}µs")
    return actual


def _ramp2(pulse, pan_target: float, tilt_target: float, duration: float, steps: int,
           settle: float, sleep, log) -> None:
    """双轴同时回起始；交接包 30 秒流程的返回段是 5.2 秒双轴，不是两轴各 5.2 秒。"""
    pan_start, tilt_start = pulse.read_us(PAN_CHANNEL), pulse.read_us(TILT_CHANNEL)
    pan_target = clamp_us(pan_target, PAN_MIN_US, PAN_MAX_US)
    tilt_target = clamp_us(tilt_target, TILT_MIN_US, TILT_MAX_US)
    if duration <= 0 or steps <= 1:
        pulse.write_us(PAN_CHANNEL, pan_target)
        pulse.write_us(TILT_CHANNEL, tilt_target)
    else:
        for index in range(1, steps + 1):
            pulse.write_us(PAN_CHANNEL, pan_start + (pan_target - pan_start) * index / steps)
            pulse.write_us(TILT_CHANNEL, tilt_start + (tilt_target - tilt_start) * index / steps)
            sleep(duration / steps)
    if settle > 0:
        sleep(settle)
    log(f"返回 pan CH{PAN_CHANNEL} {pan_start:.1f}→{pulse.read_us(PAN_CHANNEL):.1f}µs，"
        f"tilt CH{TILT_CHANNEL} {tilt_start:.1f}→{pulse.read_us(TILT_CHANNEL):.1f}µs")


# ---------------------------------------------------------------- 语音
def voice_trigger(config, audio=None, allow_text_cue=False, wait_enter=False, delay_seconds=0):
    """录音/识别/判定，返回 (trigger, verdict, voice_dir)。不通过不碰云台。"""
    gate = config.get("control", {}).get("gate", "verified")
    inbox = Path(config["inbox"])
    folder = inbox / ("voice_" + uuid.uuid4().hex)
    with rr.exclusive(inbox / ".inference.lock"):
        accepted = rr.record_and_transcribe(
            config, folder, audio_source=audio, allow_text_cue=allow_text_cue,
            wait_enter=wait_enter, delay_seconds=delay_seconds,
        )
    verdict = rr.decide_verdict(gate, accepted)
    evidence = folder / "command.json"
    record = rr.load(evidence) if evidence.is_file() else {}
    trigger = {
        "kind": "voice", "gate": gate, "verdict": verdict,
        "asr": {"text": record.get("text"), "latency_s": record.get("seconds")},
        "command_evidence": str(evidence),
    }
    return trigger, verdict, folder


# ---------------------------------------------------------------- 主流程
def run_flow30(config, *, pulse=None, capture=None, analyze=True, rotate_deg=0,
               dry_run=False, cue=False, trigger=None, timing=DEFAULT_TIMING,
               sleep=time.sleep, log=print, lock=None):
    """跑一轮 30 秒流程，返回本轮目录。

    pulse/capture/analyze/lock 可注入，便于 PC 单测（Windows 没有 fcntl）；
    正常路径自己打开 I2C 与相机，并用 room_review.exclusive 锁相机。
    """
    inbox = Path(config["inbox"])
    folder = inbox / ("round_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
    originals = folder / "originals"
    originals.mkdir(parents=True)
    meta = {
        "version": 1,
        "round_id": folder.name,
        "status": "capturing",
        "created_at": rr.now(),
        "capture_mode": "gimbal_flow30",
        "requested_views": len(PAN_TARGETS_US),
        "coverage": ("云台四个固定 pan 角（约 66.75/133.5/200.25/267°），俯仰上移约 25°；"
                     "仅本轮可见区域，无测距，不是全屋覆盖"),
        "trigger": trigger or {"kind": "manual"},
        "views": [],
        "events": [],
        "gimbal": {
            "bus": PCA_BUS,
            "address": "0x40",
            "pan_channel": PAN_CHANNEL,
            "tilt_channel": TILT_CHANNEL,
            "rotation_deg": int(rotate_deg),
            "start": {"pan_us": None, "tilt_us": None},
            "returned": None,
        },
    }
    rr.write_json(folder / "meta.json", meta)

    owns_pulse = pulse is None
    if pulse is None:
        ensure_gimbal_importable(config)
        pulse = PcaPulseBus()
    if capture is None:
        capture = lambda target, log_path: _default_capture(config, target, log_path)

    lock_ctx = nullcontext() if dry_run else (
        lock(inbox / ".camera.lock") if lock is not None else rr.exclusive(inbox / ".camera.lock"))
    try:
        with lock_ctx:
            start_pan = pulse.read_us(PAN_CHANNEL)
            start_tilt = pulse.read_us(TILT_CHANNEL)
            meta["gimbal"]["start"] = {"pan_us": round(start_pan, 1), "tilt_us": round(start_tilt, 1)}
            rr.write_json(folder / "meta.json", meta)

            if not dry_run and (abs(start_pan - START_PAN_US) > 50 or abs(start_tilt - START_TILT_US) > 50):
                meta.update(status="refused",
                            error="起始姿态不是 512.7/2055.7；PCA 可能被清零，请先恢复，未动电机")
                rr.write_json(folder / "meta.json", meta)
                raise RuntimeError(
                    "云台不在交接包初始姿态（当前 pan=%.1f tilt=%.1f，期望 %.1f/%.1f）。"
                    "PCA 可能被 Hardware(cfg)/菜单6 清零；先用 /tmp/restore_initial_pca.py "
                    "慢速恢复，本次未动电机。" % (start_pan, start_tilt, START_PAN_US, START_TILT_US))

            if dry_run:
                meta.update(status="dry_run", completed_at=rr.now(), views_captured=0,
                            plan={"pan_us": list(PAN_TARGETS_US), "tilt_up_us": TILT_UP_US,
                                  "return_to": {"pan_us": round(start_pan, 1),
                                                "tilt_us": round(start_tilt, 1)}})
                rr.write_json(folder / "meta.json", meta)
                write_state(config, folder, phase="dry_run", index=0, total=len(PAN_TARGETS_US),
                            message="干跑：只读起始姿态，未动舵机、未拍图")
                log(f"干跑：起始 pan={start_pan:.1f}µs tilt={start_tilt:.1f}µs；"
                    f"计划 pan={list(PAN_TARGETS_US)} tilt_up={TILT_UP_US}µs")
                return folder

            target_pan = [clamp_us(value, PAN_MIN_US, PAN_MAX_US) for value in PAN_TARGETS_US]
            target_tilt_up = clamp_us(TILT_UP_US, TILT_MIN_US, TILT_MAX_US)

            try:
                if cue:
                    rr.announce(config, "start", meta["events"])
                started = time.perf_counter()

                _ramp(pulse, TILT_CHANNEL, target_tilt_up, timing.move_s, timing.steps,
                      timing.settle_s, sleep, "上移", log)

                for index, target in enumerate(target_pan, start=1):
                    ident = "view_%02d" % index
                    _ramp(pulse, PAN_CHANNEL, target, timing.move_s, timing.steps,
                          timing.settle_s, sleep, ident, log)
                    rr.write_json(folder / "meta.json", meta)
                    write_state(config, folder, phase="capturing", index=index,
                                total=len(target_pan), message=f"第 {index}/{len(target_pan)} 个角度，准备拍照")
                    hold_start = time.perf_counter()
                    pan_us = pulse.read_us(PAN_CHANNEL)
                    tilt_us = pulse.read_us(TILT_CHANNEL)
                    raw = originals / (ident + "_raw.jpg")
                    target_file = originals / (ident + ".jpg")
                    captured = capture(raw, folder / (ident + "_capture.log"))
                    _rotate_image(raw, target_file, int(rotate_deg))
                    remain = timing.hold_s - (time.perf_counter() - hold_start)
                    if remain > 0:
                        sleep(remain)
                    record = {
                        "commanded": {
                            "pan_us": round(pan_us, 1),
                            "tilt_us": round(tilt_us, 1),
                            "pan": pan_deg(pan_us),
                            "tilt": tilt_deg(tilt_us),
                        },
                        "measured": None,
                        "source": "commanded",
                        "pan_source": "commanded",
                        "rotation_deg": int(rotate_deg),
                    }
                    meta["views"].append({
                        "view_id": ident,
                        "file": target_file.relative_to(folder).as_posix(),
                        "raw_file": raw.relative_to(folder).as_posix(),
                        "size": captured["size"],
                        "sha256": hashlib.sha256(target_file.read_bytes()).hexdigest(),
                        "captured_at": rr.now(),
                        "seconds": captured["seconds"],
                        "hold_started_s": round(hold_start - started, 2),
                        "pose": record,
                    })
                    rr.write_json(folder / "meta.json", meta)
                    write_state(config, folder, phase="capturing", index=index,
                                total=len(target_pan), photo=target_file.relative_to(folder).as_posix(),
                                pose=record,
                                message=f"已拍 {index}/{len(target_pan)} 张，pan={record['commanded']['pan']:.0f}°")
                    log(f"照片已保存：{target_file}（pan={pan_us:.1f}µs/{record['commanded']['pan']:.1f}°，"
                        f"tilt={tilt_us:.1f}µs/{record['commanded']['tilt']:.1f}°，"
                        f"旋转 {rotate_deg}°）")
            except BaseException as exc:
                meta.update(status="capture_failed", error=f"{type(exc).__name__}: {exc}")
                rr.write_json(folder / "meta.json", meta)
                write_state(config, folder, phase="error", index=len(meta["views"]),
                            total=len(target_pan), message=f"采集失败：{exc}")
                raise
            finally:
                # 无论成功失败都尽量回起始姿态，并保持力矩（不卸力）。
                try:
                    _ramp2(pulse, start_pan, start_tilt, timing.return_s, timing.steps,
                           timing.settle_s, sleep, log)
                    meta["gimbal"]["returned"] = {"pan_us": round(pulse.read_us(PAN_CHANNEL), 1),
                                                  "tilt_us": round(pulse.read_us(TILT_CHANNEL), 1)}
                    rr.write_json(folder / "meta.json", meta)
                except Exception as exc:                      # 回中失败不掩盖原始异常
                    log(f"回起始姿态失败（已忽略）：{exc}")

    finally:
        if owns_pulse:
            try:
                pulse.close()
            except Exception as exc:
                log(f"关闭 I2C 失败：{exc}")

    captured_views = len(meta["views"])
    elapsed = round(time.perf_counter() - started, 3)
    status = "captured" if captured_views == len(target_pan) else (
        "partial_capture" if captured_views else "capture_failed")
    meta.update(status=status, completed_at=rr.now(), seconds=elapsed,
                views_captured=captured_views)
    rr.write_json(folder / "meta.json", meta)

    if captured_views == 0:
        write_state(config, folder, phase="error", index=0, total=len(target_pan),
                    message="本轮没有拍到照片，未发布 READY")
        raise RuntimeError("本轮没有拍到照片，未发布 READY")

    # READY 是唯一进入推理的凭据，最后发布（部分完成也发布，分析会标 coverage=false）。
    rr.write_manifest(folder, meta)
    write_state(config, folder, phase="captured", analyzed=False, index=captured_views,
                total=len(target_pan), message=f"已拍 {captured_views}/{len(target_pan)} 张，等待分析")

    if analyze:
        write_state(config, folder, phase="analyzing", index=captured_views,
                    total=len(target_pan), message=f"分析中（{captured_views} 张）")
        result = rr.analyze_round(config, folder)
        write_state(config, folder, phase="done", analyzed=True, index=captured_views,
                    total=len(target_pan), level=result.get("overall_level"),
                    status=result.get("status"), report="report.md", analysis="analysis.json",
                    message=f"完成：{captured_views}/{len(target_pan)} 张，关注程度 {result.get('overall_level')}")
    else:
        write_state(config, folder, phase="done", analyzed=False, index=captured_views,
                    total=len(target_pan), message=f"已拍 {captured_views}/{len(target_pan)} 张，未送模型")
    return folder


# ---------------------------------------------------------------- 命令行
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=rr.ROOT / "configs/room_review_k1.json")
    sub = parser.add_subparsers(dest="command", required=True)

    read = sub.add_parser("read", help="只读当前 pan/tilt 脉宽，不动舵机")
    read.add_argument("--bus", type=int, default=PCA_BUS)

    run = sub.add_parser("run", help="30 秒流程 + 四张拍照（可接着分析）")
    run.add_argument("--no-analyze", action="store_true", help="只拍，不送模型")
    run.add_argument("--dry-run", action="store_true", help="只读起始姿态，不动、不拍")
    run.add_argument("--rotate", type=int, default=0, choices=(0, 180),
                     help="正式图旋转角度；2026-11-08 实测 raw 是正的，默认 0；现场倒装才用 180")
    run.add_argument("--cue", action="store_true", help="开始前播一次嘀嘀（语音入口已播过则不要加）")
    run.add_argument("--voice", action="store_true", help="先语音触发；通过后才动云台")
    run.add_argument("--audio", type=Path, help="复用录音，跳过现场录音/提示语（测试用）")
    run.add_argument("--no-cue", action="store_true", help="音箱不可用时允许文字提示")
    run.add_argument("--wait-enter", action="store_true", help="文字提示后等回车再录音")
    run.add_argument("--delay-seconds", type=int, default=0)
    run.add_argument("--trigger", default="manual", help="触发来源，写进 meta.json")

    args = parser.parse_args(argv)
    config = rr.load(args.config)

    if args.command == "read":
        ensure_gimbal_importable(config)
        pulse = PcaPulseBus(args.bus)
        try:
            print(f"pan  CH{PAN_CHANNEL} = {pulse.read_us(PAN_CHANNEL):.1f}µs "
                  f"（约 {pan_deg(pulse.read_us(PAN_CHANNEL)):.1f}°）")
            print(f"tilt CH{TILT_CHANNEL} = {pulse.read_us(TILT_CHANNEL):.1f}µs "
                  f"（约 {tilt_deg(pulse.read_us(TILT_CHANNEL)):.1f}°）")
        finally:
            pulse.close()
        return 0

    trigger = {"kind": args.trigger}
    if args.voice and not args.dry_run:
        trigger, verdict, voice_folder = voice_trigger(
            config, audio=args.audio, allow_text_cue=args.no_cue,
            wait_enter=args.wait_enter, delay_seconds=args.delay_seconds,
        )
        if verdict != "accepted":
            print(f"语音判定：{verdict}；不转动、不拍照。证据：{voice_folder}")
            return 3
        print(f"语音通过：{trigger.get('asr', {}).get('text')}")

    folder = run_flow30(config, analyze=not args.no_analyze, rotate_deg=args.rotate,
                        dry_run=args.dry_run, cue=(args.cue or args.voice),
                        trigger=trigger)
    meta = rr.load(folder / "meta.json")
    print(f"本轮目录：{folder}")
    print(f"状态：{meta['status']}；照片 {len(meta['views'])}/{meta.get('requested_views')}；"
          f"耗时 {meta.get('seconds')}s")
    for view in meta["views"]:
        pose = view["pose"]["commanded"]
        print(f"  {view['view_id']}  pan={pose['pan']:.1f}°  tilt={pose['tilt']:.1f}°  "
              f"旋转={view['pose']['rotation_deg']}°  {view['seconds']:.2f}s")
    if meta["status"] == "partial_capture":
        print("部分完成：有角度未拍到，analysis 会标 coverage=false", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    def _interrupt(signum, frame):
        raise KeyboardInterrupt("收到信号 %s" % signum)

    for _name in ("SIGTERM", "SIGHUP"):
        _sig = getattr(signal, _name, None)
        if _sig is not None:
            signal.signal(_sig, _interrupt)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("已中断；云台已尝试返回初始，请用 read 核对姿态", file=sys.stderr)
        raise SystemExit(130)
