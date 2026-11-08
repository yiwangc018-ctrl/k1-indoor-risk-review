#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新板验收（PC 侧）：通过 SSH 跑一遍旧板跑过的关键项，留下 JSON/Markdown 证据。

背景
----
2026-11-08 旧板损坏，新板已于当日上午到手。板端恢复脚本
`scripts/board/new_board_bringup.py` 负责“把东西装回去”；本脚本负责在新板到手后
从 PC 侧**独立验收**：身份、文件、设备、服务、导入、云台自检、音频自检、VLM 健康。
默认只读，不改板端；`--capture` 才会抓一张测试图到 /tmp。

用法
----
    python scripts/pc/accept_new_board.py --host 172.20.10.2
    python scripts/pc/accept_new_board.py --host 172.20.10.2 --smoke
    python scripts/pc/accept_new_board.py --host 172.20.10.2 --smoke --capture
    python scripts/pc/accept_new_board.py --dry-run        # 不连板，只列检查项

退出码：0 = 无 FAIL；1 = 有 FAIL；2 = 连接/参数错误。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ROOT = HERE.parents[1]
REMOTE_ROOT = "/home/biandu/rv/room_review"
OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"
ICONS = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", SKIP: "[SKIP]"}

REQUIRED_REMOTE = [
    "/home/biandu/rv/room_review/app/room_review.py",
    "/home/biandu/rv/room_review/app/asr_client.py",
    "/home/biandu/rv/room_review/app/asr_sensevoice.py",
    "/home/biandu/rv/room_review/app/gimbal_flow30.py",
    "/home/biandu/rv/room_review/configs/room_review_k1.json",
    "/home/biandu/rv/room_review/configs/risk_assessment_v1_08b/system_prompt.zh.txt",
    "/home/biandu/rv/room_review/configs/vlm_smt_0.8b_224/config.json",
    "/home/biandu/rv/room_review/scripts/board/start_room_review.sh",
    "/home/biandu/rv/room_review/scripts/board/voice_gimbal30.sh",
    "/home/biandu/rv/room_review/scripts/board/asr_service.py",
    "/home/biandu/rv/room_review/audio/start.wav",
    "/home/biandu/rv/gimbal/config.json",
    "/home/biandu/.cache/models/vlm/Qwen3.5-0.8B/config.json",
    "/home/biandu/.cache/models/vlm/Qwen3.5-0.8B/qwen3_5vl_0.8b-text-q41.gguf",
    "/home/biandu/rv/asr/sensevoice/sensevoice/model_quant_optimized.onnx",
    "/home/biandu/rv/asr/sensevoice/sensevoice/tokens.txt",
    "/home/biandu/rv/validation_20261103/runtime_archives/spacemit-llama.cpp.riscv64.0.1.7/bin/llama-server",
    "/home/biandu/rv/validation_20261103/runtime_archives/spacemit-ort.riscv64.2.0.6/lib",
]

# name -> 远端命令。全部只读；--smoke/--capture 才追加耗时的功能项。
COMMANDS = {
    "identity": (
        "printf 'ARCH=%s\\n' \"$(uname -m)\"; "
        "printf 'MODEL=%s\\n' \"$(tr -d '\\0' < /proc/device-tree/model 2>/dev/null)\"; "
        "printf 'OS=%s\\n' \"$(. /etc/os-release 2>/dev/null; echo \"$PRETTY_NAME\")\"; "
        "printf 'KERNEL=%s\\n' \"$(uname -r)\"; "
        "printf 'PY=%s\\n' \"$(python3 -V 2>&1)\"; "
        "printf 'MEM_MB=%s\\n' \"$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo)\"; "
        "printf 'DISK=%s\\n' \"$(df -h / | awk 'NR==2{print $4}')\""
    ),
    "files": "for p in " + " ".join(f"'{p}'" for p in REQUIRED_REMOTE)
             + '; do if [ -e "$p" ]; then printf "OK|%s\\n" "$p"; '
               'else printf "MISS|%s\\n" "$p"; fi; done',
    "tools": ("for c in python3 v4l2-ctl arecord aplay i2cdetect wpctl ffmpeg vncserver curl; do "
              "printf '%s=%s\\n' \"$c\" \"$(command -v $c 2>/dev/null)\"; done"),
    "devices": ("printf 'VIDEO=%s\\n' \"$(ls /dev/video* 2>/dev/null | tr '\\n' ' ')\"; "
                "printf 'I2C=%s\\n' \"$(ls /dev/i2c-* 2>/dev/null | tr '\\n' ' ')\"; "
                "printf 'GPIO=%s\\n' \"$(ls /dev/gpiochip* 2>/dev/null | tr '\\n' ' ')\"; "
                "printf 'USB='; lsusb 2>/dev/null | tr '\\n' ';' ; echo"),
    "audio": ("printf 'CAPTURE=%s\\n' \"$(arecord -l 2>/dev/null | grep -c '^card')\"; "
              "printf 'PLAYBACK=%s\\n' \"$(aplay -l 2>/dev/null | grep -c '^card')\""),
    "services": ("printf 'VNC=%s\\n' \"$(systemctl --user is-active tigervnc 2>/dev/null)\"; "
                 "printf 'PORT5901=%s\\n' \"$(ss -ltn 2>/dev/null | grep -q ':5901' && echo yes || echo no)\"; "
                 "printf 'PIPEWIRE_MAX=%s\\n' \"$(systemctl --user show pipewire-pulse -p MemoryMax --value 2>/dev/null)\""),
    "vlm_health": "curl -fsS --max-time 2 http://127.0.0.1:8099/health 2>/dev/null || true",
    "imports": (f"cd {REMOTE_ROOT} && python3 -c \"import sys; sys.path.insert(0, 'app'); "
                "import room_review, asr_client, gimbal_flow30, photo_pipeline; print('imports-ok')\""),
    "onnxruntime": "python3 -c \"import onnxruntime; print(onnxruntime.__version__)\" 2>&1",
    "gimbal": "cd /home/biandu/rv && python3 -m gimbal selftest 2>&1",
    "audio_check": f"cd {REMOTE_ROOT} && python3 app/room_review.py audio-check 2>&1",
    "capture": (
        "DEV=$(ls /dev/v4l/by-id/*UGREEN* 2>/dev/null | head -1); "
        "[ -n \"$DEV\" ] || DEV=$(ls /dev/video* 2>/dev/null | head -1); "
        "[ -n \"$DEV\" ] || DEV=/dev/video0; "
        "rm -f /tmp/newboard_accept.jpg; "
        "v4l2-ctl --device \"$DEV\" --set-fmt-video=width=1920,height=1080,pixelformat=MJPG "
        "--stream-mmap --stream-count=1 --stream-to=/tmp/newboard_accept.jpg >/dev/null 2>&1; "
        "if [ -s /tmp/newboard_accept.jpg ]; then stat -c 'BYTES=%s' /tmp/newboard_accept.jpg; "
        "else echo 'CAPTURE_FAILED'; fi"
    ),
}


def result(name, status, detail=""):
    return {"name": name, "status": status, "detail": detail}


def eval_identity(out):
    data = {}
    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()
    checks = [
        result("架构 riscv64", OK if data.get("ARCH") == "riscv64" else FAIL,
               data.get("ARCH", "读不到")),
        result("板型 MUSE Pi Pro / K1",
               OK if ("MUSE" in data.get("MODEL", "").upper() or "K1" in data.get("MODEL", "").upper())
               else WARN, data.get("MODEL", "读不到")),
        result("系统 Bianbu", OK if "Bianbu" in data.get("OS", "") else WARN,
               data.get("OS", "读不到")),
        result("Python 3", OK if data.get("PY", "").startswith("Python 3") else FAIL,
               data.get("PY", "python3 不可用")),
    ]
    mem = data.get("MEM_MB", "")
    mem_ok = mem.isdigit() and int(mem) >= 7500
    checks.append(result("内存 ≥ 7.5GB", OK if mem_ok else WARN,
                         f"{mem or '未知'} MB，磁盘剩余 {data.get('DISK', '?')}"))
    return checks


def eval_files(out):
    checks = []
    for line in out.splitlines():
        if "|" not in line:
            continue
        status, path = line.split("|", 1)
        checks.append(result(f"文件 {path}", OK if status == "OK" else FAIL,
                             "" if status == "OK" else "缺失"))
    if not checks:
        checks.append(result("远端文件清单", FAIL, "没有解析到任何文件状态（命令可能没执行）"))
    return checks


def eval_tools(out):
    checks = []
    for line in out.splitlines():
        if "=" not in line:
            continue
        name, path = line.split("=", 1)
        checks.append(result(f"命令 {name}", OK if path.strip() else WARN,
                             path.strip() or "未安装"))
    if not checks:
        checks.append(result("远端命令清单", FAIL, "没有解析到任何命令状态"))
    return checks


def eval_devices(out):
    data = {}
    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key] = value
    checks = [
        result("摄像头 /dev/video*", OK if data.get("VIDEO", "").strip() else WARN,
               data.get("VIDEO", "").strip() or "未发现"),
        result("I2C /dev/i2c-*", OK if data.get("I2C", "").strip() else WARN,
               data.get("I2C", "").strip() or "未发现"),
        result("GPIO /dev/gpiochip*", OK if data.get("GPIO", "").strip() else WARN,
               data.get("GPIO", "").strip() or "未发现"),
    ]
    usb = data.get("USB", "").lower()
    for label, token in (("USB 摄像头 0c45:636b", "0c45:636b"),
                         ("USB 麦克风 08bb:2902", "08bb:2902"),
                         ("USB 音箱 4c4a:4155", "4c4a:4155")):
        checks.append(result(label, OK if token in usb else WARN,
                             "已枚举" if token in usb else "未看到（重插或用 lsusb 核对）"))
    return checks


def eval_audio(out):
    data = {}
    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key] = value.strip()
    capture = data.get("CAPTURE", "0")
    playback = data.get("PLAYBACK", "0")
    return [
        result("录音设备", OK if capture.isdigit() and int(capture) > 0 else WARN,
               f"{capture} 个 card"),
        result("播放设备", OK if playback.isdigit() and int(playback) > 0 else WARN,
               f"{playback} 个 card"),
    ]


def eval_services(out):
    data = {}
    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key] = value.strip()
    mem = data.get("PIPEWIRE_MAX", "")
    checks = [
        result("VNC 服务", OK if data.get("VNC") == "active" else WARN,
               data.get("VNC") or "未启用"),
        result("VNC 端口 5901", OK if data.get("PORT5901") == "yes" else WARN,
               "监听中" if data.get("PORT5901") == "yes" else "未监听"),
        result("pipewire 内存上限 512M", OK if mem == "536870912" else WARN,
               mem or "未设置"),
    ]
    if not data:
        checks.append(result("远端服务状态", FAIL, "没有解析到任何服务状态"))
    return checks


def eval_simple(name, ok, detail):
    return [result(name, OK if ok else WARN, detail)]


def run_remote(client, command, timeout_note=""):
    """k1.run 的薄包装；返回 (code, stdout, stderr)。"""
    import k1
    return k1.run(client, command, quiet=True)


def dry_run():
    print("=== 新板验收检查项（dry-run，不连接板子） ===")
    for name, command in COMMANDS.items():
        print(f"\n[{name}]\n  {command}")
    print("\n默认执行：identity/files/tools/devices/audio/services")
    print("--smoke 追加：vlm_health/imports/onnxruntime/gimbal/audio_check")
    print("--capture 追加：capture（写 /tmp/newboard_accept.jpg）")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="新板验收（PC 侧，SSH 只读）")
    parser.add_argument("--host", help="板子 IP（--dry-run 时可省略）")
    parser.add_argument("--user", default="biandu")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--key", default=str(Path.home() / ".ssh" / "id_k1_agent"))
    parser.add_argument("--out", type=Path, help="证据目录（默认 results/newboard_accept_<时间>）")
    parser.add_argument("--smoke", action="store_true", help="加跑导入/云台/音频自检")
    parser.add_argument("--capture", action="store_true", help="抓一张 1920x1080 测试图")
    parser.add_argument("--dry-run", action="store_true", help="只列检查项，不连接")
    parser.add_argument("--json", action="store_true", help="stdout 只输出 JSON 摘要")
    args = parser.parse_args(argv)
    if args.dry_run:
        return dry_run()
    if not args.host:
        parser.error("需要 --host（或用 --dry-run）")

    import k1  # 延迟导入：--dry-run 不要求 paramiko

    connect_args = argparse.Namespace(host=args.host, user=args.user, key=args.key, port=args.port)
    try:
        client = k1.connect(connect_args)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 连不上 {args.host}：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("检查：板子已开机联网、IP 正确、板端 ~/.ssh/authorized_keys 有 PC 公钥、"
              f"私钥存在 {args.key}", file=sys.stderr)
        return 2

    names = ["identity", "files", "tools", "devices", "audio", "services"]
    if args.smoke:
        names += ["vlm_health", "imports", "onnxruntime", "gimbal", "audio_check"]
    if args.capture:
        names.append("capture")

    checks, raw = [], []
    try:
        outputs = {}
        for name in names:
            command = COMMANDS[name]
            try:
                code, out, err = run_remote(client, command)
            except Exception as exc:  # noqa: BLE001  单项失败不应拖垮整轮
                code, out, err = 1, "", f"{type(exc).__name__}: {exc}"
            outputs[name] = (code, out, err)
            raw.append(f"$ [{name}] {command}\nexit={code}\n{out}\n{err}\n")
    finally:
        client.close()

    for name in names:
        code, out, err = outputs[name]
        if name == "identity":
            checks += eval_identity(out)
        elif name == "files":
            checks += eval_files(out)
        elif name == "tools":
            checks += eval_tools(out)
        elif name == "devices":
            checks += eval_devices(out)
        elif name == "audio":
            checks += eval_audio(out)
        elif name == "services":
            checks += eval_services(out)
        elif name == "vlm_health":
            checks += eval_simple("常驻 VLM 健康检查", bool(out.strip()),
                                  out.strip()[:200] or "未启动（首次分析会自动拉起）")
        elif name == "imports":
            checks += eval_simple("应用模块可导入", "imports-ok" in out,
                                  (out.strip() or err.strip())[-300:])
        elif name == "onnxruntime":
            checks += eval_simple("onnxruntime", code == 0, out.strip() or err.strip())
        elif name == "gimbal":
            checks += eval_simple("云台离线自检", code == 0, (out.strip() or err.strip())[-300:])
        elif name == "audio_check":
            checks += eval_simple("音频自检 audio-check", code == 0, (out.strip() or err.strip())[-300:])
        elif name == "capture":
            match = re.search(r"BYTES=(\d+)", out)
            size = int(match.group(1)) if match else 0
            checks += eval_simple("抓图 1920×1080（>100KB）", size > 100_000,
                                  f"{size} B" if size else (out.strip() or err.strip())[-200:])

    counts = {status: sum(1 for c in checks if c["status"] == status)
              for status in (OK, WARN, FAIL, SKIP)}
    out_dir = args.out or (ROOT / "results" / f"newboard_accept_{time.strftime('%Y%m%d_%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    created = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    payload = {"created": created, "host": args.host, "smoke": args.smoke,
               "capture": args.capture, "checks": checks, "counts": counts}
    (out_dir / "acceptance.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# 新板验收 {args.host}", "", f"- 时间：{created}",
             f"- smoke：{args.smoke}；capture：{args.capture}",
             f"- 结果：OK {counts[OK]} / WARN {counts[WARN]} / FAIL {counts[FAIL]}", "",
             "| 状态 | 检查项 | 详情 |", "|---|---|---|"]
    for check in checks:
        detail = str(check["detail"]).replace("|", "\\|")[:160]
        lines.append(f"| {check['status']} | {check['name']} | {detail} |")
    (out_dir / "acceptance.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "raw.log").write_text("".join(raw), encoding="utf-8", errors="replace")

    print(f"=== 新板验收：{args.host} ===")
    if not args.json:
        for check in checks:
            print(f"  {ICONS[check['status']]} {check['name']}"
                  + (f"：{check['detail']}" if check["detail"] else ""))
        print(f"\nOK {counts[OK]}  WARN {counts[WARN]}  FAIL {counts[FAIL]}")
        print(f"证据：{out_dir.resolve()}")
    else:
        print(json.dumps(payload, ensure_ascii=False))
    return 0 if counts[FAIL] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
