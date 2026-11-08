"""命令行入口：python -m gimbal <命令>。"""

import argparse
import json
import os
import sys
import time

from .control import Gimbal
from .hw import Hardware, ensure_i2c_permission
from .scan import run_scan
from .servo import ServoSpec

DEFAULT_CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_cfg(path=None):
    p = path or DEFAULT_CFG
    with open(p, encoding="utf-8") as f:
        return json.load(f), p


def save_cfg(cfg, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def build(cfg, log=print):
    hw = Hardware(cfg, log=log)
    if not hw.ok():
        log(hw.report())
        log("PWM 后端不可用，无法继续。先跑 `python -m gimbal detect`。")
        return None, hw
    return Gimbal(cfg, hw.backend, hw.mpu, log=log), hw


# ---------------- 子命令 ----------------
def cmd_detect(cfg, cfg_path, args):
    ensure_i2c_permission()
    hw = Hardware(cfg)
    print(hw.report())
    print()
    print("== 当前配置解析结果 ==")
    for name in ("pan", "tilt"):
        sp = ServoSpec.from_dict(name, cfg["servos"][name])
        print("  " + sp.describe())
        lo, hi = sp.axis_limits()
        print("      可动范围 %.1f ~ %.1f°；脉宽 %.0f~%.0f us"
              % (lo, hi, sp.min_us, sp.max_us))
        if sp.travel_deg < 200 and name == "pan":
            print("      ！注意：pan 行程 %.0f° < 270°，四方位（0/90/180/270）里"
                  " 270 无法到达，需换 270° 版舵机" % sp.travel_deg)
    presets = cfg.get("presets", {}).get("pan", [])
    sp = ServoSpec.from_dict("pan", cfg["servos"]["pan"])
    lo, hi = sp.axis_limits()
    bad = [p for p in presets if p < lo - 0.1 or p > hi + 0.1]
    if bad:
        print("  ！预设方位 %s 超出软限位 [%.1f, %.1f]，运行时会自动夹紧" % (bad, lo, hi))
    hw.close()


def cmd_config(cfg, cfg_path, args):
    print("配置文件：%s" % cfg_path)
    for name in ("pan", "tilt"):
        sp = ServoSpec.from_dict(name, cfg["servos"][name])
        print("  " + sp.describe())
    print("  presets.pan  = %s" % cfg.get("presets", {}).get("pan"))
    print("  presets.tilt = %s" % cfg.get("presets", {}).get("tilt"))
    print("  control      = %s" % cfg.get("control"))
    print("  safety       = %s" % cfg.get("safety"))
    print("  scan         = %s" % cfg.get("scan"))


def cmd_pose(cfg, cfg_path, args):
    g, hw = build(cfg)
    if g is None:
        return 1
    g.start()
    try:
        if args.pitch is not None:
            g.goto(tilt=args.pitch, blocking=True, timeout=20)
            r = g.tilt_to_pitch(args.pitch, verbose=args.verbose)
            print("俯仰闭环：%s" % r)
        ok = g.goto(pan=args.pan, tilt=args.tilt, blocking=True,
                    timeout=args.timeout, settle=not args.no_settle)
        st = g.state()
        print("到位=%s  pan=%.1f°  tilt=%.1f°  实测俯仰=%s  实测滚转=%s"
              % (ok, st.get("pan_target", 0), st.get("tilt_target", 0),
                 st.get("camera_pitch"), st.get("camera_roll")))
        if args.hold > 0:
            time.sleep(args.hold)
    finally:
        g.stop(relax=not args.hold_power)
        hw.close()
    return 0


def cmd_presets(cfg, cfg_path, args):
    g, hw = build(cfg)
    if g is None:
        return 1
    g.start()
    try:
        for p in cfg.get("presets", {}).get("pan", [0, 90, 180, 270]):
            print("→ 方位 %.0f°" % p)
            g.goto(pan=float(p), blocking=True, timeout=25)
            time.sleep(0.3)
    finally:
        g.stop(relax=False)
        hw.close()
    return 0


def cmd_level(cfg, cfg_path, args):
    g, hw = build(cfg)
    if g is None:
        return 1
    g.start()
    try:
        for _ in range(max(1, args.samples)):
            lg = g.level_gate(max_deg=args.max_deg)
            print("水平门禁：%s  左右倾=%s  前后倾=%s  阈值=%.2f°"
                  % ("通过" if lg["ok"] else "不通过",
                     None if lg["roll"] is None else round(lg["roll"], 2),
                     None if lg["pitch"] is None else round(lg["pitch"], 2),
                     lg["max_deg"]))
            if not lg["ok"]:
                print("  原因：%s" % lg["reason"])
            time.sleep(0.2)
    finally:
        g.stop(relax=False)
        hw.close()
    return 0


def cmd_monitor(cfg, cfg_path, args):
    g, hw = build(cfg)
    if g is None:
        return 1
    g.start(park=(0, 0))
    try:
        print("实时监控（Ctrl-C 结束）。| pan | tilt | 指令pitch | 实测pitch | 实测roll |")
        while True:
            st = g.state()
            print("| %5.1f | %5.1f | %8.1f | %9s | %8s |"
                  % (st.get("pan", 0), st.get("tilt", 0),
                     st.get("tilt_target", 0),
                     "--" if st.get("camera_pitch") is None else "%.2f" % st["camera_pitch"],
                     "--" if st.get("camera_roll") is None else "%.2f" % st["camera_roll"]))
            time.sleep(1.0 / max(1, args.hz))
    except KeyboardInterrupt:
        pass
    finally:
        g.stop(relax=False)
        hw.close()
    return 0


def cmd_scan(cfg, cfg_path, args):
    g, hw = build(cfg)
    if g is None:
        return 1
    g.start()
    cap_dir = args.capture_dir
    if cap_dir and not os.path.isdir(cap_dir):
        os.makedirs(cap_dir)

    def capture(pose):
        # 真实项目在这里替换成：取图 + 质量门禁 + 存盘
        if not cap_dir:
            return None
        name = "r%d_pan%03d_tilt%+03d.json" % (pose["ring"], int(pose["pan"]),
                                               int(pose["tilt_cmd"]))
        p = os.path.join(cap_dir, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(pose, f, ensure_ascii=False, indent=2)
        return p

    try:
        m = run_scan(g, capture_fn=capture, out_json=args.out, cfg=cfg,
                     use_closed_loop=None if args.closed_loop is None else args.closed_loop,
                     dry_run=args.dry_run, mode=args.mode,
                     tilt=args.tilt, azimuths=args.azimuths)
        print("模式=%s 扫描位姿数：%d，门禁通过：%d"
              % (m.get("mode"), len(m["poses"]),
                 sum(1 for x in m["poses"] if x.get("gate_ok"))))
        return 0 if m["ok"] else 2
    finally:
        g.stop(relax=False)
        hw.close()


def cmd_voice(cfg, cfg_path, args):
    from .mic import SherpaASR, Speaker, have, press_to_talk_loop, record_wav
    from .voice import Dispatcher, parse, run_text_loop
    from .scan import run_scan

    g, hw = build(cfg)
    if g is None:
        return 1
    g.start()
    speaker = Speaker(enabled=not args.no_tts)

    def on_capture():
        ok, reasons = g.capture_gate()
        return "门禁通过，可以采集" if ok else "还不能采集：%s" % "、".join(reasons)

    def on_scan(mode="quick", tilt=None):
        m = run_scan(g, out_json=args.scan_out, cfg=cfg, mode=mode,
                     tilt=tilt, azimuths=args.scan_azimuths)
        if not m.get("ok"):
            d = m.get("detail") or {}
            return "没有开始拍：%s" % d.get("reason", "门禁未通过")
        n = len(m["poses"])
        ok = sum(1 for x in m["poses"] if x.get("gate_ok"))
        rings = m["plan"]["rings"]
        if len(rings) == 1:
            return "已拍 %d 张（%s，俯角 %.0f 度），门禁通过 %d/%d" % (
                n, m.get("label"), -rings[0], ok, n)
        return "已拍 %d 张（%s，%d 环），门禁通过 %d/%d" % (
            n, m.get("label"), len(rings), ok, n)

    disp = Dispatcher(g, on_capture=on_capture, on_scan=on_scan, say=speaker.say)
    try:
        if args.text:
            for t in args.text:
                it = parse(t)
                print("%s → %r" % (t, it))
                print("  " + disp.handle(it))
            return 0

        if args.asr == "off":
            print("键盘模式：直接输入指令（例如「转到90度」「俯角30度」）")
            run_text_loop(disp, sys.stdin, speaker.say)
        else:
            asr = None
            if args.asr == "sherpa":
                try:
                    asr = SherpaASR(args.model)
                except Exception as e:
                    print("ASR 初始化失败：%s" % e)
                    print("回退到键盘输入。")
            if not have("arecord"):
                print("没有 arecord，只能键盘输入。")
            press_to_talk_loop(disp, asr, speaker, seconds=args.seconds,
                               device=args.device)
    except KeyboardInterrupt:
        pass
    finally:
        g.stop(relax=not args.hold_power)
        hw.close()
    return 0


def cmd_calib(cfg, cfg_path, args):
    g, hw = build(cfg)
    if g is None:
        return 1
    g.start()
    try:
        axis = args.axis
        servo = g.pan if axis == "pan" else g.tilt
        sp = servo.spec
        lo, hi = sp.axis_limits()

        if args.direct_us is not None:
            print("直接输出 %.0f us 到 ch%d，观察舵机是否在中位附近（Ctrl-C 结束）"
                  % (args.direct_us, sp.channel))
            hw.backend.set_pulse_us(sp.channel, args.direct_us)
            time.sleep(args.hold)
            return 0

        if args.trim is not None:
            cfg["servos"][axis]["trim_us"] = float(args.trim)
            save_cfg(cfg, cfg_path)
            print("已把 %s.trim_us 设为 %.1f 并写入 %s" % (axis, args.trim, cfg_path))
            print("重新执行 pose 命令观察中位是否回正。")
            return 0

        if args.set_zero is not None:
            cfg["servos"][axis]["zero_deg"] = float(args.set_zero)
            save_cfg(cfg, cfg_path)
            print("已把 %s.zero_deg 设为 %.1f（该舵机角 = 轴角 0°）"
                  % (axis, args.set_zero))
            return 0

        print("在 [%.1f, %.1f]° 内来回扫，观察机械是否有干涉/异响（Ctrl-C 结束）"
              % (lo, hi))
        step = args.step
        a = lo
        direction = 1
        while True:
            g.goto(**{axis: a}, blocking=True, timeout=15)
            st = g.state()
            print("  %s=%.1f°  脉宽=%.0f us  实测俯仰=%s"
                  % (axis, a, servo._us,
                     "--" if st.get("camera_pitch") is None else "%.2f" % st["camera_pitch"]))
            a += direction * step
            if a > hi:
                a, direction = hi, -1
            elif a < lo:
                a, direction = lo, 1
            time.sleep(args.dwell)
    except KeyboardInterrupt:
        pass
    finally:
        g.stop(relax=not args.hold_power)
        hw.close()
    return 0


def cmd_selftest(cfg, cfg_path, args):
    from .selftest import run_all
    return 0 if run_all() else 1


# ---------------- 入口 ----------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="gimbal", description="K1 双轴云台控制（PCA9685 + MPU6050）")
    ap.add_argument("--config", default=None, help="配置文件路径，默认包内 config.json")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("detect", help="探测 I2C / PCA9685 / MPU6050")
    sub.add_parser("config", help="打印当前配置解析结果")
    sub.add_parser("presets", help="依次走到四个预设方位")
    sub.add_parser("selftest", help="离线自检（不需要硬件）")

    p = sub.add_parser("pose", help="走到指定位姿")
    p.add_argument("--pan", type=float, default=None)
    p.add_argument("--tilt", type=float, default=None)
    p.add_argument("--pitch", type=float, default=None,
                   help="用 MPU6050 闭环到目标俯仰角")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--hold", type=float, default=0.0, help="到位后保持秒数")
    p.add_argument("--no-settle", action="store_true")
    p.add_argument("--hold-power", action="store_true", help="退出后不卸力")
    p.add_argument("--verbose", action="store_true")

    p = sub.add_parser("level", help="三脚架水平门禁")
    p.add_argument("--max-deg", type=float, default=None)
    p.add_argument("--samples", type=int, default=3)

    p = sub.add_parser("monitor", help="实时打印姿态")
    p.add_argument("--hz", type=float, default=2.0)

    p = sub.add_parser("scan", help="扫描：默认快速四张（4 方位 × 俯角 30°）")
    p.add_argument("--mode", choices=["quick", "full"], default=None,
                   help="quick=4 张（默认）；full=三环 12 张")
    p.add_argument("--tilt", type=float, default=None,
                   help="覆盖俯仰角（仅单环有效），例如 -30；负值=向下")
    p.add_argument("--azimuths", type=int, default=None, help="方位数，默认 4")
    p.add_argument("--out", default="scan_manifest.json")
    p.add_argument("--capture-dir", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--closed-loop", dest="closed_loop", action="store_true", default=None)
    p.add_argument("--open-loop", dest="closed_loop", action="store_false")

    p = sub.add_parser("voice", help="语音/文字控制")
    p.add_argument("--asr", choices=["off", "sherpa"], default="off")
    p.add_argument("--model", default="/opt/models/sherpa-zh")
    p.add_argument("--device", default="default")
    p.add_argument("--seconds", type=float, default=3.0)
    p.add_argument("--no-tts", action="store_true")
    p.add_argument("--text", nargs="*", help="一次性执行若干条文字指令")
    p.add_argument("--scan-out", default="scan_manifest.json")
    p.add_argument("--scan-azimuths", type=int, default=None,
                   help="语音触发拍摄时的方位数，默认按配置（4）")
    p.add_argument("--hold-power", action="store_true")

    p = sub.add_parser("calib", help="扫描/微调/写回零位")
    p.add_argument("--axis", choices=["pan", "tilt"], default="pan")
    p.add_argument("--step", type=float, default=5.0)
    p.add_argument("--dwell", type=float, default=0.6)
    p.add_argument("--direct-us", type=float, default=None)
    p.add_argument("--trim", type=float, default=None)
    p.add_argument("--set-zero", type=float, default=None)
    p.add_argument("--hold", type=float, default=5.0)
    p.add_argument("--hold-power", action="store_true")

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 0

    cfg, cfg_path = load_cfg(args.config)
    table = {
        "detect": cmd_detect, "config": cmd_config, "presets": cmd_presets,
        "pose": cmd_pose, "level": cmd_level, "monitor": cmd_monitor,
        "scan": cmd_scan, "voice": cmd_voice, "calib": cmd_calib,
        "selftest": cmd_selftest,
    }
    return table[args.cmd](cfg, cfg_path, args) or 0


if __name__ == "__main__":
    sys.exit(main())
