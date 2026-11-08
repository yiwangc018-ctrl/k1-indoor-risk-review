#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PC 侧新板离线恢复包生成器。

把 PC 仓库里“板端需要且已经能取得”的文件，按板端绝对路径镜像收集到
``<out>/payload/`` 下，并生成 ``manifest.json`` / ``SHA256SUMS`` /
``README-恢复说明.txt``；同时把 PC 上没有、必须从官方源下载的大模型写进
manifest 的 ``missing`` 字段，避免新板恢复时才发现缺件。

用法::

    python scripts/pc/make_offline_bundle.py [--out DIR] [--zip] [--check-only]
                                            [--json] [--root DIR] [--models-dir DIR]

``payload/`` 下的布局严格等于“板端绝对路径去掉开头的 /”，因此板端可以直接
``cd <包目录> && sudo cp -a payload/. /`` 覆盖恢复。

退出码：0 正常（missing 大模型不算失败，新板可联网下载）；2 参数 / 根目录错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

#: 脚本所在仓库根（scripts/pc/make_offline_bundle.py → 上两级）。仅作默认值。
DEFAULT_ROOT = Path(__file__).resolve().parents[2]

VERSION = 1
CHUNK_SIZE = 1024 * 1024  # 1MB 分块读写，避免把大模型整块读进内存
DEFAULT_DATE_FORMAT = "%Y%m%d"

# 板端路径常量（全部是板端绝对路径，不要加 Windows 盘符）。
BOARD_HOME = "/home/biandu"
BOARD_RV = BOARD_HOME + "/rv"
BOARD_REVIEW = BOARD_RV + "/room_review"
BOARD_SCRIPTS = BOARD_REVIEW + "/scripts"
BOARD_VALIDATION = BOARD_RV + "/validation_20261103"

# 扫描时排除的目录 / 后缀；不跟随符号链接。
EXCLUDED_DIRS = {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".venv", "venv"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}

# scripts/board 下需要随包的脚本/源码类型。
# 说明：需求里列的扩展名是 .sh/.py/.cpp/.h/.desktop；额外收录 .c，因为板端
# build_room_asr_rvv.sh 会编译 test_room_asr_rvv.c，缺了它新板无法重建 ASR
# 测试程序。只多不少，不影响其他内容。
BOARD_SUFFIXES = {".sh", ".py", ".cpp", ".h", ".c", ".desktop"}
GIMBAL_SUFFIXES = {".py", ".json", ".md"}

#: 必须从官方源下载的大模型。archive 为 <models-dir> 里的文件名，存在则随包。
MODEL_DEFS = [
    {
        "archive": "Qwen3.5-0.8B.tar.gz",
        "bundle_target": BOARD_RV + "/offline/models/Qwen3.5-0.8B.tar.gz",
        "target_dir": "/home/biandu/.cache/models/vlm/Qwen3.5-0.8B",
        "extract_dir": "/home/biandu/.cache/models/vlm",
        "model_dir": "/home/biandu/.cache/models/vlm/Qwen3.5-0.8B",
        "url": "http://archive.spacemit.com/spacemit-ai/model_zoo/vlm/Qwen3.5-0.8B.tar.gz",
        "bytes_estimate": 977343463,
        "why": "PC 侧尚无该模型包（约 977MB；官方源同目录提供 .md5 校验文件）",
        "optional": False,
    },
    {
        "archive": "sensevoice.tar.gz",
        "bundle_target": BOARD_RV + "/offline/models/sensevoice.tar.gz",
        "target_dir": BOARD_RV + "/asr/sensevoice",
        # 按 new_board_bringup.py 的约定：解压到 ~/rv/asr/sensevoice/，里面再套一层 sensevoice/
        "extract_dir": BOARD_RV + "/asr/sensevoice",
        "model_dir": BOARD_RV + "/asr/sensevoice/sensevoice",
        "url": "https://archive.spacemit.com/spacemit-ai/openwebui/sensevoice.tar.gz",
        "bytes_estimate": 446692718,
        "why": "PC 侧尚无该模型包（约 426MB；SenseVoice 语音识别模型）",
        "optional": False,
    },

]

#: 从 PC 仓库 results 里取的固定文件（相对仓库根）。
RUNTIME_ARCHIVE_FILES = ("llama_017.tar.gz", "ort_206.tar.gz")


#: 新板首次启动脚本；每次运行都重新扫描，不硬编码“必须存在”。
BRINGUP_SCRIPT = "60_new_board_bringup.sh"

README_TEXT = """\
新板离线恢复包（K1 MUSE Pi Pro / Bianbu / 用户 biandu）
============================================================
一、包内容
  payload/        按板端绝对路径镜像（payload/home/biandu/...），整树拷到 /
  manifest.json   文件清单（target → payload、字节数、sha256、kind、required）
  SHA256SUMS      payload/ 下文件的校验和（路径相对包根，按路径排序，LF）
  README-恢复说明.txt  本文件
二、校验与拷贝（在包根目录执行）
  1) 校验：cd <包目录> && sha256sum -c SHA256SUMS
  2) 拷入系统：sudo cp -a payload/. /
     cp -a 会保留时间戳与权限，也会带上解包用户的属主；若属主不是 biandu：
       sudo chown -R biandu:biandu /home/biandu/rv /home/biandu/.cache
  3) 可执行位（若丢失）：
       sudo chmod +x /home/biandu/rv/room_review/scripts/board/*.sh
       sudo chmod +x /home/biandu/rv/room_review/scripts/board/*.desktop
  4) 若脚本报 $'\\r' 错误：sudo sed -i 's/\\r$//' <脚本文件>
三、大模型（本包已带两个 tar.gz；缺失时按官方源补）
  包内位置：payload/home/biandu/rv/offline/models/（不解压，install 时校验并解压）
  Qwen3.5-0.8B：http://archive.spacemit.com/spacemit-ai/model_zoo/vlm/Qwen3.5-0.8B.tar.gz
    977,343,463 B；sha256 be44a9d2…994d2ca；同目录另有 .md5
    解压到 /home/biandu/.cache/models/vlm/ → ~/.cache/models/vlm/Qwen3.5-0.8B/
  SenseVoice：https://archive.spacemit.com/spacemit-ai/openwebui/sensevoice.tar.gz
    446,692,718 B；sha256 3e54c9cb…5b6c73ff35
    解压到 /home/biandu/rv/asr/sensevoice/ → ~/rv/asr/sensevoice/sensevoice/
四、新板首次启动
  1) 先把本包拷到板端并解压，例如 /home/biandu/rv/offline/bundle
  2) 按“二”把 payload 拷到 /（这时脚本才在最终路径）
  3) 一键恢复（脚本自己调 sudo；不要用 sudo 运行，否则 HOME 变成 /root 路径全错）：
     bash /home/biandu/rv/room_review/scripts/board/60_new_board_bringup.sh install --bundle /home/biandu/rv/offline/bundle --skip payload
     若没有先拷 payload，去掉 --skip payload（脚本会自己拷贝一次）
  4) 验收：python3 /home/biandu/rv/room_review/scripts/board/new_board_bringup.py verify
  5) 启动主程序：bash /home/biandu/rv/room_review/scripts/board/start_room_review.sh
五、说明
  只收录 PC 侧已有文件；manifest 的 missing 字段列出必须联网下载的大模型。
  不收录 .git、__pycache__、*.pyc，也不跟随符号链接；包内文件与源文件逐字节一致。
"""


class BundleError(Exception):
    """参数 / 根目录 / 输出目录等“用户可修正”的错误（退出码 2）。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _human_bytes(size: int) -> str:
    """把字节数转成便于阅读的字符串（KiB/MiB/GiB）。"""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{int(size)} B"


def _relative_label(path: Path, root: Path) -> str:
    """仓库内文件返回相对路径，仓库外（模型缓存）返回绝对路径。"""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _iter_files(base: Path, suffixes=None):
    """确定性遍历 base 下的普通文件。

    - 不跟随符号链接（os.walk followlinks=False，且跳过链接文件/目录）；
    - 跳过 EXCLUDED_DIRS 与 EXCLUDED_SUFFIXES；
    - suffixes 为小写扩展名集合，None 表示不过滤。
    """
    base = Path(base)
    if not base.is_dir():
        return []
    found = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in EXCLUDED_DIRS
            and not os.path.islink(os.path.join(dirpath, name))
        )
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if full.is_symlink():
                continue
            if full.suffix.lower() in EXCLUDED_SUFFIXES:
                continue
            if suffixes is not None and full.suffix.lower() not in suffixes:
                continue
            found.append(full)
    found.sort(key=lambda item: item.relative_to(base).as_posix())
    return found


def _hash_file(path: Path):
    """分块 sha256，返回 (字节数, 十六进制摘要)。"""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while True:
            block = stream.read(CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


def _copy_and_hash(source: Path, dest: Path):
    """复制文件并同时计算 sha256（一次读源文件，避免大模型读两遍）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with open(source, "rb") as src, open(dest, "wb") as dst:
        while True:
            block = src.read(CHUNK_SIZE)
            if not block:
                break
            dst.write(block)
            digest.update(block)
            size += len(block)
    try:
        os.chmod(dest, _payload_mode(dest))
    except OSError:
        pass  # Windows 上 chmod 只读位，失败不影响打包
    return size, digest.hexdigest()


def _payload_mode(path: Path) -> int:
    """板端需要的权限位：脚本/桌面项可执行，其余 0644。"""
    if path.suffix.lower() in {".sh", ".desktop"}:
        return 0o755
    return 0o644


def _write_text(path: Path, text: str) -> None:
    """UTF-8 + LF 写文本（Windows 上也不产生 CRLF）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def _write_checksums(path: Path, entries) -> None:
    """SHA256SUMS：``<sha256>  <payload 相对路径>``，按路径排序，LF 换行。"""
    lines = [
        f"{entry['sha256']}  {entry['payload']}"
        for entry in sorted(entries, key=lambda item: item["payload"])
    ]
    _write_text(path, "".join(line + "\n" for line in lines))


# ---------------------------------------------------------------------------
# 扫描：仓库 / 缓存里有哪些文件要进包
# ---------------------------------------------------------------------------

def _add_entry(planned, warnings, root, seen, source, target, kind, required=True, extra=None):
    """登记一个待收录文件；目标路径重复时只保留第一条。"""
    source = Path(source)
    if target in seen:
        return
    if source.is_symlink():
        warnings.append(f"跳过符号链接：{_relative_label(source, root)}")
        return
    if not source.is_file():
        return
    item = {
        "source": source,
        "target": target,
        "kind": kind,
        "required": bool(required),
    }
    if extra:
        item.update(extra)
    planned.append(item)
    seen[target] = item


def collect(root: Path, models_dir: Path):
    """扫描仓库与模型缓存，返回 (planned, missing, warnings)。

    planned 里每项是 ``{source, target, kind, required}``；缺文件只记 warnings，
    绝不中断——新板可以再补，离线包先尽量完整。
    """
    root = Path(root)
    planned = []
    warnings = []
    seen = {}

    def add(source, target, kind, required=True, extra=None):
        _add_entry(planned, warnings, root, seen, source, target, kind, required, extra)

    # 1) app/ 下所有 *.py 与 app/config.yaml → /home/biandu/rv/room_review/app/
    app_base = root / "app"
    if app_base.is_dir():
        for path in _iter_files(app_base):
            if path.name == "config.yaml" or path.suffix.lower() == ".py":
                rel = path.relative_to(app_base).as_posix()
                add(path, f"{BOARD_REVIEW}/app/{rel}", "repo")
    else:
        warnings.append("源目录缺失，已跳过：app/")

    # 2) configs/ 正式配置与 0.8B 提示词
    configs_base = root / "configs"
    if configs_base.is_dir():
        for path in _iter_files(configs_base):
            rel = path.relative_to(configs_base).as_posix()
            add(path, f"{BOARD_REVIEW}/configs/{rel}", "repo")
    else:
        warnings.append("源目录缺失，已跳过：configs/")

    # 3) scripts/board/ 全部脚本/源码
    board_base = root / "scripts/board"
    if board_base.is_dir():
        for path in _iter_files(board_base, BOARD_SUFFIXES):
            rel = path.relative_to(board_base).as_posix()
            add(path, f"{BOARD_SCRIPTS}/board/{rel}", "repo")
    else:
        warnings.append("源目录缺失，已跳过：scripts/board/")

    # 正式运行依赖必须随包；缺失时标出，不借用历史实验脚本。
    for name in ("vlm_runtime.py", "risk_degree.py"):
        if not (root / "app" / name).is_file():
            warnings.append(f"源文件缺失：app/{name}")

    # 5) gimbal/ 下的 .py/.json/.md
    gimbal_base = root / "gimbal"
    if gimbal_base.is_dir():
        for path in _iter_files(gimbal_base, GIMBAL_SUFFIXES):
            rel = path.relative_to(gimbal_base).as_posix()
            add(path, f"{BOARD_RV}/gimbal/{rel}", "repo")
    else:
        warnings.append("源目录缺失，已跳过：gimbal/")

    # 6) 提示音 wav
    audio_base = root / "results/room_review_20261104/audio"
    wavs = _iter_files(audio_base, {".wav"})
    if not audio_base.is_dir():
        warnings.append("源目录缺失，已跳过：results/room_review_20261104/audio/")
    elif not wavs:
        warnings.append("源目录为空：results/room_review_20261104/audio/")
    for path in wavs:
        rel = path.relative_to(audio_base).as_posix()
        add(path, f"{BOARD_REVIEW}/audio/{rel}", "audio")

    # 8) K1 riscv64 运行时归档（必须带；没有也只警告，不阻断）
    runtime_base = root / "results/k1_validation_20261103/runtime_archives"
    for name in RUNTIME_ARCHIVE_FILES:
        source = runtime_base / name
        if source.is_file():
            add(source, f"{BOARD_VALIDATION}/runtime_archives/{name}", "runtime")
        else:
            warnings.append(f"源文件缺失，已跳过：results/k1_validation_20261103/runtime_archives/{name}")

    # 9) 大模型 tar.gz：存在就随包（不解压），不存在进 missing
    models_dir = Path(models_dir)
    if not models_dir.is_dir():
        warnings.append(f"大模型缓存目录不存在：{models_dir}（大模型将列入 missing，新板联网下载）")
    missing = []
    for model in MODEL_DEFS:
        archive = models_dir / model["archive"]
        if archive.is_file():
            add(
                archive,
                model["bundle_target"],
                "model",
                True,
                {
                    "unpack_to": model["extract_dir"],
                    "model_dir": model["model_dir"],
                },
            )
        else:
            item = {
                "target": model["target_dir"],
                "why": model["why"],
                "url": model["url"],
                "bytes_estimate": model["bytes_estimate"],
            }
            if model["optional"]:
                item["optional"] = True
            missing.append(item)

    # 10) 新板 bringup 脚本（Lead 可能稍后创建；每次运行重新扫描）
    bringup = board_base / BRINGUP_SCRIPT
    if bringup.is_file():
        add(bringup, f"{BOARD_SCRIPTS}/board/{BRINGUP_SCRIPT}", "repo")
    else:
        warnings.append(
            f"源文件缺失，已跳过：scripts/board/{BRINGUP_SCRIPT}（Lead 稍后可能创建，重跑即可收录）"
        )

    return planned, missing, warnings


# ---------------------------------------------------------------------------
# 生成包
# ---------------------------------------------------------------------------

def _entry_record(item, root: Path) -> dict:
    """把扫描项变成 manifest 条目（bytes/sha256 稍后回填）。"""
    target = item["target"]
    record = {
        "target": target,
        "payload": "payload/" + target.lstrip("/"),
        "bytes": 0,
        "sha256": "",
        "kind": item["kind"],
        "required": bool(item["required"]),
        "source": _relative_label(item["source"], root),
    }
    if "unpack_to" in item:
        record["unpack_to"] = item["unpack_to"]
    if "model_dir" in item:
        record["model_dir"] = item["model_dir"]
    return record


def _prepare_out(out: Path) -> None:
    """确认输出目录可用：不存在则新建；已存在且非空则拒绝（避免覆盖旧包）。"""
    if out.exists():
        if out.is_symlink() or not out.is_dir():
            raise BundleError(f"--out 已存在且不是普通目录：{out}")
        if any(out.iterdir()):
            raise BundleError(f"--out 目录已存在且非空：{out}（请先删除或换一个 --out）")
        return
    out.mkdir(parents=True, exist_ok=True)


def _zip_date_time(path: Path):
    """zip 条目时间戳（zip 格式只支持 1980 年以后）。"""
    try:
        stamp = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        stamp = datetime.now()
    if stamp.year < 1980:
        return (1980, 1, 1, 0, 0, 0)
    return (stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second)


def _zip_bundle(out: Path, zip_path: Path) -> None:
    """把整包打成 zip（ZIP_DEFLATED），并保留 sh/desktop 的可执行位。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    files = []
    for dirpath, dirnames, filenames in os.walk(out, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in EXCLUDED_DIRS)
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if full.is_symlink():
                continue
            files.append(full)
    files.sort(key=lambda item: item.relative_to(out).as_posix())
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            arcname = path.relative_to(out).as_posix()
            info = zipfile.ZipInfo(arcname, date_time=_zip_date_time(path))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100000 | _payload_mode(path)) << 16
            with open(path, "rb") as src, archive.open(info, "w") as dst:
                shutil.copyfileobj(src, dst, CHUNK_SIZE)


def build(
    root,
    out=None,
    zip_bundle: bool = False,
    check_only: bool = False,
    models_dir=None,
    progress=None,
):
    """生成（或只扫描）离线恢复包，返回 manifest 字典。不打印到 stdout。"""
    root = Path(root).expanduser().absolute()
    if not root.is_dir():
        raise BundleError(f"仓库根目录不存在或不是目录：{root}")

    if out is None:
        out = root / "dist" / f"newboard_bundle_{datetime.now().strftime(DEFAULT_DATE_FORMAT)}"
    out = Path(out).expanduser().absolute()

    if models_dir is None:
        models_dir = root / ".cache/newboard"
    else:
        models_dir = Path(models_dir).expanduser().absolute()

    planned, missing, warnings = collect(root, models_dir)
    if progress:
        progress(f"扫描完成：计划收录 {len(planned)} 个文件，缺失大模型 {len(missing)} 个")

    zip_path = Path(str(out) + ".zip") if zip_bundle else None
    if not check_only:
        _prepare_out(out)

    records = []
    total = len(planned)
    for index, item in enumerate(planned, 1):
        record = _entry_record(item, root)
        source = item["source"]
        if progress:
            try:
                size_hint = _human_bytes(source.stat().st_size)
            except OSError:
                size_hint = "未知大小"
            progress(f"  [{index}/{total}] {size_hint}  {record['target']}")
        if check_only:
            size, digest = _hash_file(source)
        else:
            size, digest = _copy_and_hash(source, out / record["payload"])
        record["bytes"] = size
        record["sha256"] = digest
        records.append(record)
    records.sort(key=lambda entry: entry["target"])

    manifest = {
        "version": VERSION,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "root": str(root),
        "out": str(out),
        "check_only": bool(check_only),
        "entries": records,
        "missing": missing,
        "warnings": warnings,
        "totals": {
            "files": len(records),
            "bytes": sum(entry["bytes"] for entry in records),
        },
        "zip": str(zip_path) if zip_path else None,
    }

    if check_only:
        return manifest

    _write_text(out / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    _write_checksums(out / "SHA256SUMS", records)
    _write_text(out / "README-恢复说明.txt", README_TEXT)
    if zip_path:
        if progress:
            progress(f"正在生成 zip：{zip_path}")
        _zip_bundle(out, zip_path)
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _format_summary(manifest) -> str:
    lines = []
    if manifest.get("check_only"):
        lines.append("新板离线恢复包（--check-only：仅扫描，未写任何文件）")
    else:
        lines.append("新板离线恢复包已生成")
    lines.append(f"  输出目录：{manifest['out']}")
    lines.append(
        f"  收录文件：{manifest['totals']['files']} 个，共 {_human_bytes(manifest['totals']['bytes'])}"
    )
    kinds = {}
    for entry in manifest["entries"]:
        kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
    if kinds:
        lines.append("  分类：" + "，".join(f"{name}={count}" for name, count in sorted(kinds.items())))
    if manifest.get("zip"):
        lines.append(f"  ZIP 包：{manifest['zip']}")
    if not manifest.get("check_only"):
        lines.append("  校验命令：cd <包目录> && sha256sum -c SHA256SUMS")
    missing = manifest["missing"]
    if missing:
        lines.append(f"  缺失大模型：{len(missing)} 个（需新板联网下载，不计为失败）")
        for item in missing:
            tag = "可选" if item.get("optional") else "必需"
            lines.append(f"    - [{tag}] {item['target']}")
            lines.append(f"      URL：{item['url']}（约 {_human_bytes(item['bytes_estimate'])}）")
    else:
        lines.append("  缺失大模型：无（所需模型均已随包）")
    warnings = manifest["warnings"]
    if warnings:
        lines.append(f"  跳过/警告：{len(warnings)} 条")
        for text in warnings:
            lines.append(f"    - {text}")
    return "\n".join(lines)


def _use_utf8_stdio() -> None:
    """尽量让中文输出在 Windows 控制台/重定向下都不乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _progress(message: str) -> None:
    print(message, file=sys.stderr)


def main(argv=None) -> int:
    _use_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="make_offline_bundle.py",
        description="PC 侧新板离线恢复包生成器（只读扫描仓库，输出到 dist/ 不污染源码）",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="输出目录，默认 <root>/dist/newboard_bundle_YYYYMMDD；相对路径按当前目录解析",
    )
    parser.add_argument("--zip", action="store_true", dest="zip_bundle", help="打包完成后生成 <out>.zip")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只扫描并打印清单，不创建任何目录或文件",
    )
    parser.add_argument("--json", action="store_true", help="stdout 只输出一个 JSON 摘要")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="仓库根目录（默认按脚本位置推算）")
    parser.add_argument(
        "--models-dir",
        default=None,
        help="大模型 tar.gz 缓存目录，默认 <root>/.cache/newboard",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(f"错误：--root 不是有效目录：{root}", file=sys.stderr)
        return 2
    if args.check_only and args.zip_bundle:
        print("提示：--check-only 模式下忽略 --zip（不生成 zip）", file=sys.stderr)

    try:
        manifest = build(
            root=root,
            out=args.out,
            zip_bundle=(args.zip_bundle and not args.check_only),
            check_only=args.check_only,
            models_dir=args.models_dir,
            progress=None if args.json else _progress,
        )
    except BundleError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"运行错误：{exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    else:
        print(_format_summary(manifest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
