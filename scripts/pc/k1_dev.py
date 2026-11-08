"""PC → K1 热迭代：改完 gimbal/ 的代码，保存后自动推送并在板上执行。

用法（在仓库根目录，任意 Python 环境都行）：
    # 一次性：推送改动 + 在板上跑一条命令，然后退出
    python scripts/pc/k1_dev.py run --host 172.20.10.2 --cmd "python3 -m gimbal pose --pan 90 --tilt -30 --hold 1"

    # 常驻：每次保存 gimbal/ 下的文件就推送并执行（Ctrl-C 结束）
    python scripts/pc/k1_dev.py watch --host 172.20.10.2 --cmd "python3 -m gimbal pose --pan 90 --tilt -30 --hold 1"

    # 只推送、不执行
    python scripts/pc/k1_dev.py watch --host 172.20.10.2

传输：优先用 paramiko（scripts/pc/k1.py）；venv 里没装 paramiko 时自动改用
Windows 自带 OpenSSH（ssh/scp + ~/.ssh/id_k1_agent），不需要安装任何包。

推送内容：
    gimbal/*.py, gimbal/config.json          → /home/biandu/rv/gimbal/
    app/gimbal_scan.py                       → /home/biandu/rv/room_review/app/
命令默认在 /home/biandu/rv 下执行（所以 `python3 -m gimbal ...` 直接可用）。
"""
from __future__ import annotations

import argparse
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import k1  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
GIMBAL_LOCAL = ROOT / "gimbal"
GIMBAL_REMOTE = "/home/biandu/rv/gimbal"
APP_LOCAL = ROOT / "app/gimbal_scan.py"
APP_REMOTE = "/home/biandu/rv/room_review/app/gimbal_scan.py"
WORKDIR = "/home/biandu/rv"


def tracked_files():
    files = [p for p in sorted(GIMBAL_LOCAL.iterdir())
             if p.is_file() and p.suffix in (".py", ".json", ".md")]
    if APP_LOCAL.is_file():
        files.append(APP_LOCAL)
    return files


def remote_of(path):
    return APP_REMOTE if path == APP_LOCAL else posixpath.join(GIMBAL_REMOTE, path.name)


def snapshot():
    return {p: p.stat().st_mtime_ns for p in tracked_files()}


# ---------------------------------------------------------------- 传输后端
class ParamikoTransport:
    """有 paramiko 时走它（k1.py 同一套连接参数）。"""

    def __init__(self, args):
        self.client = k1.connect(argparse.Namespace(host=args.host, user=k1.DEFAULT_USER,
                                                   key=k1.DEFAULT_KEY, port=22))
        self.sftp = self.client.open_sftp()
        for remote in (GIMBAL_REMOTE, posixpath.dirname(APP_REMOTE)):
            self._mkdirs(remote)

    def _mkdirs(self, remote_dir):
        parts, cur = [], ""
        for piece in remote_dir.strip("/").split("/"):
            cur = cur + "/" + piece
            try:
                self.sftp.stat(cur)
            except IOError:
                try:
                    self.sftp.mkdir(cur)
                except IOError:
                    pass

    def push(self, paths):
        pushed = 0
        for path in paths:
            remote = remote_of(path)
            try:
                same = int(self.sftp.stat(remote).st_mtime) >= int(path.stat().st_mtime)
            except IOError:
                same = False
            if same:
                continue
            data = path.read_bytes().replace(b"\r\n", b"\n")
            with self.sftp.open(remote, "wb") as stream:
                stream.write(data)
            pushed += 1
            print("  push %-28s → %s (%d B)" % (path.name, remote, len(data)), flush=True)
        return pushed

    def run(self, command):
        print("\n$ %s" % command, flush=True)
        code, _, _ = k1.run(self.client, "cd %s && %s" % (WORKDIR, command))
        print("[exit %d]" % code, flush=True)
        return code

    def close(self):
        try:
            self.sftp.close()
        finally:
            self.client.close()


class OpenSshTransport:
    """没有 paramiko 时走 Windows 自带 ssh/scp；不需要额外安装。"""

    def __init__(self, args):
        key = os.path.expanduser(k1.DEFAULT_KEY)
        if not Path(key).is_file():
            raise SystemExit("找不到 SSH 私钥：%s" % key)
        self.ssh_base = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                         "-i", key, "%s@%s" % (k1.DEFAULT_USER, args.host)]
        self.scp_base = ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                         "-i", key]
        self.host = "%s@%s" % (k1.DEFAULT_USER, args.host)
        check = subprocess.run(self.ssh_base + ["true"], capture_output=True, text=True)
        if check.returncode != 0:
            raise SystemExit("SSH 连接失败：%s" % (check.stderr.strip() or check.returncode))

    def push(self, paths):
        if not paths:
            return 0
        subprocess.run(self.ssh_base + ["mkdir -p %s %s" % (GIMBAL_REMOTE,
                                                             posixpath.dirname(APP_REMOTE))],
                       check=True, capture_output=True)
        # 按远端目录分组，每组一次 scp；写临时文件是为了统一 LF。
        groups = {}
        for path in paths:
            groups.setdefault(posixpath.dirname(remote_of(path)), []).append(path)
        tempdir = Path(tempfile.mkdtemp(prefix="k1dev_"))
        pushed = 0
        try:
            for remote_dir, group in groups.items():
                staged = []
                for path in group:
                    temp = tempdir / path.name
                    temp.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
                    staged.append(str(temp))
                    print("  push %-28s → %s" % (path.name, remote_of(path)), flush=True)
                subprocess.run(self.scp_base + staged + ["%s:%s/" % (self.host, remote_dir)],
                               check=True, capture_output=True)
                pushed += len(group)
        finally:
            shutil.rmtree(tempdir, ignore_errors=True)
        return pushed

    def run(self, command):
        print("\n$ %s" % command, flush=True)
        code = subprocess.run(self.ssh_base + ["cd %s && %s" % (WORKDIR, command)]).returncode
        print("[exit %d]" % code, flush=True)
        return code

    def close(self):
        pass


def connect(args):
    try:
        import paramiko  # noqa: F401
    except ImportError:
        print("（当前环境没有 paramiko，改用系统 OpenSSH 传输）", flush=True)
        return OpenSshTransport(args)
    return ParamikoTransport(args)


# ---------------------------------------------------------------- 主流程
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="172.20.10.2")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "watch"):
        child = sub.add_parser(name)
        child.add_argument("--host", default=argparse.SUPPRESS)
        child.add_argument("--cmd", default=None,
                           help="推送后在板上执行的命令，例如 python3 -m gimbal pose --pan 90 --tilt -30 --hold 1")
        child.add_argument("--interval", type=float, default=0.7, help="watch 轮询间隔（秒）")
    args = parser.parse_args(argv)

    transport = connect(args)
    try:
        if args.command == "run":
            print("推送改动…", flush=True)
            transport.push(tracked_files())
            if args.cmd:
                return transport.run(args.cmd)
            return 0

        print("监视 %s（Ctrl-C 结束）" % GIMBAL_LOCAL, flush=True)
        print("命令：%s" % (args.cmd or "（只推送，不执行）"), flush=True)
        state = snapshot()
        transport.push(list(state))
        print("等待下一次保存…", flush=True)
        while True:
            time.sleep(args.interval)
            now = snapshot()
            changed = [p for p, stamp in now.items() if state.get(p) != stamp]
            if not changed:
                continue
            state = now
            print("\n[%s] 检测到 %d 个文件变化" % (time.strftime("%H:%M:%S"), len(changed)), flush=True)
            transport.push([p for p in changed if p.exists()])
            if not args.cmd:
                print("等待下一次保存…", flush=True)
                continue
            # 编辑器一次保存可能写多个文件/多次：等 0.35s 内不再变化才执行，只跑最后一次。
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                time.sleep(0.35)
                newer = snapshot()
                diff = [p for p in newer if newer.get(p) != state.get(p)]
                if not diff:
                    break
                state = newer
                transport.push([p for p in diff if p.exists()])
            transport.run(args.cmd)
            print("等待下一次保存…", flush=True)
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)
        return 0
    finally:
        transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
