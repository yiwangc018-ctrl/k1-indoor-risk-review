#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YOLO 训练预检与命令生成器（本脚手架不执行训练）。

这个脚本只做三件事：
1. 检查 data.yaml、数据集目录、标签对齐、类别名/类别数等参数是否自洽；
2. 检查 ultralytics / torch 是否可用，并明确提示缺失；
3. 生成**一条**可复制执行的 ``yolo detect train ...`` 命令。

它**不会**：
* 导入 ultralytics 或 torch（只用 importlib 查包是否存在）；
* 下载 ``yolov8n.pt`` 或任何模型；
* 创建 ``runs/``、写训练产物；
* 调用 ``model.train()`` 或 ``yolo`` CLI。

因此无数据或没有 ultralytics 时，不存在“偷偷训练”的路径。

基线（可用参数覆盖）：
    model=yolov8n.pt  imgsz=640  epochs=50  batch=auto  seed=0  device=cpu

退出码
------
0  预检通过（可能有 warning；--dry-run 下缺 ultralytics 只告警）
1  参数/路径/数据问题
2  NO_DATA：没有训练图片
3  ultralytics 不可用（非 --dry-run 时）或参数用法错误

用法
----
    python scripts/train/train_yolo.py --help
    python scripts/train/train_yolo.py --data data/dataset_example/dataset.yaml \\
        --dataset data/dataset_example --dry-run
"""
from __future__ import annotations

import argparse
import importlib.util
import platform
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # pragma: no cover
    pass

IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"})
DEFAULT_CLASSES = ("door_threshold", "carpet_cable", "clutter")
DEFAULT_DATA = "data/dataset_example/dataset.yaml"
DEFAULT_MODEL = "yolov8n.pt"
DEFAULT_IMGSZ = 640
DEFAULT_EPOCHS = 50
DEFAULT_BATCH = "auto"
DEFAULT_SEED = 0
DEFAULT_DEVICE = "cpu"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NO_DATA = 2
EXIT_USAGE = 3


@dataclass
class Check:
    level: str  # OK / WARN / ERROR
    code: str
    message: str


@dataclass
class YamlInfo:
    has_train: bool = False
    has_val: bool = False
    has_test: bool = False
    has_names: bool = False
    names: list[str] | None = None
    nc: int | None = None
    path_value: str | None = None
    train_path: str | None = None
    val_path: str | None = None
    test_path: str | None = None


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def build_parser() -> _Parser:
    p = _Parser(
        description="YOLO 训练预检 + 生成一条训练命令（不下载模型、不执行训练）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "退出码：0 通过 / 1 参数或数据问题 / 2 NO_DATA / 3 ultralytics 缺失或用法错误\n"
            "本脚本永远不会调用训练；请复制输出的命令到训练机手动执行。"
        ),
    )
    p.add_argument(
        "--data",
        default=DEFAULT_DATA,
        help="dataset.yaml 路径，默认 %(default)s",
    )
    p.add_argument(
        "--dataset",
        default=None,
        help="数据集根目录（含 images/ labels/）；默认取 data.yaml 所在目录",
    )
    p.add_argument("--model", default=DEFAULT_MODEL, help="模型/权重，默认 %(default)s")
    p.add_argument("--imgsz", type=int, default=DEFAULT_IMGSZ, help="输入尺寸，默认 %(default)s（必须是 32 的倍数）")
    p.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS, help="训练轮数，默认 %(default)s")
    p.add_argument("--batch", default=DEFAULT_BATCH, help="批大小，默认 %(default)s；也可给正整数")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子，默认 %(default)s")
    p.add_argument("--device", default=DEFAULT_DEVICE, help="cpu / mps / 0 / 0,1，默认 %(default)s")
    p.add_argument("--project", default="runs/train", help="训练输出根目录，默认 %(default)s")
    p.add_argument("--name", default="risk3-v0", help="本次运行名，默认 %(default)s")
    p.add_argument(
        "--classes",
        default=",".join(DEFAULT_CLASSES),
        help="逗号分隔类别名，顺序即 class id；默认 3 类 %(default)s",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只做离线检查并打印命令；缺 ultralytics 只告警。注意：不加此参数也不会执行训练",
    )
    return p


def parse_classes(raw: str, parser: _Parser) -> tuple[str, ...]:
    names = tuple(x.strip() for x in raw.split(",") if x.strip())
    if not names:
        parser.error("--classes 不能为空")
    if len(set(names)) != len(names):
        parser.error("--classes 存在重复类别名")
    return names


def validate_args(args: argparse.Namespace, parser: _Parser) -> None:
    if not str(args.data).strip():
        parser.error("--data 不能为空")
    if args.dataset is not None and not str(args.dataset).strip():
        parser.error("--dataset 不能为空字符串")
    if args.imgsz <= 0 or args.imgsz % 32 != 0:
        parser.error(f"--imgsz 必须是 32 的正整数倍，实际 {args.imgsz}")
    if args.epochs <= 0:
        parser.error(f"--epochs 必须 > 0，实际 {args.epochs}")
    if args.seed < 0:
        parser.error(f"--seed 必须 >= 0，实际 {args.seed}")
    batch = str(args.batch).strip().lower()
    if batch != "auto":
        try:
            value = int(batch)
        except ValueError:
            parser.error(f"--batch 只能是 auto 或正整数，实际 {args.batch!r}")
        if value <= 0:
            parser.error(f"--batch 必须 > 0，实际 {args.batch!r}")
        args.batch = str(value)
    else:
        args.batch = "auto"
    if not re.fullmatch(r"(cpu|mps|cuda(:\d+)?|\d+(,\d+)*)", args.device.strip()):
        parser.error(f"--device 只支持 cpu / mps / cuda[:N] / 0 / 0,1，实际 {args.device!r}")
    args.device = args.device.strip()
    if not args.model.strip():
        parser.error("--model 不能为空")
    for label, value in (("--project", args.project), ("--name", args.name)):
        if not str(value).strip():
            parser.error(f"{label} 不能为空")
        if ".." in Path(str(value)).parts:
            parser.error(f"{label} 不能包含 '..'，实际 {value!r}")


# ---------------------------------------------------------------------------
# data.yaml 轻量检查
# ---------------------------------------------------------------------------


def _strip_comment(text: str) -> str:
    quote = ""
    for index, char in enumerate(text):
        if char in ("'", '"'):
            if quote == char:
                quote = ""
            elif not quote:
                quote = char
        elif char == "#" and not quote and (index == 0 or text[index - 1].isspace()):
            return text[:index].strip()
    return text.strip()


def _strip_scalar(text: str) -> str:
    value = _strip_comment(text).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()


def _split_inline_list(text: str) -> list[str]:
    inner = text.strip()
    if inner.startswith("["):
        inner = inner[1:]
    if inner.endswith("]"):
        inner = inner[:-1]
    return [_strip_scalar(part) for part in inner.split(",") if _strip_scalar(part)]


def _parse_names(lines: Sequence[str]) -> list[str] | None:
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)names\s*:\s*(.*?)\s*$", line)
        if not match:
            continue
        indent = len(match.group(1))
        inline = _strip_comment(match.group(2))
        if inline.startswith("["):
            names = _split_inline_list(inline)
            return names or None
        if inline.startswith("{"):
            entries: list[tuple[int, str]] = []
            for part in inline.strip()[1:-1].split(","):
                if ":" not in part:
                    continue
                key, value = part.split(":", 1)
                try:
                    entries.append((int(key.strip()), _strip_scalar(value)))
                except ValueError:
                    continue
            return [value for _, value in sorted(entries)] if entries else None
        if inline:
            # names: my_names（YAML anchor）等复杂写法，不做猜测。
            return None
        entries = []
        for child in lines[index + 1:]:
            if not child.strip() or child.lstrip().startswith("#"):
                continue
            child_indent = len(child) - len(child.lstrip())
            item = child.strip()
            is_list_item = item.startswith("- ")
            if child_indent <= indent and not is_list_item:
                break
            if is_list_item:
                item = item[2:].strip()
            item = _strip_comment(item)
            if not item:
                continue
            key_match = re.match(r"^(\d+)\s*:\s*(.+)$", item)
            if key_match:
                entries.append((int(key_match.group(1)), _strip_scalar(key_match.group(2))))
            else:
                entries.append((len(entries), _strip_scalar(item)))
        if not entries:
            return None
        keys = [key for key, _ in entries]
        if len(set(keys)) != len(keys):
            return None
        return [value for _, value in sorted(entries)]
    return None


def inspect_data_yaml(path: Path, classes: Sequence[str], checks: list[Check]) -> YamlInfo:
    info = YamlInfo()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        checks.append(Check("ERROR", "DATA_YAML_ENCODING", f"data.yaml 不是 UTF-8：{exc}"))
        return info
    except OSError as exc:
        checks.append(Check("ERROR", "DATA_YAML_READ", f"读取 data.yaml 失败：{exc}"))
        return info
    if not text.strip():
        checks.append(Check("ERROR", "DATA_YAML_EMPTY", "data.yaml 是空文件"))
        return info

    lines = text.splitlines()
    info.has_train = any(re.match(r"^\s*train\s*:", line) for line in lines)
    info.has_val = any(re.match(r"^\s*val\s*:", line) for line in lines)
    info.has_test = any(re.match(r"^\s*test\s*:", line) for line in lines)
    info.has_names = any(re.match(r"^\s*names\s*:", line) for line in lines)
    def key_value(key: str) -> str | None:
        match = re.search(rf"(?m)^\s*{re.escape(key)}\s*:\s*(.+?)\s*(?:#.*)?$", text)
        if not match:
            return None
        value = _strip_scalar(match.group(1))
        return value or None

    info.path_value = key_value("path")
    info.train_path = key_value("train")
    info.val_path = key_value("val")
    info.test_path = key_value("test")
    for key, value in (("train", info.train_path), ("val", info.val_path), ("test", info.test_path)):
        if not value:
            continue
        normalized = value.replace("\\", "/").rstrip("/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized != f"images/{key}":
            checks.append(
                Check(
                    "WARN",
                    "DATA_YAML_SPLIT_PATH",
                    f"data.yaml {key}: {value} 不是标准 images/{key}；"
                    "脚本只会按 images/train|val|test 做目录检查，请确认两者指向同一批数据",
                )
            )

    if not info.has_train:
        checks.append(Check("ERROR", "DATA_YAML_TRAIN", "data.yaml 缺少 train: 键"))
    if not info.has_val:
        checks.append(Check("ERROR", "DATA_YAML_VAL", "data.yaml 缺少 val: 键"))
    if not info.has_test:
        checks.append(Check("WARN", "DATA_YAML_TEST", "data.yaml 缺少 test: 键；最终测试指标无法产生"))
    if not info.has_names:
        checks.append(Check("ERROR", "DATA_YAML_NAMES", "data.yaml 缺少 names: 键"))
        return info

    names = _parse_names(lines)
    if names is None:
        checks.append(
            Check(
                "ERROR",
                "DATA_YAML_NAMES_PARSE",
                "无法解析 names（支持 0: name 映射或 [name, ...] 列表）；请改成简单写法后重试",
            )
        )
    else:
        info.names = names
        if len(names) != len(classes):
            checks.append(
                Check(
                    "ERROR",
                    "CLASS_COUNT_MISMATCH",
                    f"data.yaml 有 {len(names)} 类，但 --classes 有 {len(classes)} 类",
                )
            )
        elif [x.strip() for x in names] != [x.strip() for x in classes]:
            checks.append(
                Check(
                    "ERROR",
                    "CLASS_NAME_MISMATCH",
                    f"data.yaml names={names} 与 --classes={list(classes)} 不一致；"
                    "请统一类名后再训练，避免标签 id 错位",
                )
            )
        else:
            checks.append(Check("OK", "DATA_YAML_NAMES_OK", f"names 匹配：{', '.join(names)}"))

    nc_match = re.search(r"(?m)^\s*nc\s*:\s*(\d+)\s*(?:#.*)?$", text)
    if nc_match:
        info.nc = int(nc_match.group(1))
        if info.nc != len(classes):
            checks.append(
                Check("ERROR", "NC_MISMATCH", f"data.yaml nc={info.nc}，但 --classes 有 {len(classes)} 类")
            )
    return info


# ---------------------------------------------------------------------------
# 数据集预检
# ---------------------------------------------------------------------------


def _image_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file()
        and path.suffix.lower() in IMAGE_EXTS
        and not any(part.startswith(".") for part in path.relative_to(directory).parts)
    )


def _missing_labels(image_dir: Path, label_dir: Path, images: Sequence[Path]) -> list[Path]:
    missing: list[Path] = []
    for image in images:
        label = label_dir / image.relative_to(image_dir).with_suffix(".txt")
        if not label.is_file():
            missing.append(image)
    return missing


def inspect_dataset(
    dataset_root: Path,
    checks: list[Check],
) -> tuple[bool, int, int]:
    """返回 (有没有任何可用数据, train 数量, val 数量)。"""
    if not dataset_root.exists():
        checks.append(Check("ERROR", "NO_DATA", f"数据集目录不存在：{dataset_root}"))
        return False, 0, 0
    if not dataset_root.is_dir():
        checks.append(Check("ERROR", "DATASET_NOT_DIR", f"数据集路径不是目录：{dataset_root}"))
        return False, 0, 0

    train_images = _image_files(dataset_root / "images" / "train")
    val_images = _image_files(dataset_root / "images" / "val")
    test_images = _image_files(dataset_root / "images" / "test")
    if not train_images and not val_images:
        checks.append(
            Check(
                "ERROR",
                "NO_DATA",
                f"{dataset_root}/images/{{train,val}} 下没有图片；"
                "请先采集/放置数据，或检查 --dataset 是否指对",
            )
        )
        return False, 0, 0

    checks.append(Check("OK", "DATA_FOUND", f"train={len(train_images)} 张，val={len(val_images)} 张，test={len(test_images)} 张"))
    if not train_images:
        checks.append(Check("ERROR", "EMPTY_TRAIN", "images/train 没有图片，无法训练"))
    if not val_images:
        checks.append(Check("ERROR", "EMPTY_VAL", "images/val 没有图片，无法计算验证指标"))
    if not test_images:
        checks.append(Check("WARN", "EMPTY_TEST", "images/test 没有图片；训练可以开始，但最终测试指标无法产生"))

    for split, images in (("train", train_images), ("val", val_images)):
        label_dir = dataset_root / "labels" / split
        if not label_dir.is_dir():
            checks.append(Check("ERROR", "LABELS_DIR_MISSING", f"缺少 labels/{split}/ 目录"))
            continue
        missing = _missing_labels(dataset_root / "images" / split, label_dir, images)
        if missing:
            checks.append(
                Check(
                    "ERROR",
                    "LABEL_ALIGNMENT",
                    f"labels/{split} 有 {len(missing)} 张图片缺同名 .txt；示例："
                    f"{missing[0].relative_to(dataset_root).as_posix()}",
                )
            )
        else:
            checks.append(Check("OK", "LABEL_ALIGNMENT", f"labels/{split} 与 images/{split} 一一对应"))

    if train_images and len(train_images) < 10:
        checks.append(Check("WARN", "TRAIN_SMALL", f"train 只有 {len(train_images)} 张，只能做连通性验证"))
    if train_images and val_images and len(train_images) + len(val_images) < 30:
        checks.append(
            Check("WARN", "DATA_SMALL", "train+val 少于 30 张；不要用这次训练的 mAP 作为比赛准确率")
        )
    return True, len(train_images), len(val_images)


def package_status(name: str) -> tuple[bool, str]:
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ModuleNotFoundError, ValueError):
        spec = None
    if spec is None:
        return False, ""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return True, version(name)
        except PackageNotFoundError:
            return True, "unknown"
    except Exception:  # pragma: no cover - 元数据损坏
        return True, "unknown"


def inspect_environment(args: argparse.Namespace, checks: list[Check]) -> None:
    ultra_ok, ultra_version = package_status("ultralytics")
    torch_ok, torch_version = package_status("torch")
    if ultra_ok:
        checks.append(Check("OK", "ULTRALYTICS_OK", f"ultralytics {ultra_version} 可用"))
    else:
        level = "WARN" if args.dry_run else "ERROR"
        checks.append(
            Check(
                level,
                "NO_ULTRALYTICS",
                "未检测到 ultralytics；不会训练。安装示例：python -m pip install ultralytics "
                "（需联网；本脚本不安装、不下载模型）",
            )
        )
    if torch_ok:
        checks.append(Check("OK", "TORCH_OK", f"torch {torch_version} 可用"))
    else:
        level = "WARN" if args.dry_run else "ERROR"
        checks.append(
            Check(
                level,
                "NO_TORCH",
                "未检测到 torch；ultralytics 训练需要 PyTorch。本脚本不会安装依赖",
            )
        )
    if sys.version_info < (3, 9):
        checks.append(Check("WARN", "PYTHON_OLD", f"Python {sys.version.split()[0]} 较旧，建议 3.10/3.11"))


def inspect_weights(args: argparse.Namespace, checks: list[Check]) -> None:
    raw = Path(args.model).expanduser()
    local = raw if raw.is_absolute() else Path.cwd() / raw
    if local.is_file():
        checks.append(Check("OK", "WEIGHTS_LOCAL", f"本地权重存在：{local}"))
        return
    checks.append(
        Check(
            "WARN",
            "WEIGHTS_NOT_LOCAL",
            f"本地未找到 {args.model}；ultralytics 执行命令时可能联网下载预训练权重。"
            "本轮脚手架不下载、不执行，请人工确认网络与授权",
        )
    )


# ---------------------------------------------------------------------------
# 命令与输出
# ---------------------------------------------------------------------------


def _shell_value(value: object) -> str:
    text = str(value)
    if re.fullmatch(r"[A-Za-z0-9_./:=,@%+\-]+", text):
        return text
    return '"' + text.replace('"', '\\"') + '"'


def build_command(args: argparse.Namespace, data_yaml: Path) -> str:
    pairs = [
        ("data", data_yaml.resolve().as_posix()),
        ("model", args.model),
        ("imgsz", args.imgsz),
        ("epochs", args.epochs),
        ("batch", args.batch),
        ("seed", args.seed),
        ("device", args.device),
        ("deterministic", "True"),
        ("project", args.project),
        ("name", args.name),
        ("exist_ok", "False"),
    ]
    return "yolo detect train " + " ".join(f"{key}={_shell_value(value)}" for key, value in pairs)


def _print_checks(checks: Sequence[Check]) -> None:
    print("\n[检查]")
    if not checks:
        print("  (无)")
        return
    for check in checks:
        print(f"  {check.level:<5} {check.code}: {check.message}")


def print_plan(
    args: argparse.Namespace,
    classes: Sequence[str],
    data_yaml: Path,
    dataset_root: Path,
    checks: Sequence[Check],
    command: str | None,
    no_data: bool,
    exit_code: int,
) -> None:
    print("=" * 78)
    print("YOLO 训练预检（只生成命令，不执行训练）")
    print("=" * 78)
    print(f"mode    : {'--dry-run（离线）' if args.dry_run else '执行前预检（仍不自动训练）'}")
    print(f"data    : {data_yaml}")
    print(f"dataset : {dataset_root}")
    print(f"classes : {len(classes)} -> {', '.join(classes)}")
    print("\n[参数]")
    model_note = "  (基线 yolov8n)" if args.model == DEFAULT_MODEL else ""
    print(f"  model         {args.model}{model_note}")
    print(f"  imgsz         {args.imgsz}")
    print(f"  epochs        {args.epochs}")
    print(f"  batch         {args.batch}")
    print(f"  seed          {args.seed}")
    print(f"  device        {args.device}")
    print(f"  deterministic True")
    print(f"  project/name  {args.project}/{args.name}")
    print(f"  python        {sys.version.split()[0]}")
    print(f"  platform      {platform.platform()}")

    _print_checks(checks)
    errors = [c for c in checks if c.level == "ERROR"]
    warnings = [c for c in checks if c.level == "WARN"]

    print("\n[训练命令]")
    if command:
        print("  " + command)
        if args.batch == "auto":
            print("  提示：batch=auto 是基线写法；若本机 ultralytics 不接受，改为 batch=-1（自动）或正整数。")
    else:
        print("  (未生成：先修复上述错误或提供数据)")

    print("\n" + "-" * 78)
    # Configuration errors take precedence over an empty dataset, while the
    # expected NO_DATA check remains a distinct result even though it is an
    # ERROR-level check internally.
    config_errors = [
        c for c in errors
        if c.code not in ("NO_DATA", "NO_ULTRALYTICS", "NO_TORCH")
    ]
    if config_errors:
        print(f"RESULT: BLOCKED —— {len(config_errors)} 个错误，{len(warnings)} 个警告（退出码 {exit_code}）")
    elif no_data:
        print(f"RESULT: NO_DATA —— 没有可训练图片，未生成可执行计划（退出码 {exit_code}）")
    elif errors:
        print(f"RESULT: BLOCKED —— {len(errors)} 个错误，{len(warnings)} 个警告（退出码 {exit_code}）")
    elif warnings:
        print(f"RESULT: READY_WITH_WARNINGS —— 0 个错误，{len(warnings)} 个警告（退出码 {exit_code}）")
    else:
        print(f"RESULT: READY —— 0 个错误，0 个警告（退出码 {exit_code}）")
    print("未执行：本脚本不导入 ultralytics、不下载模型、不创建 runs/、不调用训练。")
    print("请复制[训练命令]到训练机手动执行；执行前先跑 check_dataset.py。")


def run(args: argparse.Namespace, classes: Sequence[str]) -> int:
    data_yaml = Path(args.data).expanduser()
    if not data_yaml.is_absolute():
        data_yaml = Path.cwd() / data_yaml
    try:
        data_yaml = data_yaml.resolve()
    except OSError:
        pass

    checks: list[Check] = []
    info = YamlInfo()
    if not data_yaml.is_file():
        checks.append(Check("ERROR", "DATA_YAML_MISSING", f"data.yaml 不存在：{data_yaml}；请从 dataset.yaml.example 复制并修改"))
    else:
        checks.append(Check("OK", "DATA_YAML_FOUND", f"data.yaml 存在：{data_yaml}"))
        if data_yaml.suffix.lower() == ".example":
            checks.append(Check("WARN", "DATA_YAML_EXAMPLE", "正在使用 .example 示例文件；训练前应复制为 dataset.yaml 并核对路径"))
        info = inspect_data_yaml(data_yaml, classes, checks)

    if args.dataset:
        dataset_root = Path(args.dataset).expanduser()
    elif info.path_value:
        # 与 ultralytics 一致：path 相对于 data.yaml 所在目录。
        raw_root = Path(info.path_value).expanduser()
        dataset_root = raw_root if raw_root.is_absolute() else data_yaml.parent / raw_root
    else:
        dataset_root = data_yaml.parent
    if not dataset_root.is_absolute():
        dataset_root = Path.cwd() / dataset_root
    try:
        dataset_root = dataset_root.resolve()
    except OSError:
        pass

    has_data, _train_count, _val_count = inspect_dataset(dataset_root, checks)
    inspect_weights(args, checks)
    inspect_environment(args, checks)

    errors = [c for c in checks if c.level == "ERROR"]
    # data.yaml 缺失 / 类别不匹配 / 标签不对齐属于配置错误；数据集不存在或没图片才是 NO_DATA。
    config_errors = [
        c
        for c in errors
        if c.code not in ("NO_DATA", "NO_ULTRALYTICS", "NO_TORCH")
    ]
    genuinely_no_data = not has_data and not any(c.code == "DATA_YAML_MISSING" for c in checks)
    command: str | None = None
    if has_data and not config_errors:
        # 即使 ultralytics 缺失也打印命令：装好依赖后可直接复制执行。
        command = build_command(args, data_yaml)

    if config_errors:
        exit_code = EXIT_FAIL
    elif genuinely_no_data:
        exit_code = EXIT_NO_DATA
    elif errors:
        only_ultralytics = all(c.code in ("NO_ULTRALYTICS", "NO_TORCH") for c in errors)
        exit_code = EXIT_USAGE if (only_ultralytics and not args.dry_run) else EXIT_FAIL
    else:
        exit_code = EXIT_OK

    print_plan(
        args=args,
        classes=classes,
        data_yaml=data_yaml,
        dataset_root=dataset_root,
        checks=checks,
        command=command,
        no_data=genuinely_no_data and not config_errors,
        exit_code=exit_code,
    )
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(args, parser)
    classes = parse_classes(args.classes, parser)
    return run(args, classes)


if __name__ == "__main__":
    sys.exit(main())
