#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""k1.py —— 从 Windows 直接操作 K1（免密、免 Xshell）。

背景：Windows 自带的 OpenSSH 客户端在"私钥带口令"时会拿到 PK_OK 后直接掐连接，
报 Permission denied 而服务端日志里查不到原因。所以这里用 paramiko 走同一把
**无口令**钥匙（~/.ssh/id_k1_agent），稳定且能拿到明确报错。

用法：
    python scripts/pc/k1.py run "ls -l ~/rv"
    python scripts/pc/k1.py run "bash ~/rv/30_oled_check.sh"
    python scripts/pc/k1.py put app/display.py ~/rv/app/display.py
    python scripts/pc/k1.py putdir app ~/rv/app
    python scripts/pc/k1.py get ~/rv/results/llm_display.jsonl .\\results\\
    python scripts/pc/k1.py shell                     # 进交互式（简单 REPL）

约定：
    * 远端命令用 login shell 执行（bash -lc），所以 ~/hwenv 之类的环境能生效
    * 退出码原样返回，输出原样打印（stdout 到 stdout，stderr 到 stderr）
"""
from __future__ import annotations

import argparse
import os
import posixpath
import stat
import sys

DEFAULT_HOST = os.environ.get("K1_HOST", "172.20.10.2")  # 2026-11-08 新板；DHCP 会变，可用 --host 覆盖
DEFAULT_USER = os.environ.get("K1_USER", "biandu")
DEFAULT_KEY = os.path.expanduser(os.environ.get("K1_KEY", "~/.ssh/id_k1_agent"))


def connect(args):
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        args.host, port=args.port, username=args.user, key_filename=args.key,
        look_for_keys=False, allow_agent=False,
        timeout=8, banner_timeout=8, auth_timeout=8,
    )
    return client


def run(client, command, quiet=False):
    """执行一条远端命令，返回 (exit_code, stdout, stderr)。用 bash -lc 保证环境一致。"""
    wrapped = "bash -lc %s" % shq(command)
    _, out, err = client.exec_command(wrapped, timeout=None, get_pty=False)
    stdout = out.read().decode("utf-8", "replace")
    stderr = err.read().decode("utf-8", "replace")
    code = out.channel.recv_exit_status()
    if not quiet:
        sys.stdout.write(stdout)
        if stderr:
            sys.stderr.write(stderr)
    return code, stdout, stderr


def shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _expand(sftp, remote):
    """把远端路径规范化。

    坑：PowerShell 7 会在把参数交给 exe *之前* 把 `~/x` 展开成本地路径
    （`~/rv` -> `C:\\Users\\<me>/rv`），所以命令行里写 `~` 反而危险。
    这里做两件事：展开真正的 `~/`，并把误传进来的 Windows 盘符路径直接拒掉。
    """
    if len(remote) > 1 and remote[1] == ":" and remote[0].isalpha():
        raise SystemExit(
            "[k1] 远端路径被 PowerShell 展开成了本地路径：%r\n"
            "     请改用绝对路径，例如 /home/biandu/rv/app" % remote
        )
    if remote == "~":
        return sftp.normalize(".")
    if remote.startswith("~/"):
        return sftp.normalize(".") + remote[1:]
    return remote


def put(client, local, remote, quiet=False):
    sftp = client.open_sftp()
    try:
        remote = _expand(sftp, remote)
        _mkdirs(sftp, posixpath.dirname(remote) or "/")
        sftp.put(local, remote)
        if not quiet:
            print("[put] %s -> %s (%d B)" % (local, remote, os.path.getsize(local)))
    finally:
        sftp.close()


def _mkdirs(sftp, remote_dir):
    parts, cur = [], ""
    for piece in remote_dir.strip("/").split("/"):
        cur = cur + "/" + piece if cur else "/" + piece
        try:
            sftp.stat(cur)
        except IOError:
            try:
                sftp.mkdir(cur)
            except IOError:
                pass


def putdir(client, local_dir, remote_dir, quiet=False):
    sftp = client.open_sftp()
    n = 0
    try:
        remote_dir = _expand(sftp, remote_dir)
        for root, dirs, files in os.walk(local_dir):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
            rel = os.path.relpath(root, local_dir).replace("\\", "/")
            target = remote_dir if rel == "." else posixpath.join(remote_dir, rel)
            _mkdirs(sftp, target)
            for f in files:
                if f.endswith((".pyc", ".pyo")):
                    continue
                sftp.put(os.path.join(root, f), posixpath.join(target, f))
                n += 1
        if not quiet:
            print("[putdir] %s -> %s (%d files)" % (local_dir, remote_dir, n))
    finally:
        sftp.close()


def get(client, remote, local):
    sftp = client.open_sftp()
    try:
        os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
        sftp.get(remote, local)
        print("[get] %s -> %s" % (remote, local))
    finally:
        sftp.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="K1 远程操作（paramiko + 无口令钥匙）")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--key", default=DEFAULT_KEY)
    sub = ap.add_subparsers(dest="action", required=True)

    p_run = sub.add_parser("run", help="执行一条命令")
    p_run.add_argument("command")

    p_put = sub.add_parser("put", help="上传单个文件")
    p_put.add_argument("local")
    p_put.add_argument("remote")

    p_dir = sub.add_parser("putdir", help="上传整个目录")
    p_dir.add_argument("local")
    p_dir.add_argument("remote")

    p_get = sub.add_parser("get", help="下载单个文件")
    p_get.add_argument("remote")
    p_get.add_argument("local")

    p_scr = sub.add_parser("script", help="上传本地 .sh 并执行（避免多层引号）")
    p_scr.add_argument("local")
    p_scr.add_argument("--keep", action="store_true", help="执行后保留远端脚本")

    sub.add_parser("shell", help="交互式（逐条执行）")

    args = ap.parse_args(argv)
    client = connect(args)
    try:
        if args.action == "run":
            code, _, _ = run(client, args.command)
            return code
        if args.action == "put":
            put(client, args.local, args.remote)
            return 0
        if args.action == "putdir":
            putdir(client, args.local, args.remote)
            return 0
        if args.action == "get":
            get(client, args.remote, args.local)
            return 0
        if args.action == "script":
            name = os.path.basename(args.local)
            remote = "/tmp/" + name
            put(client, args.local, remote, quiet=True)
            print("[script] %s" % args.local)
            code, _, _ = run(client, "bash %s" % remote)
            if not args.keep:
                run(client, "rm -f %s" % remote, quiet=True)
            return code
        if args.action == "shell":
            while True:
                try:
                    line = input("k1> ")
                except (EOFError, KeyboardInterrupt):
                    break
                if line.strip() in ("exit", "quit"):
                    break
                run(client, line)
            return 0
        return 2
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
