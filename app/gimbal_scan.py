"""云台四角采集：把 gimbal 包的四位姿扫描接成 room_review 的一轮照片。

2026-11-08 交接包修订：新的 30 秒流程用 raw I2C（pan CH15、tilt CH11、
初始 512.7/2055.7µs），见 docs/云台30秒流程与接入-20261108.md。
本模块走 Gimbal/Hardware，`Hardware(cfg)` 会清零 PCA，不用于该 30 秒流程。

链路：
    云台转到固定位姿 → 停稳 + 采图门禁 → v4l2-ctl 拍一张 → 写 meta/state
    四张齐 → READY.json（原子发布）→ 可选调用 room_review.analyze_round

边界（报告必须如实写）：
    * 相机是否随云台转动取决于机械安装，本模块不检测；相机没固定在云台上时，
      四张图会是同一个画面，只能在证据里看出，不能靠代码补救。
    * 没有 MPU6050 时俯仰只有命令值（pose.source=commanded），不能写“实测角度”。
      TBS-K15 不带位置反馈，pan 本来就是开环索引（0/90/180/270 由舵机行程给出）。
    * 四个方位只保证“四个视角”，不等于全屋覆盖；覆盖缺口见 scan_manifest.json。

用法（板端）：
    cd /home/biandu/rv/room_review
    python3 app/gimbal_scan.py run               # 四张 + 分析
    python3 app/gimbal_scan.py run --no-analyze  # 只拍，不送模型
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

import room_review as rr

REPO_GIMBAL_PARENT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 包与配置
def ensure_gimbal_importable(config):
    """让 `import gimbal` 可用：仓库根目录或板端 /home/biandu/rv。"""
    if "gimbal" in sys.modules:
        return sys.modules["gimbal"]
    try:
        import gimbal
        return gimbal
    except ImportError:
        pass
    gc = config.get("gimbal") or {}
    for candidate in (gc.get("package_parent"), "/home/biandu/rv", REPO_GIMBAL_PARENT):
        if not candidate:
            continue
        root = Path(candidate)
        if (root / "gimbal" / "__init__.py").is_file():
            sys.path.insert(0, str(root))
            import gimbal
            return gimbal
    raise RuntimeError(
        "找不到 gimbal 包：把 gimbal/ 部署到 /home/biandu/rv/gimbal，"
        "或在 configs/room_review_k1.json 的 gimbal.package_parent 里指明父目录")


def _absolute(path):
    path = Path(path)
    return path if path.is_absolute() else rr.ROOT / path


def load_gimbal_config(config):
    gc = config.get("gimbal") or {}
    path = _absolute(gc.get("config") or "gimbal/config.json")
    if not path.is_file():
        raise FileNotFoundError(f"云台配置文件不存在：{path}")
    with path.open(encoding="utf-8") as stream:
        return json.load(stream), path


def make_gimbal(config, log=print):
    """探测硬件并构造 Gimbal（未启动控制线程）。"""
    ensure_gimbal_importable(config)
    from gimbal.hw import Hardware
    from gimbal.control import Gimbal
    gcfg, gpath = load_gimbal_config(config)
    hardware = Hardware(gcfg, log=log)
    if not hardware.ok():
        hardware.close()
        raise RuntimeError("云台 PWM 后端不可用（先跑 python3 -m gimbal detect）：\n" + hardware.report())
    log(f"[Gimbal] 配置文件：{gpath}")
    return Gimbal(gcfg, hardware.backend, hardware.mpu, log=log), hardware, gcfg


# ---------------------------------------------------------------- 状态文件
def _state_file(config):
    gc = config.get("gimbal") or {}
    target = gc.get("state_file")
    return _absolute(target) if target else None


def write_state(config, folder, **state):
    """原子写本轮 state.json；如配置了 state_file，同步写一份供显示窗读取。"""
    payload = {"round_id": Path(folder).name, "round_dir": str(folder), "updated_at": rr.now()}
    payload.update(state)
    rr.write_json(Path(folder) / "state.json", payload)
    shared = _state_file(config)
    if shared is not None:
        rr.write_json(shared, payload)


# ---------------------------------------------------------------- 位姿记录
def pose_record(gimbal, pose):
    """命令角 + （有 MPU 时的）俯仰/滚转实测角。pan 无绝对角传感器，永远是命令值。"""
    record = {
        "commanded": {"pan": round(float(pose["pan"]), 1),
                      "tilt": round(float(pose["tilt_cmd"]), 1)},
        "measured": None,
        "source": "commanded",
        "pan_source": "commanded",
        "gate_ok": pose.get("gate_ok"),
        "gate_reasons": list(pose.get("gate_reasons") or []),
    }
    mpu = getattr(gimbal, "mpu", None)
    if mpu is not None:
        try:
            pitch, roll = mpu.read_pitch_roll()
            record["measured"] = {"pitch": round(float(pitch), 2), "roll": round(float(roll), 2)}
            # 只有俯仰/滚转是实测；pan 仍然是开环命令值，所以不写 source=sensor。
            record["source"] = "partial_sensor"
        except Exception as exc:                       # 读不到就退回命令值，不伪造
            record["mpu_error"] = f"{type(exc).__name__}: {exc}"
    return record


# ---------------------------------------------------------------- 主流程
def capture_round_scan(config, gimbal=None, gcfg=None, mode=None, tilt=None, azimuths=None,
                       trigger=None, analyze=False, dry_run=False, log=print):
    """跑一轮云台四角采集，返回本轮目录。

    gimbal/gcfg 只为测试注入；正常路径自己探测硬件。
    """
    ensure_gimbal_importable(config)
    from gimbal.scan import _azimuths, default_plan, run_scan

    gc = config.get("gimbal") or {}
    hardware = None
    if gimbal is None:
        gimbal, hardware, gcfg = make_gimbal(config, log=log)
    elif gcfg is None:
        gcfg, _ = load_gimbal_config(config)

    plan = default_plan(gcfg, mode or gc.get("scan_mode"))
    if azimuths:
        plan["azimuths"], plan["azimuths_from_presets"] = _azimuths(gcfg, int(azimuths), True)
    if tilt is not None:
        if len(plan["rings"]) > 1:
            log(f"[Scan] 多环模式下忽略 tilt={tilt:.0f}（每环角度由模式决定）")
        else:
            plan["rings"] = [float(tilt)]
    total = len(plan["rings"]) * len(plan["azimuths"])

    inbox = Path(config["inbox"])
    folder = inbox / ("round_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
    originals = folder / "originals"
    originals.mkdir(parents=True)
    meta = {"version": 1, "round_id": folder.name, "status": "capturing", "created_at": rr.now(),
            "capture_mode": "gimbal_scan", "requested_views": total,
            "coverage": "云台四个固定视角；无测距、无自动覆盖保证；角度来源逐张见 pose",
            "trigger": trigger or {"kind": "manual"}, "views": [], "events": [],
            "gimbal": {"mode": plan["mode"], "label": plan["label"],
                       "closed_loop": bool(gcfg.get("scan", {}).get("tilt_closed_loop", True))
                       and getattr(gimbal, "mpu", None) is not None}}
    rr.write_json(folder / "meta.json", meta)
    capture_settle = float(gc.get("capture_settle_seconds", 0.3))

    def capture(pose):
        index = len(meta["views"]) + 1
        ident = "view_%02d" % index
        time.sleep(capture_settle)
        target = originals / (ident + ".jpg")
        captured = rr.capture_image(config, target, folder / (ident + "_capture.log"))
        record = pose_record(gimbal, pose)
        meta["views"].append({"view_id": ident, "file": target.relative_to(folder).as_posix(),
                              "size": captured["size"],
                              "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                              "captured_at": rr.now(), "seconds": captured["seconds"],
                              "pose": record})
        rr.announce(config, "capture_ok", meta["events"])
        rr.write_json(folder / "meta.json", meta)
        write_state(config, folder, phase="capturing", index=index, total=total,
                    photo=str(target.relative_to(folder)), pose=record,
                    message=f"第 {index}/{total} 张，pan={record['commanded']['pan']:.0f}° "
                            f"tilt={record['commanded']['tilt']:.0f}°")
        log(f"照片已保存：{target}（pan={record['commanded']['pan']:.0f}°，"
            f"tilt={record['commanded']['tilt']:.0f}°，{captured['seconds']:.2f}s）")
        return str(target)

    started = time.perf_counter()
    manifest = None
    try:
        gimbal.start()
        try:
            # 复用 .camera.lock：云台扫描和手动拍照不能同时占相机。
            with rr.exclusive(inbox / ".camera.lock"):
                manifest = run_scan(gimbal, capture_fn=None if dry_run else capture,
                                    out_json=folder / "scan_manifest.json", cfg=gcfg,
                                    mode=mode, tilt=tilt, azimuths=azimuths,
                                    dry_run=dry_run, log=log)
        finally:
            # 回中后保持力矩：relax 等于给舵机卸力，相机可能直接下坠。
            if gc.get("home_after", True):
                try:
                    gimbal.home(blocking=True)
                except Exception as exc:
                    log(f"[Gimbal] 回中失败（已忽略）：{exc}")
            gimbal.stop(relax=bool(gc.get("relax_after_scan", False)))
    except BaseException as exc:
        meta.update(status="capture_failed", error=f"{type(exc).__name__}: {exc}")
        rr.write_json(folder / "meta.json", meta)
        write_state(config, folder, phase="error", index=len(meta["views"]), total=total, message=f"采集失败：{exc}")
        raise
    finally:
        if hardware is not None:
            hardware.close()

    captured_views = len(meta["views"])
    if dry_run:
        status = "dry_run"
    elif captured_views == 0:
        status = "capture_failed"
    elif captured_views < (manifest or {}).get("plan", {}).get("count", total):
        status = "partial_capture"
    else:
        status = "captured"
    meta.update(status=status, completed_at=rr.now(),
                seconds=round(time.perf_counter() - started, 3), views_captured=captured_views,
                scan={"mode": (manifest or {}).get("mode"),
                      "label": (manifest or {}).get("label"),
                      "plan": (manifest or {}).get("plan"),
                      "coverage_gaps_deg": (manifest or {}).get("coverage_gaps_deg"),
                      "closed_loop": (manifest or {}).get("closed_loop"),
                      "gate_rejected": sum(1 for p in (manifest or {}).get("poses", [])
                                           if not p.get("gate_ok"))})
    rr.write_json(folder / "meta.json", meta)

    if dry_run:
        write_state(config, folder, phase="done", index=0, total=total, message="干跑结束（未拍图、未发布 READY）")
        return folder
    if captured_views == 0:
        write_state(config, folder, phase="error", index=0, total=total, message="本轮没有拍到照片，未发布 READY")
        raise RuntimeError("本轮没有拍到照片（采图门禁未通过或相机失败），未发布 READY")

    # READY.json 是唯一进入推理的凭据，最后发布。
    rr.write_manifest(folder, meta)
    write_state(config, folder, phase="captured", analyzed=False, index=captured_views, total=total,
                message=f"已拍 {captured_views}/{total} 张，等待分析")

    if analyze:
        write_state(config, folder, phase="analyzing", index=captured_views, total=total, message=f"分析中（{captured_views} 张）")
        result = rr.analyze_round(config, folder)
        write_state(config, folder, phase="done", analyzed=True, index=captured_views, total=total,
                    level=result.get("overall_level"), status=result.get("status"),
                    report="report.md", analysis="analysis.json",
                    message=f"完成：{captured_views}/{total} 张，关注程度 {result.get('overall_level')}")
    else:
        write_state(config, folder, phase="done", analyzed=False, index=captured_views, total=total,
                    message=f"已拍 {captured_views}/{total} 张，未送模型")
    return folder


# ---------------------------------------------------------------- 命令行
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=rr.ROOT / "configs/room_review_k1.json")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="云台转到固定四位姿、各拍一张，可接着分析")
    run.add_argument("--mode", choices=["quick", "full"], help="quick=4 张；full=三环 12 张")
    run.add_argument("--tilt", type=float, help="覆盖俯仰角（单环有效），负值=向下")
    run.add_argument("--azimuths", type=int, help="方位数，默认按云台配置（4）")
    run.add_argument("--no-analyze", action="store_true", help="只拍，不送模型")
    run.add_argument("--dry-run", action="store_true", help="只走位姿与门禁，不拍图")
    run.add_argument("--trigger", default="manual", help="触发来源，写进 meta.json")
    args = parser.parse_args(argv)

    config = rr.load(args.config)
    folder = capture_round_scan(config, mode=args.mode, tilt=args.tilt, azimuths=args.azimuths,
                                trigger={"kind": args.trigger},
                                analyze=not args.no_analyze, dry_run=args.dry_run)
    meta = rr.load(folder / "meta.json")
    print(f"本轮目录：{folder}", flush=True)
    print(f"状态：{meta['status']}；照片 {len(meta['views'])}/"
          f"{meta.get('requested_views')}；耗时 {meta.get('seconds')}s", flush=True)
    for view in meta["views"]:
        pose = view["pose"]["commanded"]
        print(f"  {view['view_id']}  pan={pose['pan']:.0f}°  tilt={pose['tilt']:.0f}°  "
              f"角度来源={view['pose']['source']}  {view['seconds']:.2f}s", flush=True)
    if meta["status"] == "partial_capture":
        print("部分完成：有视角未通过采图门禁或拍照失败，详见 scan_manifest.json", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
