#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YOLO 数据集静态检查（只用 Python 标准库）。

检查内容
--------
* 目录结构：images/{train,val,test} 与 labels/{train,val,test} 成对存在；
* 图片/标签对齐：每张图片有同名 .txt，不出现没有图片的孤儿标签；
* 空标签：允许（纯负例），但会统计比例；全空或占比过高会报错/警告；
* 类别 id：必须为整数且位于 [0, 类别数)；
* 框坐标：YOLO 的 cx/cy/w/h 均为 0..1，w/h > 0，且框完整落在画面内；
* 重复文件：对图片和标签计算 SHA256；同内容图片（同 split 或跨 split）报错，
  非空标签内容重复只给出提示（相同框坐标可能是正常样本）；
* 家庭/场景泄漏：从 sidecar metadata（CSV/JSON）或
  ``family__scene__frame.ext`` 文件名读取 family_id / scene_id；
  同一 family 或同一 (family, scene) 跨 train/val/test 出现即报错。

本脚本不读取图片像素，不依赖 Pillow / OpenCV，也不下载任何东西。

退出码
------
0  检查通过（可能有 warning）
1  发现错误
2  NO_DATA：没有找到任何图片（不会假装通过）
3  参数或用法错误

用法
----
    python scripts/train/check_dataset.py --dataset data/dataset_example
    python scripts/train/check_dataset.py --dataset datasets/risk3-v0 \\
        --meta datasets/risk3-v0/metadata.csv --report results/dataset-check.json

    python scripts/train/check_dataset.py --help
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # pragma: no cover - 老 Python / 非标准流
    pass

IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"})
SPLITS = ("train", "val", "test")
DEFAULT_CLASSES = ("door_threshold", "carpet_cable", "clutter")
DEFAULT_DATASET = "data/dataset_example"
MAX_DETAIL = 20
EPS = 1e-9

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NO_DATA = 2
EXIT_USAGE = 3


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Issue:
    level: str  # ERROR / WARN / INFO
    code: str
    message: str
    path: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "message": self.message, "path": self.path}


@dataclass
class ImageRecord:
    split: str
    path: Path
    rel: str
    label_path: Path
    sha256: str = ""
    label_sha256: str = ""
    family: str = ""
    scene: str = ""
    frame: str = ""
    id_source: str = "unknown"
    label_exists: bool = False
    boxes: int = 0
    empty_label: bool = False
    class_counts: Counter[int] = field(default_factory=Counter)


class Report:
    def __init__(self, root: Path, classes: Sequence[str]) -> None:
        self.root = root
        self.classes = list(classes)
        self.issues: list[Issue] = []
        self.images: list[ImageRecord] = []
        self.missing_dirs: list[str] = []
        self.metadata_files: list[str] = []
        self.duplicates: list[dict[str, Any]] = []
        self.status = "FAIL"
        self.exit_code = EXIT_FAIL

    # -- 记录问题 ---------------------------------------------------------
    def add(self, level: str, code: str, message: str, path: str = "") -> None:
        self.issues.append(Issue(level, code, message, path))

    def error(self, code: str, message: str, path: str = "") -> None:
        self.add("ERROR", code, message, path)

    def warn(self, code: str, message: str, path: str = "") -> None:
        self.add("WARN", code, message, path)

    def info(self, code: str, message: str, path: str = "") -> None:
        self.add("INFO", code, message, path)

    # -- 统计 -------------------------------------------------------------
    @property
    def errors(self) -> list[Issue]:
        return [x for x in self.issues if x.level == "ERROR"]

    @property
    def warnings(self) -> list[Issue]:
        return [x for x in self.issues if x.level == "WARN"]

    def split_images(self, split: str) -> list[ImageRecord]:
        return [r for r in self.images if r.split == split]

    def split_stats(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for split in SPLITS:
            recs = self.split_images(split)
            labeled = [r for r in recs if r.label_exists]
            out[split] = {
                "images": len(recs),
                "labels": len(labeled),
                "empty_labels": sum(1 for r in labeled if r.empty_label),
                "boxes": sum(r.boxes for r in recs),
                "families": len({r.family for r in recs if r.family}),
                "scenes": len({(r.family, r.scene) for r in recs if r.family and r.scene}),
            }
        return out

    def class_counts(self) -> dict[str, int]:
        counts: Counter[int] = Counter()
        for rec in self.images:
            counts.update(rec.class_counts)
        return {name: int(counts.get(i, 0)) for i, name in enumerate(self.classes)}

    def to_dict(self) -> dict[str, Any]:
        stats = self.split_stats()
        labeled = [r for r in self.images if r.label_exists]
        empty = [r for r in labeled if r.empty_label]
        return {
            "schema": "yolo-dataset-check/1",
            "dataset": str(self.root),
            "status": self.status,
            "exit_code": self.exit_code,
            "classes": self.classes,
            "metadata_files": self.metadata_files,
            "summary": {
                "images": len(self.images),
                "labels": len(labeled),
                "empty_labels": len(empty),
                "empty_label_ratio": round(len(empty) / len(labeled), 4) if labeled else None,
                "boxes": sum(r.boxes for r in self.images),
                "families": len({r.family for r in self.images if r.family}),
                "unknown_family": sum(1 for r in self.images if not r.family),
                "unknown_scene": sum(1 for r in self.images if not r.scene),
                "class_counts": self.class_counts(),
                "splits": stats,
            },
            "duplicates": self.duplicates,
            "issues": [x.as_dict() for x in self.issues],
            "images": [
                {
                    "split": r.split,
                    "path": r.rel,
                    "label": r.label_path.relative_to(self.root).as_posix()
                    if r.label_path.is_relative_to(self.root)
                    else str(r.label_path),
                    "sha256": r.sha256,
                    "label_sha256": r.label_sha256,
                    "family": r.family,
                    "scene": r.scene,
                    "frame": r.frame,
                    "id_source": r.id_source,
                    "label_exists": r.label_exists,
                    "boxes": r.boxes,
                    "empty_label": r.empty_label,
                    "class_counts": {name: int(r.class_counts.get(i, 0)) for i, name in enumerate(self.classes)},
                }
                for r in self.images
            ],
        }


# ---------------------------------------------------------------------------
# 参数与通用工具
# ---------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """让用法错误返回 3，避免和 NO_DATA 的退出码 2 混淆。"""

    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def build_parser() -> _Parser:
    p = _Parser(
        description="YOLO 数据集静态检查（纯标准库；不读像素、不下载、不训练）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "退出码：0 通过 / 1 有错误 / 2 NO_DATA / 3 参数错误\n"
            "没有数据时一定返回 2，不会输出“通过”。"
        ),
    )
    p.add_argument(
        "--dataset",
        default=DEFAULT_DATASET,
        help="数据集根目录（其下应有 images/ 与 labels/），默认 %(default)s",
    )
    p.add_argument(
        "--classes",
        default=",".join(DEFAULT_CLASSES),
        help="逗号分隔的类别名，顺序即 class id；默认 3 类 %(default)s",
    )
    p.add_argument(
        "--meta",
        default=None,
        help="显式 sidecar metadata 文件或目录（.csv/.json）；不填则自动找 "
        "metadata.csv/json、meta.csv/json、sidecar.csv/json 或 sidecar/、metadata/ 目录",
    )
    p.add_argument(
        "--report",
        default=None,
        help="可选：把完整 JSON 报告写到该路径（父目录自动创建）",
    )
    p.add_argument(
        "--max-list",
        type=int,
        default=MAX_DETAIL,
        help="终端每个问题类别最多列出多少条，默认 %(default)s",
    )
    return p


def parse_classes(raw: str, parser: _Parser) -> tuple[str, ...]:
    names = tuple(x.strip() for x in raw.split(",") if x.strip())
    if not names:
        parser.error("--classes 不能为空")
    if len(set(names)) != len(names):
        parser.error("--classes 存在重复类别名")
    return names


def norm_key(value: Any) -> str:
    """把 metadata 里的图片键统一成 posix 风格相对路径。"""
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def read_text(path: Path, *, encoding: str = "utf-8-sig") -> str:
    with path.open("r", encoding=encoding) as fh:
        return fh.read()


def read_text_fallback(path: Path) -> str:
    """metadata 可能是 Excel 导出的 GBK；只给 metadata 用这个回退。"""
    last: UnicodeDecodeError | None = None
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return read_text(path, encoding=enc)
        except UnicodeDecodeError as exc:  # pragma: no cover - 取决于文件
            last = exc
    if last is not None:
        raise last
    raise ValueError(f"无法解码：{path}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_image_files(directory: Path) -> Iterable[Path]:
    if not directory.is_dir():
        return []
    found: list[Path] = []
    for path in directory.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTS:
            continue
        rel = path.relative_to(directory)
        if any(part.startswith(".") for part in rel.parts):
            continue
        found.append(path)
    return sorted(found)


def parse_filename(stem: str) -> tuple[str, str, str]:
    """解析 family__scene__frame；分隔符必须是双下划线。"""
    parts = stem.split("__")
    if len(parts) >= 3:
        family, scene = parts[0].strip(), parts[1].strip()
        frame = "__".join(parts[2:]).strip()
        if family and scene and frame:
            return family, scene, frame
    return "", "", ""


# ---------------------------------------------------------------------------
# sidecar metadata（CSV / JSON）
# ---------------------------------------------------------------------------


class MetadataIndex:
    def __init__(self) -> None:
        self._map: dict[str, tuple[str, str, str, str]] = {}
        self.ambiguous: set[str] = set()
        self.rows = 0

    def add(self, key: Any, family: str, scene: str, frame: str, source: str) -> None:
        norm = norm_key(key)
        if not norm:
            return
        value = (family.strip(), scene.strip(), frame.strip(), source)
        old = self._map.get(norm)
        if old is None:
            self._map[norm] = value
        elif old[:3] != value[:3]:
            self.ambiguous.add(norm)
        self.rows += 1

    def add_variants(self, key: Any, family: str, scene: str, frame: str, source: str) -> None:
        norm = norm_key(key)
        if not norm:
            return
        self.add(norm, family, scene, frame, source)
        base = norm.rsplit("/", 1)[-1]
        self.add(base, family, scene, frame, source)
        stem = base.rsplit(".", 1)[0] if "." in base else base
        self.add(stem, family, scene, frame, source)

    def lookup(self, candidates: Sequence[str]) -> tuple[str, str, str, str] | None:
        for candidate in candidates:
            norm = norm_key(candidate)
            if not norm:
                continue
            if norm in self.ambiguous:
                return None
            if norm in self._map:
                return self._map[norm]
        return None


def _first(record: dict[str, Any], keys: Sequence[str]) -> Any:
    lowered = {str(k).strip().lower(): v for k, v in record.items()}
    for key in keys:
        if key in lowered and lowered[key] not in (None, ""):
            return lowered[key]
    return ""


IMAGE_KEYS = (
    "image",
    "image_path",
    "image_name",
    "image_file",
    "file",
    "filepath",
    "file_path",
    "file_name",
    "filename",
    "path",
    "rel_path",
    "relative_path",
    "img",
    "img_path",
)
FAMILY_KEYS = ("family_id", "familyid", "family", "family_key", "family_no", "family_num", "household_id", "household")
SCENE_KEYS = ("scene_id", "sceneid", "scene", "scene_key", "scene_name", "room", "room_id", "room_name")
FRAME_KEYS = ("frame", "frame_id", "frameid", "frame_index", "frame_no", "frame_num", "shot_id")


def _record_values(record: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(_first(record, IMAGE_KEYS) or "").strip(),
        str(_first(record, FAMILY_KEYS) or "").strip(),
        str(_first(record, SCENE_KEYS) or "").strip(),
        str(_first(record, FRAME_KEYS) or "").strip(),
    )


def _iter_json_records(obj: Any) -> Iterable[tuple[dict[str, Any], str]]:
    """产出 (record, fallback_image_key)。"""
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                yield item, ""
        return
    if not isinstance(obj, dict):
        return
    for key in ("records", "images", "items"):
        value = obj.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield item, ""
            return
        if isinstance(value, dict):
            # {"images": {"H01/a.jpg": {"family_id": ...}, ...}}
            for image_key, item in value.items():
                if isinstance(item, dict):
                    record = dict(item)
                    record.setdefault("image", image_key)
                    yield record, ""
            return
    # 单张图的 sidecar：{"family_id": ..., "scene": ...}
    if any(k in obj for k in IMAGE_KEYS) or any(k in obj for k in FAMILY_KEYS) or any(k in obj for k in SCENE_KEYS):
        yield obj, ""
        return
    # { "H01/a.jpg": {"family_id": ...}, ... }
    for key, value in obj.items():
        if isinstance(value, dict):
            item = dict(value)
            item.setdefault("image", key)
            yield item, ""


def _sidecar_key_from_path(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = path.name
    for prefix in ("sidecar/", "metadata/"):
        if rel.startswith(prefix):
            rel = rel[len(prefix):]
            break
    if rel.lower().endswith(".json"):
        rel = rel[:-5]
    return rel


def load_metadata_files(files: Sequence[Path], root: Path, index: MetadataIndex, report: Report) -> None:
    for path in files:
        suffix = path.suffix.lower()
        try:
            if suffix == ".csv":
                _load_metadata_csv(path, index, report)
            elif suffix == ".json":
                _load_metadata_json(path, root, index, report)
            else:
                report.warn("META_IGNORED", f"忽略不支持的 metadata 文件：{path}")
                continue
            report.metadata_files.append(str(path))
        except OSError as exc:
            report.error("META_READ", f"读取 metadata 失败：{exc}", str(path))
        except Exception as exc:  # 坏 JSON / 坏 CSV 不应让整个检查崩掉
            report.error("META_PARSE", f"解析 metadata 失败：{exc}", str(path))


def _load_metadata_csv(path: Path, index: MetadataIndex, report: Report) -> None:
    text = read_text_fallback(path)
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        report.error("META_CSV_HEADER", "CSV 没有表头", str(path))
        return
    columns = {str(name).strip().lower(): name for name in reader.fieldnames if name}
    image_col = next((columns[k] for k in IMAGE_KEYS if k in columns), None)
    family_col = next((columns[k] for k in FAMILY_KEYS if k in columns), None)
    scene_col = next((columns[k] for k in SCENE_KEYS if k in columns), None)
    frame_col = next((columns[k] for k in FRAME_KEYS if k in columns), None)
    if image_col is None:
        report.error(
            "META_CSV_IMAGE_COLUMN",
            "CSV 缺少图片列；需要 image/file/path/filename 之一",
            str(path),
        )
        return
    if family_col is None:
        report.warn("META_CSV_FAMILY_COLUMN", "CSV 没有 family_id/family 列，只能靠文件名补 family", str(path))
    if scene_col is None:
        report.warn("META_CSV_SCENE_COLUMN", "CSV 没有 scene_id/scene 列，只能靠文件名补 scene", str(path))
    for row in reader:
        image = str(row.get(image_col) or "").strip()
        if not image:
            continue
        family = str(row.get(family_col) or "").strip() if family_col else ""
        scene = str(row.get(scene_col) or "").strip() if scene_col else ""
        frame = str(row.get(frame_col) or "").strip() if frame_col else ""
        index.add_variants(image, family, scene, frame, "metadata")


def _load_metadata_json(path: Path, root: Path, index: MetadataIndex, report: Report) -> None:
    text = read_text_fallback(path)
    data = json.loads(text)
    fallback = _sidecar_key_from_path(path, root)
    count = 0
    for record, record_fallback in _iter_json_records(data):
        image, family, scene, frame = _record_values(record)
        if not image:
            image = record_fallback or fallback
        if not image:
            continue
        index.add_variants(image, family, scene, frame, "metadata")
        count += 1
    if count == 0:
        report.warn("META_JSON_EMPTY", "JSON 中没有可识别的记录（需要 image + family_id/scene_id）", str(path))


def discover_metadata(root: Path, explicit: str | None) -> tuple[list[Path], list[str]]:
    errors: list[str] = []
    files: list[Path] = []
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            return [], [f"--meta 指定的路径不存在：{path}"]
        if path.is_dir():
            for p in sorted(path.rglob("*")):
                if p.is_file() and p.suffix.lower() in (".csv", ".json"):
                    files.append(p)
        else:
            files.append(path)
    else:
        for name in ("metadata.csv", "metadata.json", "meta.csv", "meta.json", "sidecar.csv", "sidecar.json"):
            candidate = root / name
            if candidate.is_file():
                files.append(candidate)
        for dirname in ("metadata", "sidecar"):
            directory = root / dirname
            if directory.is_dir():
                for p in sorted(directory.rglob("*")):
                    if p.is_file() and p.suffix.lower() in (".csv", ".json"):
                        files.append(p)
    # 去重且保序
    seen: set[str] = set()
    unique: list[Path] = []
    for path in files:
        key = str(path.resolve()) if path.exists() else str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique, errors


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(read_text_fallback(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _adjacent_sidecar_values(
    image_path: Path,
    root: Path,
    cache: dict[str, tuple[str, str, str] | None],
    report: Report,
) -> tuple[str, str, str] | None:
    rel = image_path.relative_to(root)
    candidates = [
        image_path.with_name(image_path.name + ".json"),
        image_path.with_suffix(".json"),
        root / "sidecar" / rel.with_suffix(rel.suffix + ".json"),
        root / "sidecar" / rel.with_suffix(".json"),
        root / "metadata" / rel.with_suffix(rel.suffix + ".json"),
        root / "metadata" / rel.with_suffix(".json"),
    ]
    for candidate in candidates:
        key = str(candidate)
        if key in cache:
            if cache[key] is not None:
                return cache[key]
            continue
        if not candidate.is_file():
            cache[key] = None
            continue
        data = _read_json_object(candidate)
        if data is None:
            report.warn("SIDECAR_JSON_BAD", "sidecar 不是合法 JSON 对象，已跳过", str(candidate))
            cache[key] = None
            continue
        image, family, scene, frame = _record_values(data)
        # 单图 sidecar 不一定写 image 字段；这不是错误。
        value = (family, scene, frame)
        cache[key] = value
        return value
    return None


# ---------------------------------------------------------------------------
# 标签解析
# ---------------------------------------------------------------------------


def _parse_label(
    record: ImageRecord,
    num_classes: int,
    report: Report,
) -> None:
    path = record.label_path
    try:
        text = read_text(path)
    except UnicodeDecodeError as exc:
        report.error("LABEL_ENCODING", f"标签不是 UTF-8：{exc}", str(path))
        return
    except OSError as exc:
        report.error("LABEL_READ", f"读取标签失败：{exc}", str(path))
        return

    boxes = 0
    saw_content = False
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        saw_content = True
        parts = line.split()
        if len(parts) != 5:
            report.error(
                "LABEL_FIELD_COUNT",
                f"第 {line_no} 行字段数 {len(parts)}，YOLO 检测标签应为 5 个（class cx cy w h）",
                str(path),
            )
            continue
        try:
            class_id = int(parts[0])
        except ValueError:
            report.error("LABEL_CLASS_INT", f"第 {line_no} 行 class id 不是整数：{parts[0]!r}", str(path))
            continue
        try:
            values = [float(x) for x in parts[1:]]
        except ValueError:
            report.error("LABEL_COORD_FLOAT", f"第 {line_no} 行坐标不是数字：{parts[1:]!r}", str(path))
            continue
        if not all(math.isfinite(v) for v in values):
            report.error("LABEL_COORD_NAN", f"第 {line_no} 行坐标含 NaN/Inf", str(path))
            continue
        if class_id < 0 or class_id >= num_classes:
            report.error(
                "LABEL_CLASS_RANGE",
                f"第 {line_no} 行 class id={class_id}，但当前只有 0..{num_classes - 1}",
                str(path),
            )
            continue
        cx, cy, width, height = values
        if any(v < -EPS or v > 1.0 + EPS for v in values):
            report.error(
                "LABEL_COORD_RANGE",
                f"第 {line_no} 行坐标不在 0..1：{parts[1:]}",
                str(path),
            )
            continue
        if width <= 0.0 or height <= 0.0:
            report.error(
                "LABEL_BOX_NONPOSITIVE",
                f"第 {line_no} 行 w/h 必须 > 0，实际 w={width}, h={height}",
                str(path),
            )
            continue
        if (
            cx - width / 2.0 < -EPS
            or cx + width / 2.0 > 1.0 + EPS
            or cy - height / 2.0 < -EPS
            or cy + height / 2.0 > 1.0 + EPS
        ):
            report.error(
                "LABEL_BOX_OUTSIDE",
                f"第 {line_no} 行框超出画面：cx={cx}, cy={cy}, w={width}, h={height}",
                str(path),
            )
            continue
        boxes += 1
        record.class_counts[class_id] += 1

    record.boxes = boxes
    record.empty_label = not saw_content
    if not saw_content:
        report.info("EMPTY_LABEL", "空标签（纯负例）", str(path))


# ---------------------------------------------------------------------------
# 检查主流程
# ---------------------------------------------------------------------------


def _add_structure_issues(root: Path, report: Report) -> None:
    for kind in ("images", "labels"):
        base = root / kind
        if not base.is_dir():
            report.error("MISSING_DIR", f"缺少目录：{kind}/", str(base))
    for split in SPLITS:
        for kind in ("images", "labels"):
            path = root / kind / split
            if not path.is_dir():
                report.missing_dirs.append(str(path))
                report.error("MISSING_DIR", f"缺少目录：{kind}/{split}/", str(path))
    for kind in ("images", "labels"):
        base = root / kind
        if base.is_dir():
            extras = sorted(p.name for p in base.iterdir() if p.is_dir() and p.name not in SPLITS and not p.name.startswith("."))
            for name in extras:
                report.warn("UNEXPECTED_SPLIT_DIR", f"{kind}/{name}/ 不在 train|val|test 内", str(base / name))


def _discover(root: Path, report: Report) -> None:
    for split in SPLITS:
        image_dir = root / "images" / split
        label_dir = root / "labels" / split
        if not image_dir.is_dir():
            continue
        for path in iter_image_files(image_dir):
            rel_in_split = path.relative_to(image_dir)
            rel = f"images/{split}/{rel_in_split.as_posix()}"
            label_path = label_dir / rel_in_split.with_suffix(".txt")
            report.images.append(
                ImageRecord(
                    split=split,
                    path=path,
                    rel=rel,
                    label_path=label_path,
                )
            )


def _resolve_ids(root: Path, report: Report, index: MetadataIndex) -> None:
    cache: dict[str, tuple[str, str, str] | None] = {}
    for record in report.images:
        family = scene = frame = ""
        source = "unknown"
        hit = index.lookup([record.rel, record.rel.split("images/", 1)[-1], record.path.name, record.path.stem])
        if hit is not None:
            family, scene, frame, _src = hit
            if family or scene or frame:
                source = "metadata"
        if not family or not scene:
            sidecar = _adjacent_sidecar_values(record.path, root, cache, report)
            if sidecar is not None:
                side_family, side_scene, side_frame = sidecar
                family = family or side_family
                scene = scene or side_scene
                frame = frame or side_frame
                if family or scene:
                    source = "metadata"
        parsed_family, parsed_scene, parsed_frame = parse_filename(record.path.stem)
        if parsed_family and not family:
            family = parsed_family
            source = "filename" if source == "unknown" else "metadata+filename"
        if parsed_scene and not scene:
            scene = parsed_scene
            source = "filename" if source == "unknown" else "metadata+filename"
        if parsed_frame and not frame:
            frame = parsed_frame
        record.family = family.strip()
        record.scene = scene.strip()
        record.frame = frame.strip() or record.path.stem
        record.id_source = source


def _add_alignment_issues(root: Path, report: Report) -> None:
    # 图片缺标签
    for record in report.images:
        if record.label_path.is_file():
            record.label_exists = True
        else:
            report.error("MISSING_LABEL", "图片缺少同名 .txt 标签", str(record.path))
    # 孤儿标签
    for split in SPLITS:
        label_dir = root / "labels" / split
        if not label_dir.is_dir():
            continue
        image_dir = root / "images" / split
        image_stems: set[str] = set()
        if image_dir.is_dir():
            for path in iter_image_files(image_dir):
                image_stems.add(path.relative_to(image_dir).with_suffix("").as_posix().casefold())
        for path in sorted(label_dir.rglob("*.txt")):
            if not path.is_file():
                continue
            rel = path.relative_to(label_dir)
            if any(part.startswith(".") for part in rel.parts):
                continue
            stem = rel.with_suffix("").as_posix().casefold()
            if stem not in image_stems:
                report.error("ORPHAN_LABEL", "标签没有对应图片", str(path))


def _add_duplicate_issues(report: Report) -> None:
    image_groups: dict[str, list[ImageRecord]] = defaultdict(list)
    label_groups: dict[str, list[ImageRecord]] = defaultdict(list)
    for record in report.images:
        if record.sha256:
            image_groups[record.sha256].append(record)
        if record.label_exists and record.label_sha256:
            label_groups[record.label_sha256].append(record)
    for digest, records in sorted(image_groups.items()):
        if len(records) < 2:
            continue
        splits = sorted({r.split for r in records})
        report.duplicates.append(
            {
                "kind": "image",
                "sha256": digest,
                "splits": splits,
                "images": [r.rel for r in records],
            }
        )
        report.error(
            "DUPLICATE_IMAGE",
            f"重复图片 sha256={digest[:12]}… 出现 {len(records)} 次，split={','.join(splits)}",
            records[0].rel,
        )
    for digest, records in sorted(label_groups.items()):
        # 空标签内容必然相同，不据此判重；只对非空标签提示。
        records = [r for r in records if not r.empty_label]
        if len(records) < 2:
            continue
        splits = sorted({r.split for r in records})
        report.duplicates.append(
            {
                "kind": "label",
                "sha256": digest,
                "splits": splits,
                "labels": [r.label_path.relative_to(report.root).as_posix() for r in records],
            }
        )
        report.info(
            "DUPLICATE_LABEL",
            f"标签内容重复 sha256={digest[:12]}… 出现 {len(records)} 次，split={','.join(splits)}；"
            "相同框坐标可能是正常样本，仅提示复核",
            records[0].label_path.relative_to(report.root).as_posix(),
        )


def _add_leakage_issues(report: Report) -> None:
    family_splits: dict[str, set[str]] = defaultdict(set)
    pair_splits: dict[tuple[str, str], set[str]] = defaultdict(set)
    frame_splits: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    scene_families: dict[str, set[str]] = defaultdict(set)
    family_display: dict[str, str] = {}
    scene_display: dict[str, str] = {}
    unknown_family: list[ImageRecord] = []
    unknown_scene: list[ImageRecord] = []

    for record in report.images:
        family = record.family.strip()
        scene = record.scene.strip()
        if not family:
            unknown_family.append(record)
        if not scene:
            unknown_scene.append(record)
        if not family:
            continue
        fk = family.casefold()
        family_display.setdefault(fk, family)
        family_splits[fk].add(record.split)
        if not scene:
            continue
        sk = scene.casefold()
        scene_display.setdefault(sk, scene)
        pair_splits[(fk, sk)].add(record.split)
        scene_families[sk].add(fk)
        frame = (record.frame or record.path.stem).casefold()
        frame_splits[(fk, sk, frame)].add(record.split)

    if unknown_family:
        report.error(
            "FAMILY_ID_MISSING",
            f"{len(unknown_family)}/{len(report.images)} 张图片缺少 family_id；无法验证家庭隔离。"
            "请提供 sidecar metadata 或使用 family__scene__frame.ext 命名。",
            unknown_family[0].rel,
        )
    if unknown_scene:
        report.error(
            "SCENE_ID_MISSING",
            f"{len(unknown_scene)}/{len(report.images)} 张图片缺少 scene_id；无法验证场景隔离。"
            "请提供 sidecar metadata 或使用 family__scene__frame.ext 命名。",
            unknown_scene[0].rel,
        )

    for fk, splits in sorted(family_splits.items()):
        if len(splits) > 1:
            report.error(
                "FAMILY_LEAK",
                f"family_id={family_display.get(fk, fk)} 同时出现在 {', '.join(sorted(splits))}",
            )
    for (fk, sk), splits in sorted(pair_splits.items()):
        if len(splits) > 1:
            report.error(
                "SCENE_LEAK",
                f"family_id={family_display.get(fk, fk)} + scene_id={scene_display.get(sk, sk)} "
                f"同时出现在 {', '.join(sorted(splits))}",
            )
    for (fk, sk, frame), splits in sorted(frame_splits.items()):
        if len(splits) > 1:
            report.error(
                "FRAME_LEAK",
                f"family_id={family_display.get(fk, fk)} + scene_id={scene_display.get(sk, sk)} "
                f"+ frame={frame} 同时出现在 {', '.join(sorted(splits))}",
            )
    reused = {sk: fams for sk, fams in scene_families.items() if len(fams) > 1}
    if reused:
        report.info(
            "SCENE_ID_REUSED",
            f"{len(reused)} 个 scene_id 在多个家庭复用（例如同名房间）；"
            "场景泄漏按 (family_id, scene_id) 组合检查，不把同名房间当泄漏。",
        )


def _add_quality_issues(report: Report) -> None:
    stats = report.split_stats()
    labeled = [r for r in report.images if r.label_exists]
    empty = [r for r in labeled if r.empty_label]
    total_boxes = sum(r.boxes for r in report.images)

    if stats["train"]["images"] == 0:
        report.error("EMPTY_SPLIT", "images/train 没有图片")
    if stats["val"]["images"] == 0:
        report.error("EMPTY_SPLIT", "images/val 没有图片")
    if stats["test"]["images"] == 0:
        report.warn("EMPTY_SPLIT", "images/test 没有图片；最终测试指标无法产生")

    if labeled and len(empty) == len(labeled):
        report.error("NO_BOXES", "所有标签都是空的，没有可训练的检测正例")
    elif total_boxes == 0:
        report.error("NO_BOXES", "没有任何有效检测框，无法训练检测模型")
    if labeled and empty and len(empty) / len(labeled) > 0.30:
        report.warn(
            "EMPTY_LABEL_RATIO",
            f"空标签 {len(empty)}/{len(labeled)} = {len(empty) / len(labeled):.1%} > 30%；"
            "纯负例过多，需补正例",
        )
    for class_id, name in enumerate(report.classes):
        count = sum(r.class_counts.get(class_id, 0) for r in report.images)
        if count == 0:
            report.warn("CLASS_NO_BOXES", f"类别 {name}（id={class_id}）没有任何正例框")

    test_records = report.split_images("test")
    if test_records:
        test_families = {r.family for r in test_records if r.family}
        if len(test_families) < 2:
            report.warn(
                "TEST_FAMILIES_LT_2",
                f"test 只有 {len(test_families)} 个家庭；测试统计意义不足，建议至少 2 个",
            )
        if len(test_records) < 30:
            report.warn("TEST_IMAGES_LT_30", f"test 只有 {len(test_records)} 张图；建议至少 30 张")
        for class_id, name in enumerate(report.classes):
            count = sum(r.class_counts.get(class_id, 0) for r in test_records)
            if count < 10:
                report.warn(
                    "TEST_CLASS_LT_10",
                    f"test 中类别 {name} 正例只有 {count} 个；该类指标只能标“样本不足”",
                )


def _collect_hashes(report: Report) -> None:
    for record in report.images:
        try:
            record.sha256 = sha256_file(record.path)
        except OSError as exc:
            report.error("HASH_READ", f"计算图片 sha256 失败：{exc}", str(record.path))
        if record.label_exists:
            try:
                record.label_sha256 = sha256_file(record.label_path)
            except OSError as exc:
                report.warn("LABEL_HASH_READ", f"计算标签 sha256 失败：{exc}", str(record.label_path))


def run(args: argparse.Namespace, classes: Sequence[str]) -> int:
    root = Path(args.dataset).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    try:
        root = root.resolve()
    except OSError as exc:
        print(f"ERROR: 无法解析数据集路径：{exc}", file=sys.stderr)
        return EXIT_FAIL

    report = Report(root, classes)
    if not root.exists():
        report.error("DATASET_MISSING", f"数据集目录不存在：{root}")
        report.status = "NO_DATA"
        report.exit_code = EXIT_NO_DATA
        _finish(report, args)
        return report.exit_code
    if not root.is_dir():
        report.error("DATASET_NOT_DIR", f"数据集路径不是目录：{root}")
        report.status = "FAIL"
        report.exit_code = EXIT_FAIL
        _finish(report, args)
        return report.exit_code

    metadata_files, meta_errors = discover_metadata(root, args.meta)
    for message in meta_errors:
        report.error("META_PATH", message)
    index = MetadataIndex()
    if metadata_files:
        load_metadata_files(metadata_files, root, index, report)

    _add_structure_issues(root, report)
    _discover(root, report)

    if not report.images:
        report.status = "NO_DATA"
        report.exit_code = EXIT_NO_DATA
        if report.missing_dirs:
            report.info(
                "NO_DATA_HINT",
                "没有找到任何图片；请检查 images/train|val|test 是否存在并放入 .jpg/.png 等文件",
            )
        _finish(report, args)
        return report.exit_code

    _resolve_ids(root, report, index)
    _add_alignment_issues(root, report)
    _collect_hashes(report)
    for record in report.images:
        if record.label_exists:
            _parse_label(record, len(classes), report)
    _add_duplicate_issues(report)
    _add_leakage_issues(report)
    _add_quality_issues(report)

    if report.errors:
        report.status = "FAIL"
        report.exit_code = EXIT_FAIL
    elif report.warnings:
        report.status = "PASS_WITH_WARNINGS"
        report.exit_code = EXIT_OK
    else:
        report.status = "PASS"
        report.exit_code = EXIT_OK

    _finish(report, args)
    return report.exit_code


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _print_issue_group(title: str, issues: Sequence[Issue], limit: int) -> None:
    print(f"\n[{title}] {len(issues)}")
    if not issues:
        print("  (无)")
        return
    shown = issues[: max(1, limit)]
    for issue in shown:
        print(f"  {issue.level:<5} {issue.code}: {issue.message}")
        if issue.path:
            print(f"        {issue.path}")
    if len(issues) > len(shown):
        print(f"  ... 另有 {len(issues) - len(shown)} 条，见 --report 输出的 JSON")


def print_report(report: Report, limit: int) -> None:
    stats = report.split_stats()
    print("=" * 78)
    print("YOLO 数据集检查报告")
    print("=" * 78)
    print(f"dataset : {report.root}")
    print(f"classes : {len(report.classes)} -> {', '.join(report.classes)}")
    if report.metadata_files:
        print("metadata: " + ", ".join(report.metadata_files))
    else:
        print("metadata: 未找到 sidecar CSV/JSON，仅尝试 family__scene__frame.ext 文件名")

    print("\n[目录与划分]")
    print(f"  {'split':<6} {'images':>7} {'labels':>7} {'empty':>7} {'boxes':>7} {'families':>9} {'scenes':>7}")
    for split in SPLITS:
        s = stats[split]
        print(
            f"  {split:<6} {s['images']:>7} {s['labels']:>7} {s['empty_labels']:>7} "
            f"{s['boxes']:>7} {s['families']:>9} {s['scenes']:>7}"
        )
    print(f"  {'total':<6} {len(report.images):>7} "
          f"{sum(1 for r in report.images if r.label_exists):>7} "
          f"{sum(1 for r in report.images if r.empty_label):>7} "
          f"{sum(r.boxes for r in report.images):>7}")

    print("\n[类别框数]")
    for name, count in report.class_counts().items():
        print(f"  {name:<18} {count:>7}")

    if report.duplicates:
        print("\n[重复文件 sha256]")
        for group in report.duplicates:
            kind = group.get("kind", "image")
            paths = group.get("images") or group.get("labels") or []
            print(f"  {kind:<5} {group['sha256'][:16]}…  splits={','.join(group['splits'])}  n={len(paths)}")
            for path in paths[:3]:
                print(f"      {path}")

    _print_issue_group("错误", report.errors, limit)
    _print_issue_group("警告", report.warnings, limit)
    infos = [x for x in report.issues if x.level == "INFO"]
    _print_issue_group("提示", infos, limit)

    print("\n" + "-" * 78)
    if report.status == "NO_DATA":
        print("RESULT: NO_DATA —— 没有找到任何图片，不能判定通过（退出码 2）")
    elif report.status == "FAIL":
        print(f"RESULT: FAIL —— {len(report.errors)} 个错误，{len(report.warnings)} 个警告（退出码 1）")
    elif report.status == "PASS_WITH_WARNINGS":
        print(f"RESULT: PASS_WITH_WARNINGS —— 0 个错误，{len(report.warnings)} 个警告（退出码 0）")
    else:
        print("RESULT: PASS —— 0 个错误，0 个警告（退出码 0）")
    print("提示：本检查只读文件树/标签文本/哈希，不读取图片像素，也不证明模型精度。")


def _write_json_report(report: Report, raw_path: str) -> bool:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: 写 JSON 报告失败：{exc}", file=sys.stderr)
        return False
    print(f"\nJSON 报告: {path}")
    return True


def _finish(report: Report, args: argparse.Namespace) -> None:
    if args.report:
        if not _write_json_report(report, args.report):
            report.error("REPORT_WRITE", "写 JSON 报告失败", args.report)
            if report.exit_code == EXIT_OK:
                report.exit_code = EXIT_FAIL
                report.status = "FAIL"
    print_report(report, args.max_list)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_list < 1:
        parser.error("--max-list 必须 >= 1")
    if not str(args.dataset).strip():
        parser.error("--dataset 不能为空")
    if args.meta is not None and not str(args.meta).strip():
        parser.error("--meta 不能为空字符串")
    if args.report is not None and not str(args.report).strip():
        parser.error("--report 不能为空字符串")
    classes = parse_classes(args.classes, parser)
    return run(args, classes)


if __name__ == "__main__":
    sys.exit(main())
