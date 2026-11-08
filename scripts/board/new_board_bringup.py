#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新板一键恢复（K1 MUSE Pi Pro / Bianbu 2.3.5 默认 XFCE / 用户 biandu）。

背景
----
2026-11-08 旧板损坏，换成规格完全相同的新板。旧板上真正需要恢复的东西
分三类：

1. 仓库里已有的：应用代码、配置、脚本、提示音、运行时压缩包、whisper 模型；
2. 仓库里没有、但可从官方源下载的：Qwen3.5-0.8B、SenseVoice；
3. 必须在板端重新安装/生成的：apt 包、Python 依赖、VNC 服务、udev 规则、
   用户组、pipewire 内存上限、桌面快捷方式。

本脚本把第 2、3 类和“把第 1 类拷到正确位置”串成幂等步骤。默认只做检查
（plan/verify），真正改动要显式 `install`。

用法
----
    python3 scripts/board/new_board_bringup.py plan
    python3 scripts/board/new_board_bringup.py install --bundle ~/rv/offline/bundle
    python3 scripts/board/new_board_bringup.py install --offline      # 不联网
    python3 scripts/board/new_board_bringup.py verify --json
    python3 scripts/board/new_board_bringup.py inventory              # 写板端盘点 JSON

边界
----
* 不是“已验收”：本脚本在旧板损坏、新板未到的情况下写成，只在 PC 上做过
  干跑和单测；板端结果以 `verify` 输出为准。
* 不烧录系统镜像、不改设备树、不装内核驱动（摄像头是 UVC，云台走 I2C）。
* 不恢复旧板未同步的临时文件；那部分只能从旧板存储里抢救。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

HOME = Path.home()
BOARD = HOME / "rv"
ROOM = BOARD / "room_review"
GIMBAL = BOARD / "gimbal"
ASR_ROOT = BOARD / "asr"
ASR_MODEL = ASR_ROOT / "sensevoice" / "sensevoice"
RUNTIME = BOARD / "validation_20261103" / "runtime_archives"
CACHE_VLM = HOME / ".cache" / "models" / "vlm"
QWEN_DIR = CACHE_VLM / "Qwen3.5-0.8B"
OFFLINE = BOARD / "offline"
MODELS = OFFLINE / "models"

LLAMA_DIR = RUNTIME / "spacemit-llama.cpp.riscv64.0.1.7"
ORT_DIR = RUNTIME / "spacemit-ort.riscv64.2.0.6"

QWEN_TAR = "Qwen3.5-0.8B.tar.gz"
QWEN_URL = "http://archive.spacemit.com/spacemit-ai/model_zoo/vlm/Qwen3.5-0.8B.tar.gz"
QWEN_MD5 = "e780db6364024d9315eaad50e613421d"
QWEN_SHA256 = "be44a9d2350c0668bbd830cf79d48c6a1ce5d691bb3c6e8e702dcb58a994d2ca"

SENSEVOICE_TAR = "sensevoice.tar.gz"
SENSEVOICE_URL = "https://archive.spacemit.com/spacemit-ai/openwebui/sensevoice.tar.gz"
SENSEVOICE_SHA256 = "3e54c9cb42beef5d37dc38c3cc87248ee6e9615d143112e0fd8afd5b6c73ff35"

TARGET_USER = "biandu"

APT_PACKAGES = [
    "python3-numpy", "python3-pil", "python3-pillow", "python3-pip",
    "v4l-utils", "alsa-utils", "i2c-tools", "ffmpeg",
    "tigervnc-standalone-server", "tigervnc-common", "openbox",
    "desktop-file-utils", "dbus-x11", "dbus-user-session", "qterminal",
    "curl", "wget", "unzip",
]

REQUIRED_FILES = [
    ROOM / "app/room_review.py",
    ROOM / "app/asr_client.py",
    ROOM / "app/asr_sensevoice.py",
    ROOM / "app/gimbal_scan.py",
    ROOM / "app/photo_pipeline.py",
    ROOM / "configs/room_review_k1.json",
    ROOM / "configs/risk_assessment_v1_08b/system_prompt.zh.txt",
    ROOM / "configs/vlm_smt_0.8b_224/config.json",
    ROOM / "scripts/board/start_room_review.sh",
    ROOM / "scripts/board/scan_gimbal.sh",
    ROOM / "scripts/board/asr_service.py",
    ROOM / "scripts/board/ensure_room_vlm.sh",
    ROOM / "audio/start.wav",
    GIMBAL / "config.json",
    QWEN_DIR / "config.json",
    QWEN_DIR / "qwen3_5vl_0.8b-text-q41.gguf",
    ASR_MODEL / "model_quant_optimized.onnx",
    ASR_MODEL / "tokens.txt",
    LLAMA_DIR / "bin/llama-server",
    ORT_DIR / "lib",
]

VNC_UNIT = """[Unit]
Description=TigerVNC server :1 (room review)
After=network.target

[Service]
Type=forking
ExecStartPre=-/usr/bin/vncserver -kill :1
ExecStart=/usr/bin/vncserver :1 -geometry 1600x900 -depth 24 -AlwaysShared -SecurityTypes None -localhost no --I-KNOW-THIS-IS-INSECURE
ExecStop=/usr/bin/vncserver -kill :1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""

XSTARTUP = """#!/bin/sh
# 由 scripts/board/new_board_bringup.py 生成（2026-11-08 新板重建）。
# 新板镜像默认桌面是 XFCE（SDDM autologin Session=xfce），VNC 也用 XFCE，
# 保证 VNC 和接 HDMI 时看到的是同一套桌面。
# 不要用 gnome-session：用户级 systemd 会话里拿不到 systemd user bus，
# 会掉进 gnome-session-failed（“Oh no! Something has gone wrong”）。
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
export XDG_SESSION_TYPE=x11
export XDG_CURRENT_DESKTOP=XFCE
export XDG_SESSION_DESKTOP=xfce
export GDK_BACKEND=x11
export LANG="${LANG:-zh_CN.UTF-8}"

command -v xset >/dev/null 2>&1 && xset s off -dpms s noblank >/dev/null 2>&1

exec dbus-run-session -- startxfce4
"""

VNC_NOTE = """K1 远程桌面（VNC）说明
======================
更新：2026-11-08（新板，用户名 biandu）

显示 :1 / 端口 5901，分辨率 1600x900，桌面 XFCE，无密码。

Windows 端连接：
  TigerVNC 查看器输入   k1.local:1
  或                    k1.local:5901
IP 由热点 DHCP 分配，会变；用 k1.local（mDNS）就不用改。解析失败时在 PC 上
跑 scripts/pc/find-k1.ps1 找当前 IP。

启动/停止/排错（板端 SSH，用户 biandu）：
  systemctl --user status tigervnc
  systemctl --user restart tigervnc
  journalctl --user -u tigervnc -n 50 --no-pager
  ss -ltn | grep 5901
  日志：~/.vnc/k1:1.log

开机自启：用户服务 + loginctl enable-linger biandu，重启后不需要登录。

安全边界：无密码且监听所有网卡，只用于自家热点/可信局域网；
换到校园网/公共网络前先加密码：
  vncpasswd
  然后把服务文件里的 -SecurityTypes None 换成 -SecurityTypes VncAuth
  （~/.config/systemd/user/tigervnc.service），再 systemctl --user restart tigervnc。

已知坑：
- 不要用 gnome-session 做 VNC 会话：用户级 systemd 里会失败，显示
  “Oh no! Something has gone wrong”。本板默认桌面是 XFCE，xstartup 用 startxfce4。
- 本地 :0 是 SDDM 自动登录的 bianbu 桌面；VNC :1 是 biandu 的独立虚拟会话。
  项目文件在 /home/biandu/rv/room_review，用 biandu 才能直接跑。
"""

# VNC 会话里的两个“会弹出密码框/锁屏”的坑：colord 的 polkit 授权和 light-locker。
# 两者都只影响虚拟显示，按用户/按会话收窄处理，不放宽整机权限。
POLKIT_COLOR_RULE = """// K1 VNC 用：只允许 biandu 处理 colord 的颜色管理请求，避免 VNC 桌面弹出
// “Authentication is required to create a color managed device”密码框。
// biandu 本身已有 NOPASSWD sudo，这里只收窄到 color-manager 动作。
polkit.addRule(function(action, subject) {
    if (subject.user == "biandu" &&
        action.id.indexOf("org.freedesktop.color-manager.") === 0) {
        return polkit.Result.YES;
    }
});
"""

LIGHT_LOCKER_AUTOSTART = """[Desktop Entry]
Type=Application
Name=Screen Locker
Comment=Disabled for VNC session (avoid password lock on virtual display)
Exec=light-locker
NoDisplay=true
Hidden=true
"""

UDEV_RULES = {
    "90-k1-gpio.rules": 'KERNEL=="gpiochip*", MODE="0666"\nKERNEL=="ttyACM*", MODE="0666"\nKERNEL=="ttyUSB*", MODE="0666"\n',
    "99-i2c.rules": 'KERNEL=="i2c-*", MODE="0666"\n',
}

PIPEWIRE_LIMIT = """[Service]
MemoryHigh=256M
MemoryMax=512M
"""

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"
DRY = False
RESULTS = []


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def mark(status, name, detail=""):
    RESULTS.append({"status": status, "name": name, "detail": detail})
    icons = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", SKIP: "[SKIP]"}
    print(f"  {icons.get(status, '[????]')} {name}" + (f"：{detail}" if detail else ""), flush=True)


def section(name):
    print(f"\n=== {name} ===", flush=True)


def run(cmd, timeout=300, quiet=False, cwd=None):
    """执行命令，返回 (code, stdout, stderr)；找不到命令不抛异常。"""
    if cwd and not Path(cwd).is_dir():
        cwd = None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              errors="replace", cwd=cwd)
    except FileNotFoundError:
        return 127, "", f"命令不存在：{cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"超时 {timeout}s：{' '.join(cmd)}"
    if not quiet:
        print("       $ " + " ".join(cmd), flush=True)
    return proc.returncode, proc.stdout, proc.stderr


def sudo(cmd, timeout=600, quiet=False):
    if DRY:
        print("     [dry-run] sudo " + " ".join(cmd), flush=True)
        return 0, "", ""
    return run(["sudo"] + cmd, timeout=timeout, quiet=quiet)


def sha256_file(path, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def md5_file(path, chunk=1024 * 1024):
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_md5_file(text):
    """官方 .md5 形如 `e780...  ./Qwen3.5-0.8B.tar.gz`。"""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        return line.split()[0].lower()
    return ""


def write_user_file(path, content, mode=0o644):
    path = Path(path)
    if DRY:
        print(f"     [dry-run] 写 {path}（{len(content)} B）", flush=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(mode)
    except OSError:
        pass


def write_root_file(target, content, mode="0644"):
    """写任意需要 root 的系统文件；先落 /tmp 再 sudo install。"""
    target = Path(target)
    if DRY:
        print(f"     [dry-run] sudo install {target}", flush=True)
        return
    tmp = Path("/tmp") / (target.name + ".new")
    tmp.write_text(content, encoding="utf-8")
    code, _, err = sudo(["install", "-m", mode, str(tmp), str(target)])
    tmp.unlink(missing_ok=True)
    if code != 0:
        raise RuntimeError(f"写 {target} 失败：{err.strip()}")


def write_etc_file(name, content):
    """写 /etc/udev/rules.d/<name>；需要 sudo。"""
    target = Path("/etc/udev/rules.d") / name
    if DRY:
        print(f"     [dry-run] sudo tee {target}", flush=True)
        return
    tmp = Path("/tmp") / (name + ".new")
    tmp.write_text(content, encoding="utf-8")
    code, _, err = sudo(["install", "-m", "0644", str(tmp), str(target)])
    tmp.unlink(missing_ok=True)
    if code != 0:
        raise RuntimeError(f"写 {target} 失败：{err.strip()}")


def find_bundle(explicit=None):
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates += [OFFLINE / "bundle", Path.cwd(), BOARD]
    for candidate in candidates:
        if (candidate / "payload").is_dir() and (candidate / "manifest.json").is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# 检查
# ---------------------------------------------------------------------------
def identity():
    info = {}
    for key, cmd in {
        "arch": ["uname", "-m"],
        "kernel": ["uname", "-r"],
        "board": ["sh", "-c", "tr -d '\\0' < /proc/device-tree/model 2>/dev/null || echo unknown"],
        "os": ["sh", "-c", ". /etc/os-release 2>/dev/null && echo \"$PRETTY_NAME\""],
        "python": ["python3", "-V"],
        "cpu": ["nproc"],
        "mem_mb": ["sh", "-c", "awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo"],
        "disk": ["sh", "-c", "df -h / | awk 'NR==2{print $4}'"],
    }.items():
        code, out, _ = run(cmd, timeout=15, quiet=True)
        info[key] = out.strip() if code == 0 else ""
    # 在 PC 上跑 plan 时补一个可读的兜底，避免整张表全空。
    import platform
    info["arch"] = info["arch"] or platform.machine()
    info["kernel"] = info["kernel"] or platform.release()
    info["python"] = info["python"] or ("Python " + platform.python_version())
    info["cpu"] = info["cpu"] or str(os.cpu_count() or "")
    info["os"] = info["os"] or platform.platform()
    return info


def collect_checks(full=True):
    checks = []
    add = checks.append

    # 1. 身份
    info = identity()
    add({"name": "架构 riscv64", "status": OK if info["arch"] == "riscv64" else FAIL,
         "detail": info["arch"] or "读不到"})
    board_ok = ("MUSE" in info["board"].upper() or "K1" in info["board"].upper())
    add({"name": "板型 MUSE Pi Pro / K1", "status": OK if board_ok else WARN,
         "detail": info["board"] or "读不到 /proc/device-tree/model"})
    add({"name": "系统 Bianbu", "status": OK if "Bianbu" in info["os"] else WARN,
         "detail": info["os"] or "读不到 /etc/os-release"})
    add({"name": "Python 3", "status": OK if info["python"].startswith("Python 3") else FAIL,
         "detail": info["python"] or "python3 不可用"})
    mem = int(info["mem_mb"]) if info["mem_mb"].isdigit() else 0
    add({"name": "内存 ≥ 7.5GB（0.8B VLM 需要）", "status": OK if mem >= 7500 else WARN,
         "detail": f"{mem} MB，磁盘剩余 {info['disk']}"})
    add({"name": "用户/主目录符合配置 /home/biandu", "status": OK if str(HOME) == "/home/biandu" else FAIL,
         "detail": str(HOME)})

    # 2. 文件
    for path in REQUIRED_FILES:
        status = OK if path.exists() else FAIL
        detail = ""
        if path.is_file() and path.stat().st_size:
            detail = f"{path.stat().st_size} B"
        add({"name": f"文件 {path}", "status": status, "detail": detail})

    # 2b. 配置里的相机 by-id 路径（换板/换口后可能变）
    config_path = ROOM / "configs/room_review_k1.json"
    if config_path.is_file():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            camera = (cfg.get("camera") or {}).get("device", "")
        except (ValueError, OSError):
            camera = ""
        if camera:
            add({"name": "配置里的相机设备", "status": OK if Path(camera).exists() else WARN,
                 "detail": camera + ("" if Path(camera).exists() else "（不存在；用 ls /dev/v4l/by-id 核对并改配置）")})
        else:
            add({"name": "配置里的相机设备", "status": WARN, "detail": "配置没有 camera.device"})

    # 3. 设备
    def glob_status(pattern, label):
        found = sorted(str(p) for p in Path("/").glob(pattern.lstrip("/")))
        add({"name": label, "status": OK if found else WARN,
             "detail": ", ".join(found) if found else "未发现（未插设备或驱动未加载）"})

    glob_status("/dev/video*", "摄像头 /dev/video*")
    glob_status("/dev/i2c-*", "I2C /dev/i2c-*（PCA9685 云台）")
    glob_status("/dev/gpiochip*", "GPIO /dev/gpiochip*")
    code, out, _ = run(["sh", "-c", "arecord -l 2>/dev/null | grep -c '^card' || true"], quiet=True)
    add({"name": "录音设备（arecord）", "status": OK if code == 0 and out.strip() not in ("", "0") else WARN,
         "detail": (out.strip() or "0") + " 个"})
    code, out, _ = run(["sh", "-c", "lsusb 2>/dev/null || true"], quiet=True)
    usb = out.lower()
    for label, token in (("USB 摄像头 0c45:636b", "0c45:636b"),
                         ("USB 麦克风 08bb:2902", "08bb:2902"),
                         ("USB 音箱 4c4a:4155", "4c4a:4155")):
        add({"name": label, "status": OK if token in usb else WARN,
             "detail": "已枚举" if token in usb else "未看到（重插或用 lsusb 核对）"})

    # 4. 工具
    for tool in ("python3", "v4l2-ctl", "arecord", "aplay", "i2cdetect", "wpctl", "ffmpeg", "vncserver"):
        code, out, _ = run(["sh", "-c", f"command -v {tool} 2>/dev/null || true"], quiet=True)
        add({"name": f"命令 {tool}", "status": OK if out.strip() else WARN,
             "detail": out.strip() or "未安装"})

    if not full:
        return checks

    # 5. 服务与运行态（新板上用户级 systemd 可能没有 bus，必须限时，不能把 verify 挂死）
    code, out, _ = run(["sh", "-c", "systemctl --user is-active tigervnc 2>/dev/null || true"],
                       quiet=True, timeout=10)
    add({"name": "VNC 服务（tigervnc）", "status": OK if out.strip() == "active" else WARN,
         "detail": out.strip() or "未启用"})
    code, out, _ = run(["sh", "-c", "ss -ltn 2>/dev/null | grep -q ':5901' && echo yes || echo no"],
                       quiet=True, timeout=10)
    add({"name": "VNC 端口 5901", "status": OK if out.strip() == "yes" else WARN,
         "detail": "监听中" if out.strip() == "yes" else "未监听"})
    code, out, _ = run(["sh", "-c",
                        "systemctl --user show pipewire-pulse -p MemoryMax --value 2>/dev/null || true"],
                       quiet=True, timeout=10)
    add({"name": "pipewire-pulse 内存上限 512M", "status": OK if out.strip() == "536870912" else WARN,
         "detail": out.strip() or "未设置"})
    code, out, _ = run(["sh", "-c", "curl -fsS --max-time 2 http://127.0.0.1:8099/health 2>/dev/null || true"],
                       quiet=True)
    add({"name": "常驻 VLM 健康检查 :8099", "status": OK if out.strip() else WARN,
         "detail": out.strip() or "未启动（首次分析会自动拉起）"})

    # 6. 导入与自检
    code, out, err = run(["python3", "-c",
                          "import sys; sys.path.insert(0, 'app'); "
                          "import room_review, asr_client, gimbal_scan, photo_pipeline; print('imports-ok')"],
                         timeout=60, cwd=str(ROOM))
    add({"name": "应用模块可导入", "status": OK if code == 0 and "imports-ok" in out else FAIL,
         "detail": (out.strip() or err.strip())[-300:]})
    code, out, err = run(["python3", "-c", "import numpy, PIL; print('deps-ok')"], timeout=60)
    add({"name": "Python 依赖 numpy/Pillow", "status": OK if code == 0 else FAIL,
         "detail": (out.strip() or err.strip())[-300:]})
    code, out, err = run(["python3", "-c", "import onnxruntime; print(onnxruntime.__version__)"], timeout=60)
    add({"name": "onnxruntime（SenseVoice 用）", "status": OK if code == 0 else FAIL,
         "detail": (out.strip() or err.strip())[-300:]})
    code, out, err = run(["python3", "-m", "gimbal", "selftest"], timeout=90, cwd=str(BOARD))
    add({"name": "云台离线自检（71 项）", "status": OK if code == 0 else WARN,
         "detail": ((out.strip().splitlines() or [""])[-1] + " " + err.strip())[-300:]})
    code, out, err = run(["python3", "app/room_review.py", "audio-check"], timeout=45, cwd=str(ROOM))
    add({"name": "音频自检 audio-check", "status": OK if code == 0 else WARN,
         "detail": ((out.strip().splitlines() or [""])[-1] + " " + err.strip())[-300:]})
    return checks


def summarize(checks, title):
    counts = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0}
    for check in checks:
        counts[check["status"]] = counts.get(check["status"], 0) + 1
    print(f"\n=== {title} ===")
    print(f"  OK {counts[OK]}  WARN {counts[WARN]}  FAIL {counts[FAIL]}  SKIP {counts[SKIP]}")
    return counts


# ---------------------------------------------------------------------------
# install 步骤
# ---------------------------------------------------------------------------
def step_preflight(args):
    section("preflight")
    info = identity()
    if sys.platform != "linux" and not DRY:
        mark(FAIL, "非 Linux 环境", "install 必须在板端跑；PC 上只能 plan/--dry-run")
        return False
    if info["arch"] != "riscv64":
        if DRY:
            mark(WARN, "不是 riscv64", f"{info['arch'] or '未知'}；dry-run 继续，真机必须是 K1/Bianbu")
        else:
            mark(FAIL, "不是 riscv64", f"{info['arch']}；新板必须是 K1/Bianbu")
            return False
    if str(HOME) != f"/home/{TARGET_USER}" and not args.allow_other_home:
        message = ("配置里的绝对路径全部写死，换用户会全错；"
                   "如确要这样跑，加 --allow-other-home")
        if DRY:
            mark(WARN, f"HOME 不是 /home/{TARGET_USER}", message)
        else:
            mark(FAIL, f"HOME 不是 /home/{TARGET_USER}", message)
            return False
    mark(OK, "运行环境", f"{info['board'] or info['arch']} / {info['os']} / {info['python']}")
    return True


def step_dirs(args):
    section("dirs：目录骨架")
    for path in (ROOM, GIMBAL, ASR_ROOT, RUNTIME, MODELS,
                 ROOM / "audio", ROOM / "logs", CACHE_VLM):
        if DRY:
            print(f"     [dry-run] mkdir -p {path}", flush=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
    mark(OK, "目录骨架", f"{ROOM}、{GIMBAL}、{ASR_ROOT}、{RUNTIME}、{MODELS}")
    return True


def step_payload(args):
    section("payload：把离线包拷到板端")
    bundle = find_bundle(args.bundle)
    if not bundle:
        mark(WARN, "未找到离线包", "跳过；可只用仓库文件手工部署，或加 --bundle 指向解压目录")
        return True
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest.get("entries", [])
    if not args.skip_sha:
        bad = []
        for entry in entries:
            payload = bundle / entry.get("payload", "")
            if not payload.is_file():
                bad.append(f"缺 {payload}")
                continue
            if entry.get("sha256") and sha256_file(payload) != entry["sha256"]:
                bad.append(f"sha256 不符 {payload}")
        if bad:
            mark(FAIL, "离线包校验", "; ".join(bad[:5]))
            return False
        mark(OK, "离线包校验", f"{len(entries)} 个文件")
    if DRY:
        mark(OK, "将执行", "sudo cp -a payload/. /")
        return True
    code, _, err = sudo(["cp", "-a", str(bundle / "payload") + "/.", "/"])
    if code != 0:
        mark(FAIL, "拷贝 payload 失败", err.strip()[:300])
        return False
    sudo(["chown", "-R", f"{TARGET_USER}:{TARGET_USER}", f"/home/{TARGET_USER}"], timeout=600)
    mark(OK, "payload 已落到板端", str(bundle))
    return True


def step_runtime(args):
    section("runtime：解压 K1 运行时")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    for tar_name, expect in (("llama_017.tar.gz", LLAMA_DIR / "bin/llama-server"),
                             ("ort_206.tar.gz", ORT_DIR / "lib")):
        tar_path = RUNTIME / tar_name
        if not tar_path.exists():
            mark(WARN, f"缺 {tar_name}", "离线包里应带；没有就跳过，VLM/ORT 会不可用")
            continue
        if expect.exists():
            mark(OK, f"{tar_name} 已解压", str(expect))
            continue
        if DRY:
            mark(OK, "将解压", str(tar_path))
            continue
        code, out, err = run(["tar", "-xzf", str(tar_path), "-C", str(RUNTIME)], timeout=300)
        if code != 0:
            mark(FAIL, f"解压 {tar_name}", err.strip()[:300])
            continue
        mark(OK, f"解压 {tar_name}", "完成")
    for path in (LLAMA_DIR / "bin/llama-server", ORT_DIR / "lib"):
        mark(OK if path.exists() else FAIL, f"运行时 {path}", "存在" if path.exists() else "缺失")
    return True


def ensure_tar(path, url, sha256=None, md5=None, offline=False):
    if path.exists():
        if sha256 and sha256_file(path) != sha256:
            mark(FAIL, f"{path.name} 校验失败", "sha256 不符；删掉重下")
            return False
        if md5:
            md5_path = Path(str(path) + ".md5")
            if md5_path.exists():
                expected = parse_md5_file(md5_path.read_text(encoding="utf-8", errors="replace"))
                if expected and md5_file(path) != expected:
                    mark(FAIL, f"{path.name} 校验失败", ".md5 不符；删掉重下")
                    return False
        mark(OK, f"{path.name} 已存在", f"{path.stat().st_size} B")
        return True
    if offline:
        mark(FAIL, f"缺 {path.name}", "已指定 --offline，不联网下载")
        return False
    if DRY:
        mark(OK, f"将下载 {path.name}", url)
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    code, _, err = run(["curl", "-L", "--retry", "3", "--retry-delay", "2", "-C", "-",
                        "-o", str(path), url], timeout=3600)
    if code != 0:
        mark(FAIL, f"下载 {path.name}", err.strip()[:300])
        return False
    if sha256 and sha256_file(path) != sha256:
        mark(FAIL, f"{path.name} 校验失败", "sha256 不符，文件可能不完整")
        return False
    if md5 and md5_file(path) != md5:
        mark(FAIL, f"{path.name} 校验失败", "md5 不符")
        return False
    mark(OK, f"下载 {path.name}", f"{path.stat().st_size} B")
    return True


def write_smt_config():
    """按 ensure_room_vlm.sh 的规则生成 224 视觉输入的 SMT 配置。"""
    src = QWEN_DIR / "config.json"
    out = ROOM / "configs/vlm_smt_0.8b_224/config.json"
    if not src.is_file():
        mark(WARN, "SMT 配置未生成", "缺 Qwen3.5-0.8B/config.json")
        return False
    cfg = json.loads(src.read_text(encoding="utf-8"))
    for key in ("vision_model", "text_model"):
        path = Path(cfg[key]["model_path"])
        cfg[key]["model_path"] = str(path if path.is_absolute() else (src.parent / path).resolve())
    vision = Path(cfg["vision_model"]["model_path"])
    cfg["vision_model"]["model_path"] = str(vision.with_name(vision.name.replace("-384-", "-224-")))
    cfg["vision_model"]["input_size"] = 224
    write_user_file(out, json.dumps(cfg, ensure_ascii=False, indent=1))
    mark(OK, "SMT 224 配置", str(out))
    return True


def step_models(args):
    section("models：Qwen3.5-0.8B 与 SenseVoice")
    MODELS.mkdir(parents=True, exist_ok=True)
    qwen_tar = MODELS / QWEN_TAR
    sv_tar = MODELS / SENSEVOICE_TAR
    if not args.models_dir:
        models_dir = MODELS
    else:
        models_dir = Path(args.models_dir).expanduser()
    for name in (QWEN_TAR, SENSEVOICE_TAR):
        candidate = models_dir / name
        target = MODELS / name
        if candidate.exists() and candidate != target and not target.exists():
            if DRY:
                mark(OK, f"将复制 {name}", f"{candidate} -> {target}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidate, target)
            mark(OK, f"已从 {models_dir} 取到 {name}", str(target))

    got_qwen = (QWEN_DIR.is_dir() and (QWEN_DIR / "config.json").is_file()
                and (QWEN_DIR / "qwen3_5vl_0.8b-text-q41.gguf").is_file())
    got_sv = (ASR_MODEL.is_dir() and (ASR_MODEL / "model_quant_optimized.onnx").is_file())
    if got_qwen:
        mark(OK, "Qwen3.5-0.8B 已就位", "跳过下载")
    else:
        got_qwen = ensure_tar(qwen_tar, QWEN_URL, sha256=QWEN_SHA256, md5=QWEN_MD5,
                              offline=args.offline)
    if got_sv:
        mark(OK, "SenseVoice 已就位", "跳过下载")
    else:
        got_sv = ensure_tar(sv_tar, SENSEVOICE_URL, sha256=SENSEVOICE_SHA256,
                            offline=args.offline)

    if got_qwen and QWEN_DIR.is_dir() and (QWEN_DIR / "config.json").exists():
        mark(OK, "Qwen3.5-0.8B 已解压", str(QWEN_DIR))
    elif got_qwen:
        if DRY:
            mark(OK, "将解压 Qwen3.5-0.8B", str(CACHE_VLM))
        else:
            CACHE_VLM.mkdir(parents=True, exist_ok=True)
            code, _, err = run(["tar", "-xzf", str(qwen_tar), "-C", str(CACHE_VLM)], timeout=600)
            mark(OK if code == 0 else FAIL, "解压 Qwen3.5-0.8B", err.strip()[:300])

    if got_sv and ASR_MODEL.is_dir() and (ASR_MODEL / "model_quant_optimized.onnx").exists():
        mark(OK, "SenseVoice 已解压", str(ASR_MODEL))
    elif got_sv:
        if DRY:
            mark(OK, "将解压 SenseVoice", str(ASR_ROOT / "sensevoice"))
        else:
            dest = ASR_ROOT / "sensevoice"
            dest.mkdir(parents=True, exist_ok=True)
            code, _, err = run(["tar", "-xzf", str(sv_tar), "-C", str(dest)], timeout=600)
            mark(OK if code == 0 else FAIL, "解压 SenseVoice", err.strip()[:300])

    if not DRY:
        write_smt_config()
    return True


def step_apt(args):
    section("apt：系统依赖")
    if DRY:
        mark(OK, "将安装", " ".join(APT_PACKAGES))
        return True
    sudo(["apt-get", "update"], timeout=600)
    avail = []
    for pkg in APT_PACKAGES:
        code, _, _ = run(["apt-cache", "show", pkg], timeout=30, quiet=True)
        if code == 0:
            avail.append(pkg)
        else:
            mark(WARN, f"apt 源里没有 {pkg}", "跳过")
    if avail:
        code, _, err = sudo(["apt-get", "install", "-y"] + avail, timeout=1800)
        mark(OK if code == 0 else FAIL, "apt 安装", f"{len(avail)} 个包" if code == 0 else err.strip()[:300])
    return True


def step_pydeps(args):
    section("Python 依赖 / onnxruntime")
    code, out, _ = run(["python3", "-c", "import numpy, PIL; print('ok')"], timeout=60, quiet=True)
    mark(OK if code == 0 else FAIL, "numpy + Pillow", out.strip() or "缺失（apt python3-numpy/python3-pil）")
    code, out, _ = run(["python3", "-c", "import smbus2; print('ok')"], timeout=60, quiet=True)
    if code != 0 and not DRY:
        run(["pip3", "install", "--break-system-packages", "--user", "smbus2"], timeout=600)
        code, out, _ = run(["python3", "-c", "import smbus2; print('ok')"], timeout=60, quiet=True)
    mark(OK if code == 0 else WARN, "smbus2（旧 OLED 小屏 demo 用）",
         "已安装" if code == 0 else "未安装；当前主链路不需要")
    code, out, _ = run(["python3", "-c", "import onnxruntime; print(onnxruntime.__version__)"], timeout=60, quiet=True)
    if code == 0:
        mark(OK, "onnxruntime", out.strip())
        return True
    mark(WARN, "onnxruntime 缺失", "尝试 apt python3-spacemit-ort / pip 安装")
    if DRY:
        mark(OK, "将尝试安装", "python3-spacemit-ort 或 pip spacemit-ort")
        return True
    code, _, _ = sudo(["apt-get", "install", "-y", "python3-spacemit-ort"], timeout=900)
    code, out, _ = run(["python3", "-c", "import onnxruntime; print(onnxruntime.__version__)"], timeout=60, quiet=True)
    if code == 0:
        mark(OK, "onnxruntime", out.strip())
        return True
    code, out, err = run(["pip3", "install", "--break-system-packages", "--user",
                          "spacemit-ort==2.0.0+beta2", "onnxruntime==1.20.1+spacemit",
                          "--extra-index-url", "https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple"],
                         timeout=1200)
    code, out, _ = run(["python3", "-c", "import onnxruntime; print(onnxruntime.__version__)"], timeout=60, quiet=True)
    mark(OK if code == 0 else FAIL, "onnxruntime", out.strip() or err.strip()[:300])
    return True


def step_cues(args):
    section("audio：生成提示音")
    target = ROOM / "audio/start.wav"
    if target.exists():
        mark(OK, "start.wav 已存在", f"{target.stat().st_size} B")
        return True
    if DRY:
        mark(OK, "将运行", "python3 scripts/board/make_cues.py")
        return True
    code, out, err = run(["python3", "scripts/board/make_cues.py"], timeout=120, cwd=str(ROOM))
    mark(OK if code == 0 and target.exists() else FAIL, "make_cues.py",
         out.strip().splitlines()[-1] if out.strip() else err.strip()[:300])
    return True


def step_udev(args):
    section("udev：设备权限")
    if DRY:
        mark(OK, "将写", ", ".join(UDEV_RULES))
        return True
    try:
        for name, content in UDEV_RULES.items():
            write_etc_file(name, content)
        sudo(["udevadm", "control", "--reload-rules"])
        sudo(["udevadm", "trigger"])
        mark(OK, "udev 规则", "已写入并重载")
    except Exception as exc:  # noqa: BLE001
        mark(FAIL, "udev 规则", str(exc)[:300])
    return True


def step_groups(args):
    section("用户组：audio / i2c / gpio / video")
    if DRY:
        mark(OK, "将执行", f"usermod -aG audio,i2c,gpio,video {TARGET_USER}")
        return True
    for group in ("audio", "i2c", "gpio", "video"):
        code, _, _ = run(["getent", "group", group], timeout=15, quiet=True)
        if code == 0:
            sudo(["usermod", "-aG", group, TARGET_USER])
            mark(OK, f"加入 {group}", "重新登录后生效")
        else:
            mark(WARN, f"组 {group} 不存在", "跳过")
    return True


def step_audio(args):
    section("pipewire-pulse 内存上限")
    dropin = HOME / ".config/systemd/user/pipewire-pulse.service.d/room-review-memory.conf"
    write_user_file(dropin, PIPEWIRE_LIMIT)
    if DRY:
        mark(OK, "将重启", "systemctl --user daemon-reload && restart pipewire-pulse")
        return True
    run(["systemctl", "--user", "daemon-reload"], timeout=30)
    code, _, err = run(["systemctl", "--user", "restart", "pipewire-pulse"], timeout=60)
    mark(OK if code == 0 else WARN, "pipewire-pulse", "已应用 512M 上限" if code == 0 else err.strip()[:300])
    return True


def step_vnc(args):
    section("VNC 远程桌面")
    write_user_file(HOME / ".vnc/xstartup", XSTARTUP, mode=0o755)
    write_user_file(HOME / ".config/systemd/user/tigervnc.service", VNC_UNIT)
    write_user_file(HOME / "VNC-说明.txt", VNC_NOTE)
    # 虚拟显示上的两个密码框/锁屏坑：light-locker 会锁住没有密码的 biandu 会话，
    # colord 会弹 polkit 授权。两者都按用户/会话收窄处理。
    write_user_file(HOME / ".config/autostart/light-locker.desktop", LIGHT_LOCKER_AUTOSTART)
    try:
        write_root_file("/etc/polkit-1/rules.d/49-k1-vnc-color.rules", POLKIT_COLOR_RULE)
        mark(OK, "polkit 颜色规则", "biandu 免密处理 colord（仅 color-manager 动作）")
    except Exception as exc:  # noqa: BLE001
        mark(WARN, "polkit 颜色规则", str(exc)[:300])
    if DRY:
        mark(OK, "将启用", "loginctl enable-linger + systemctl --user enable/restart tigervnc")
        return True
    run(["loginctl", "enable-linger", TARGET_USER], timeout=30)
    run(["systemctl", "--user", "daemon-reload"], timeout=30)
    run(["systemctl", "--user", "enable", "tigervnc"], timeout=60)
    code, _, err = run(["systemctl", "--user", "restart", "tigervnc"], timeout=120)
    mark(OK if code == 0 else WARN, "tigervnc 服务", "已启动" if code == 0 else err.strip()[:300])
    return True


def desktop_dir():
    """XDG 桌面目录。中文镜像下是 ~/桌面，不是 ~/Desktop。"""
    code, out, _ = run(["xdg-user-dir", "DESKTOP"], timeout=15, quiet=True)
    out = out.strip()
    if code == 0 and out and Path(out) != HOME:
        return Path(out)
    return HOME / "Desktop"


def step_desktop(args):
    section("桌面快捷方式")
    launchers = [ROOM / "scripts/board/room_review_gui.sh",
                 ROOM / "scripts/board/voice_gimbal30.sh"]
    for launcher in launchers:
        if launcher.is_file() and not DRY:
            # qterminal -e 直接执行它；644 会 Permission denied。
            launcher.chmod(0o755)
            mark(OK, "启动脚本可执行", str(launcher))
    for name in ("RoomReview.desktop", "RoomReviewMenu.desktop"):
        src = ROOM / "scripts/board" / name
        if not src.is_file():
            mark(WARN, "缺 " + name, "跳过")
            continue
        targets = [desktop_dir() / name, HOME / ".local/share/applications" / name]
        for target in targets:
            if DRY:
                mark(OK, "将写", str(target))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            target.chmod(0o755)
            # XFCE 4.16+ 对桌面 .desktop 还有“可信”元数据；否则双击会报
            # “此功能需要有文件管理器服务存在”。失败不阻塞 bring-up。
            run(["gio", "set", "-t", "string", str(target), "metadata::trusted", "true"], timeout=15)
            checksum = hashlib.sha256(target.read_bytes()).hexdigest()
            run(["gio", "set", "-t", "string", str(target),
                 "metadata::xfce-exe-checksum", checksum], timeout=15)
            mark(OK, "桌面项", str(target))
    autostart_src = ROOM / "scripts/board/thunar-daemon.desktop"
    if autostart_src.is_file():
        target = HOME / ".config/autostart/thunar-daemon.desktop"
        if not DRY:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(autostart_src, target)
            target.chmod(0o644)
            mark(OK, "Thunar 服务自启", str(target))
        else:
            mark(OK, "将写", str(target))
    if not DRY:
        run(["update-desktop-database", str(HOME / ".local/share/applications")], timeout=30)
    return True


def step_verify(args):
    section("verify：板端自检")
    checks = collect_checks(full=True)
    counts = summarize(checks, "板端自检")
    if args.json:
        print(json.dumps({"checks": checks, "counts": counts}, ensure_ascii=False, indent=1))
    return counts[FAIL] == 0


STEPS = [
    ("preflight", step_preflight),
    ("dirs", step_dirs),
    ("payload", step_payload),
    ("runtime", step_runtime),
    ("models", step_models),
    ("apt", step_apt),
    ("pydeps", step_pydeps),
    ("cues", step_cues),
    ("udev", step_udev),
    ("groups", step_groups),
    ("audio", step_audio),
    ("vnc", step_vnc),
    ("desktop", step_desktop),
    ("verify", step_verify),
]


def selected_steps(args):
    names = [name for name, _ in STEPS]
    if args.only:
        wanted = [x.strip() for x in args.only.split(",") if x.strip()]
        unknown = [x for x in wanted if x not in names]
        if unknown:
            raise SystemExit(f"未知步骤：{', '.join(unknown)}；可用：{', '.join(names)}")
        names = [x for x in names if x in wanted]
    if args.skip:
        skipped = {x.strip() for x in args.skip.split(",") if x.strip()}
        names = [x for x in names if x not in skipped]
    return [(name, dict(STEPS)[name]) for name in names]


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def cmd_plan(args):
    info = identity()
    print("=== 新板恢复计划（只读，不改任何东西） ===")
    if sys.platform != "linux":
        print("  注意：当前不是 Linux/板端，下面目录是按当前 HOME 推算的（板端应为 /home/biandu/rv）。")
    for key, value in info.items():
        print(f"  {key:10}: {value}")
    print("\n将要恢复的内容：")
    for name, _ in STEPS:
        print(f"  - {name}")
    print(f"\n目标目录：{BOARD}")
    print(f"模型下载源：\n  {QWEN_URL}\n  {SENSEVOICE_URL}")
    print("\n下一步：")
    print("  1) PC: python scripts/pc/make_offline_bundle.py --zip")
    print("  2) 把 dist/*.zip 传到新板 ~/，解压后 sudo cp -a payload/. /")
    print(f"  3) 板端: python3 scripts/board/new_board_bringup.py install --bundle <解压目录>")
    return 0


def cmd_install(args):
    global DRY
    DRY = args.dry_run
    if DRY:
        print("*** dry-run：只打印将执行的动作，不做任何修改 ***")
    if not args.dry_run and sys.platform == "linux":
        # 用 sudo -n 检查“非交互 sudo”是否可用；sudo -v 在 NOPASSWD 下仍可能要求终端。
        code, _, _ = run(["sudo", "-n", "true"], timeout=30)
        if code != 0:
            print("sudo 非交互不可用；请先确认 biandu 有 NOPASSWD sudo，或在终端执行 sudo -v",
                  file=sys.stderr)
            return 2
    steps = selected_steps(args)
    print(f"=== install：{len(steps)} 个步骤 ===")
    ok = True
    for name, func in steps:
        try:
            result = func(args)
        except Exception as exc:  # noqa: BLE001  一步失败不拖垮整轮
            mark(FAIL, name, f"{type(exc).__name__}: {exc}")
            result = False
        ok = ok and result is not False
        if name == "preflight" and result is False:
            print("preflight 未通过，停止后续步骤", file=sys.stderr)
            break
    counts = {status: sum(1 for r in RESULTS if r["status"] == status)
              for status in (OK, WARN, FAIL, SKIP)}
    print(f"\n=== install 汇总 ===\n  OK {counts[OK]}  WARN {counts[WARN]}  FAIL {counts[FAIL]}")
    if args.json:
        print(json.dumps({"results": RESULTS, "counts": counts}, ensure_ascii=False, indent=1))
    print("\n下一步：")
    print("  - 板端: python3 scripts/board/new_board_bringup.py verify")
    print("  - PC:   python scripts/pc/accept_new_board.py --host <板子IP> --smoke")
    print("  - 首次拍照前先确认音箱/USB 摄像头已枚举；VNC 见 ~/VNC-说明.txt")
    return 0 if ok and counts[FAIL] == 0 else 1


def cmd_verify(args):
    checks = collect_checks(full=not args.quick)
    counts = summarize(checks, "新板验收自检（板端）")
    for check in checks:
        if check["status"] in (FAIL, WARN):
            print(f"  {check['status']:4} {check['name']}：{check['detail']}")
    if args.json:
        print(json.dumps({"checks": checks, "counts": counts}, ensure_ascii=False, indent=1))
    return 0 if counts[FAIL] == 0 else 1


def cmd_inventory(args):
    info = identity()
    checks = collect_checks(full=True)
    payload = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "identity": info,
        "checks": checks,
        "files": {str(p): ({"exists": True, "bytes": p.stat().st_size} if p.is_file()
                           else {"exists": p.exists()}) for p in REQUIRED_FILES},
        "expected_models": {
            "qwen": {"path": str(QWEN_DIR), "url": QWEN_URL, "sha256": QWEN_SHA256},
            "sensevoice": {"path": str(ASR_MODEL), "url": SENSEVOICE_URL, "sha256": SENSEVOICE_SHA256},
        },
    }
    out_dir = BOARD / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"newboard_inventory_{time.strftime('%Y%m%d_%H%M%S')}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"盘点已写入 {target}")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="K1 新板一键恢复（Bianbu / biandu）")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="只读：显示环境与恢复计划")
    plan.set_defaults(func=cmd_plan)

    install = sub.add_parser("install", help="执行恢复（幂等，可重复跑）")
    install.add_argument("--bundle", help="离线包解压目录（含 payload/ 与 manifest.json）")
    install.add_argument("--models-dir", help="模型 tar.gz 所在目录（默认 ~/rv/offline/models）")
    install.add_argument("--offline", action="store_true", help="缺模型时直接失败，不联网")
    install.add_argument("--dry-run", action="store_true", help="只打印将执行的动作")
    install.add_argument("--only", help="只跑指定步骤，逗号分隔")
    install.add_argument("--skip", help="跳过指定步骤，逗号分隔")
    install.add_argument("--skip-sha", action="store_true", help="跳过离线包逐文件 sha256 校验")
    install.add_argument("--allow-other-home", action="store_true", help="允许 HOME 不是 /home/biandu")
    install.add_argument("--json", action="store_true", help="结束时输出 JSON 摘要")
    install.set_defaults(func=cmd_install)

    verify = sub.add_parser("verify", help="板端验收自检（只读）")
    verify.add_argument("--quick", action="store_true", help="只查身份/文件/设备，不跑导入与自检")
    verify.add_argument("--json", action="store_true", help="输出 JSON")
    verify.set_defaults(func=cmd_verify)

    inventory = sub.add_parser("inventory", help="写板端盘点 JSON 到 ~/rv/results/")
    inventory.add_argument("--json", action="store_true", help="同时打印 JSON")
    inventory.set_defaults(func=cmd_inventory)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
