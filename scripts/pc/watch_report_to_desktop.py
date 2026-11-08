#!/usr/bin/env python3
"""把 K1 板端最新生成的买家版 report.html 自动复制到 Windows 桌面。

板端入口在分析结束后把 HTML 写到：
    /home/biandu/rv/room_review/rounds/round_*/report.html

本脚本在 PC 上轮询这些文件，发现新文件/新版本就经 SSH/SFTP 拉到桌面：
    K1-室内风险初评-最新.html
    K1-室内风险初评-YYYYMMDD-HHMMSS.html

默认每 3 秒检查一次；断线自动重连。用 pythonw 启动时无窗口，日志在
%LOCALAPPDATA%\\K1ReportWatcher\\watcher.log。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime

import paramiko

USER = "biandu"
KEY = Path.home() / ".ssh" / "id_k1_agent"
REMOTE_ROOT = "/home/biandu/rv/room_review/rounds"
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "K1ReportWatcher"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "watcher.log"
LOCK_FILE = STATE_DIR / "watcher.lock"
DEFAULT_HOSTS = ("172.20.10.2", "k1.local")
LATEST_NAME = "K1-室内风险初评-最新.html"


def desktop_dir() -> Path:
    if os.name == "nt":
        try:
            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_DESKTOPDIRECTORY = 0x0010
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0:
                return Path(buf.value)
        except Exception:
            pass
    return Path.home() / "Desktop"


def log(message: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
        except Exception:
            pass
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock():
    """保证开机自启 + 手动启动不会同时跑两个监听。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            return fd
        except FileExistsError:
            try:
                pid = int(LOCK_FILE.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                pid = 0
            if _pid_alive(pid):
                return None
            try:
                LOCK_FILE.unlink()
            except OSError:
                return None
    return None


def release_lock(fd) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def connect(hosts):
    last = None
    for host in hosts:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(hostname=host, username=USER, key_filename=str(KEY),
                           timeout=8, banner_timeout=15, auth_timeout=15,
                           look_for_keys=False, allow_agent=False)
            log("已连接 %s" % host)
            return client, host
        except Exception as exc:  # noqa: BLE001 - 网络错误都要重试
            last = exc
            try:
                client.close()
            except Exception:
                pass
    raise RuntimeError("连接板子失败：%s" % last)


def latest_report(sftp):
    newest = None
    for entry in sftp.listdir_attr(REMOTE_ROOT):
        if not entry.filename.startswith("round_"):
            continue
        remote = "%s/%s/report.html" % (REMOTE_ROOT, entry.filename)
        try:
            st = sftp.stat(remote)
        except OSError:
            continue
        if st.st_size < 50_000:  # 正常自包含报告约 600–900KB，避免拿到半截文件
            continue
        item = (st.st_mtime, remote, st.st_size)
        if newest is None or item[0] > newest[0]:
            newest = item
    return newest


def stamp_for(remote: str, mtime: float) -> str:
    m = re.search(r"round_(\d{8})_(\d{6})_", remote)
    if m:
        return "%s-%s" % (m.group(1), m.group(2))
    return datetime.fromtimestamp(mtime).strftime("%Y%m%d-%H%M%S")


def download(sftp, remote: str, size: int, dest_latest: Path, dest_stamped: Path) -> None:
    dest_latest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_latest.with_name(dest_latest.name + ".part")
    sftp.get(remote, str(tmp))
    local_size = tmp.stat().st_size
    if local_size != size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("下载大小不一致：远端 %d，本地 %d" % (size, local_size))
    os.replace(tmp, dest_stamped)
    # 最新一份固定文件名也更新（浏览器收藏/桌面固定入口）
    try:
        if dest_latest.exists():
            dest_latest.unlink()
        os.link(dest_stamped, dest_latest)
    except OSError:
        # 文件系统不支持硬链接时退回复制
        import shutil
        shutil.copyfile(dest_stamped, dest_latest)


def check_once(sftp, state: dict, desktop: Path) -> bool:
    item = latest_report(sftp)
    if item is None:
        return False
    mtime, remote, size = item
    if (state.get("remote") == remote and
            state.get("mtime") == mtime and state.get("size") == size and
            (desktop / LATEST_NAME).exists()):
        return False
    # 等 0.8 秒再 stat 一次，避免读到正在写的文件
    time.sleep(0.8)
    again = sftp.stat(remote)
    if again.st_size != size or again.st_mtime != mtime:
        return False
    stamp = stamp_for(remote, mtime)
    dest_latest = desktop / LATEST_NAME
    dest_stamped = desktop / ("K1-室内风险初评-%s.html" % stamp)
    download(sftp, remote, size, dest_latest, dest_stamped)
    state.update(remote=remote, mtime=mtime, size=size, stamp=stamp,
                 latest=str(dest_latest), updated=datetime.now().isoformat(timespec="seconds"))
    save_state(state)
    log("已复制：%s（%d 字节）" % (dest_latest, size))
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", action="append", default=[],
                        help="可重复；默认先 172.20.10.2，再 k1.local")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    hosts = tuple(args.host) if args.host else DEFAULT_HOSTS
    desktop = desktop_dir()
    desktop.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    if not args.once:
        lock_fd = acquire_lock()
        if lock_fd is None:
            log("已有报告同步实例在运行，本实例退出")
            return 0
    state = load_state()
    log("监视 %s，目标桌面 %s" % (", ".join(hosts), desktop))
    client = None
    sftp = None
    try:
        while True:
            try:
                if client is None:
                    client, host = connect(hosts)
                    sftp = client.open_sftp()
                changed = check_once(sftp, state, desktop)
                if args.once:
                    return 0 if changed or state.get("remote") else 2
            except Exception as exc:  # noqa: BLE001
                log("本轮失败：%s" % exc)
                try:
                    if sftp:
                        sftp.close()
                except Exception:
                    pass
                try:
                    if client:
                        client.close()
                except Exception:
                    pass
                sftp = None
                client = None
                if args.once:
                    return 1
            time.sleep(max(1.0, args.interval))
    finally:
        try:
            if sftp:
                sftp.close()
        except Exception:
            pass
        try:
            if client:
                client.close()
        except Exception:
            pass
        release_lock(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
