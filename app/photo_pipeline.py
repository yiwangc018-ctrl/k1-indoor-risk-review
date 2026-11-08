#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PC 侧照片处理最小链路：读取 -> 画质门禁 -> 预处理 -> 去重 -> 裁剪 -> JSON。

    python app/photo_pipeline.py 照片1.jpg 照片2.jpg --out .\\tmp_photos

设计边界
--------
* 只使用标准库 + Pillow + numpy；不依赖 cv2 / ultralytics / PyYAML，不联网。
* 不实现 YOLO、检测、分割、风险识别；本文件只处理"一张图能不能用、怎么留证据"。
* 清晰度只使用 numpy 梯度方差代理，**不是 Laplacian**；它只能区分"有/没有纹理"，
  不能替代对焦检测或模型指标。
* 原图只读：所有输出写到 output_dir，文件名带 ``_processed``，绝不覆盖源文件。
* 缺 Pillow / numpy、坏图、文件不存在都抛 ``PhotoPipelineError`` 子类；
  ``run_file_pipeline`` 会把它们变成逐条的 error 结果，不中断其它图片。

可替换点
--------
``ImageLoader`` / ``QualityChecker`` / ``Preprocessor`` / ``AverageHasher`` 四个
Protocol 描述了各阶段的最小接口；``run_file_pipeline`` 接受对应参数，测试里可以
注入假实现，不需要改主流程。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import numbers
import os
import tempfile
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, List, Mapping, Optional, Protocol, Tuple, runtime_checkable

# 质量门禁 / 流水线状态。QualityResult 只允许前三个；PipelineResult 还允许
# duplicate（本次运行内与前面的图重复）和 error（读图/处理失败）。
PASS = "pass"
RETRY = "retry"
REJECT = "reject"
DUPLICATE = "duplicate"
ERROR = "error"

__all__ = [
    "PASS",
    "RETRY",
    "REJECT",
    "DUPLICATE",
    "ERROR",
    "PhotoPipelineError",
    "DependencyError",
    "ImageLoadError",
    "QualityConfigError",
    "PreprocessError",
    "CropError",
    "FrameMeta",
    "QualityThresholds",
    "QualityResult",
    "CropRecord",
    "LoadedImage",
    "PipelineResult",
    "ImageLoader",
    "QualityChecker",
    "Preprocessor",
    "AverageHasher",
    "load_image",
    "quality_check",
    "preprocess",
    "average_hash",
    "hamming_distance",
    "is_duplicate",
    "crop_box",
    "run_file_pipeline",
    "results_to_json",
]

SUPPORTED_FORMATS = ("JPEG", "PNG")

_EXT_TO_FORMAT = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".jpe": "JPEG",
    ".png": "PNG",
}


# ---------------------------------------------------------------------------
# 异常与可选依赖
# ---------------------------------------------------------------------------
class PhotoPipelineError(Exception):
    """照片处理链路的可预期错误基类。"""


class DependencyError(PhotoPipelineError):
    """Pillow / numpy 缺失。"""


class ImageLoadError(PhotoPipelineError):
    """文件不存在、不是普通文件、坏图或无法解码。"""


class QualityConfigError(PhotoPipelineError, ValueError):
    """画质阈值配置非法。"""


class PreprocessError(PhotoPipelineError, ValueError):
    """预处理参数或写盘失败。"""


class CropError(PhotoPipelineError, ValueError):
    """裁剪框非法或与图像无交集。"""


def _import_pil():
    """延迟导入 Pillow，缺依赖时给可读错误而不是 ImportError 栈。"""
    try:
        from PIL import Image  # noqa: WPS433 (局部导入是有意的)
    except ImportError as exc:  # pragma: no cover - 本机已知装了 Pillow
        raise DependencyError(
            "缺少 Pillow (PIL)：照片链路需要 Pillow + numpy；"
            "请先安装 Pillow（pip install Pillow），不需要 cv2/ultralytics。"
        ) from exc
    return Image


def _import_numpy():
    """延迟导入 numpy，缺依赖时给可读错误。"""
    try:
        import numpy  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover - 本机已知装了 numpy
        raise DependencyError(
            "缺少 numpy：画质检查需要 numpy 计算亮度/过曝/梯度方差；"
            "请先安装 numpy（pip install numpy）。"
        ) from exc
    return numpy


def _resample(Image, name):
    """兼容 Pillow 新旧版本的 Resampling 枚举。"""
    enum = getattr(Image, "Resampling", None)
    if enum is not None:
        return getattr(enum, name)
    return getattr(Image, name)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FrameMeta:
    """一帧图片的来源与处理元数据。

    ``width`` / ``height`` 是"当前帧"的尺寸；``source_width`` / ``source_height``
    是最初文件解码后的尺寸。``original_sha256`` 永远来自源文件字节，
    ``processed_sha256`` 来自实际写出的输出文件字节。
    """

    source_path: str
    source_width: int
    source_height: int
    width: int
    height: int
    source_mode: str
    source_format: str
    mode: str
    format: str
    original_sha256: str
    source_file_size: int
    processed_sha256: Optional[str] = None
    rotation: float = 0.0
    scale: float = 1.0
    output_path: Optional[str] = None
    output_file_size: int = 0

    @property
    def size(self) -> Tuple[int, int]:
        return (self.width, self.height)

    def to_dict(self) -> dict:
        return {
            "source_path": self.source_path,
            "source_width": self.source_width,
            "source_height": self.source_height,
            "width": self.width,
            "height": self.height,
            "source_mode": self.source_mode,
            "source_format": self.source_format,
            "mode": self.mode,
            "format": self.format,
            "original_sha256": self.original_sha256,
            "processed_sha256": self.processed_sha256,
            "source_file_size": self.source_file_size,
            "rotation": self.rotation,
            "scale": self.scale,
            "output_path": self.output_path,
            "output_file_size": self.output_file_size,
        }


@dataclass(frozen=True)
class QualityThresholds:
    """画质门禁阈值，全部可配置，且不依赖 YAML。

    pass / retry / reject 的判定：
      * reject：低于硬下限/高于硬上限（尺寸、亮度、过曝、梯度方差）；
      * retry：在目标阈值与硬边界之间，可以重拍；
      * pass：全部落在目标范围内。

    尺寸同时有 ``min_*``（硬下限）和 ``recommended_*``（建议值）：
    小于硬下限 -> reject；小于建议值但大于等于硬下限 -> retry。
    """

    min_width: int = 320
    min_height: int = 240
    recommended_width: int = 640
    recommended_height: int = 480
    brightness_min: float = 40.0
    brightness_max: float = 220.0
    brightness_retry_margin: float = 20.0
    overexposed_pixel_value: int = 250
    max_overexposed_ratio: float = 0.15
    overexposed_retry_margin: float = 0.10
    min_gradient_variance: float = 80.0
    gradient_retry_margin: float = 40.0

    _INT_FIELDS = (
        "min_width",
        "min_height",
        "recommended_width",
        "recommended_height",
        "overexposed_pixel_value",
    )
    _FLOAT_FIELDS = (
        "brightness_min",
        "brightness_max",
        "brightness_retry_margin",
        "max_overexposed_ratio",
        "overexposed_retry_margin",
        "min_gradient_variance",
        "gradient_retry_margin",
    )

    def __post_init__(self):
        self._validate()
        # 归一化成内置 int/float，保证 to_dict()/json.dumps 不依赖 numpy 标量。
        for name in self._INT_FIELDS:
            object.__setattr__(self, name, int(getattr(self, name)))
        for name in self._FLOAT_FIELDS:
            object.__setattr__(self, name, float(getattr(self, name)))

    @classmethod
    def field_names(cls) -> Tuple[str, ...]:
        return tuple(f.name for f in fields(cls))

    def _validate(self):
        for name in self._INT_FIELDS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, numbers.Integral):
                raise QualityConfigError("%s 必须是整数，得到 %r" % (name, value))
        for name in self._FLOAT_FIELDS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, numbers.Real):
                raise QualityConfigError("%s 必须是数字，得到 %r" % (name, value))
            if not math.isfinite(float(value)):
                raise QualityConfigError("%s 必须是有限数字，得到 %r" % (name, value))

        if self.min_width < 1 or self.min_height < 1:
            raise QualityConfigError("min_width/min_height 必须 >= 1")
        if self.recommended_width < self.min_width:
            raise QualityConfigError("recommended_width 不能小于 min_width")
        if self.recommended_height < self.min_height:
            raise QualityConfigError("recommended_height 不能小于 min_height")
        if not (0.0 <= self.brightness_min <= self.brightness_max <= 255.0):
            raise QualityConfigError("亮度阈值必须满足 0 <= min <= max <= 255")
        if self.brightness_retry_margin < 0:
            raise QualityConfigError("brightness_retry_margin 必须 >= 0")
        if not (0 <= self.overexposed_pixel_value <= 255):
            raise QualityConfigError("overexposed_pixel_value 必须在 0..255")
        if not (0.0 <= self.max_overexposed_ratio <= 1.0):
            raise QualityConfigError("max_overexposed_ratio 必须在 0..1")
        if self.overexposed_retry_margin < 0:
            raise QualityConfigError("overexposed_retry_margin 必须 >= 0")
        if self.max_overexposed_ratio + self.overexposed_retry_margin > 1.0:
            raise QualityConfigError("max_overexposed_ratio + 余量不能超过 1.0")
        if self.min_gradient_variance < 0:
            raise QualityConfigError("min_gradient_variance 必须 >= 0")
        if self.gradient_retry_margin < 0:
            raise QualityConfigError("gradient_retry_margin 必须 >= 0")

    @classmethod
    def from_mapping(cls, values) -> "QualityThresholds":
        """从 dict/JSON 风格映射构造；未知键报错，避免拼写错误静默生效。"""
        if values is None:
            return cls()
        if isinstance(values, cls):
            return values
        if not isinstance(values, Mapping):
            raise QualityConfigError(
                "thresholds 必须是 QualityThresholds 或映射，得到 %s"
                % type(values).__name__
            )
        known = set(cls.field_names())
        unknown = sorted(set(values) - known)
        if unknown:
            raise QualityConfigError("未知画质阈值字段: %s" % ", ".join(unknown))
        defaults = cls()
        data = {}
        for name in cls.field_names():
            if name not in values:
                data[name] = getattr(defaults, name)
                continue
            raw = values[name]
            if isinstance(raw, bool):
                raise QualityConfigError("%s 必须是数字，不能是 bool" % name)
            try:
                data[name] = int(raw) if name in cls._INT_FIELDS else float(raw)
            except (TypeError, ValueError) as exc:
                raise QualityConfigError("%s 的值无法转换: %r" % (name, raw)) from exc
        return cls(**data)

    def with_overrides(self, **overrides) -> "QualityThresholds":
        data = self.to_dict()
        data.update(overrides)
        return type(self).from_mapping(data)

    def to_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.field_names()}


@dataclass(frozen=True)
class QualityResult:
    """一次画质门禁的结构化结果。"""

    status: str
    metrics: Mapping[str, float]
    reasons: Tuple[str, ...] = ()
    thresholds: Optional[Mapping[str, float]] = None

    def __post_init__(self):
        if self.status not in (PASS, RETRY, REJECT):
            raise ValueError("QualityResult.status 非法: %r" % (self.status,))
        object.__setattr__(self, "metrics", dict(self.metrics))
        object.__setattr__(self, "reasons", tuple(self.reasons or ()))
        object.__setattr__(
            self, "thresholds", dict(self.thresholds) if self.thresholds else {}
        )

    @property
    def ok(self) -> bool:
        return self.status == PASS

    @property
    def needs_retry(self) -> bool:
        return self.status == RETRY

    @property
    def is_rejected(self) -> bool:
        return self.status == REJECT

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "ok": self.ok,
            "metrics": dict(self.metrics),
            "reasons": list(self.reasons),
            "thresholds": dict(self.thresholds),
        }


@dataclass(frozen=True)
class CropRecord:
    """裁剪证据：请求框、实际裁剪框、来源与输出尺寸。"""

    source_box: Tuple[int, int, int, int]
    crop_box: Tuple[int, int, int, int]
    source_size: Tuple[int, int]
    output_size: Tuple[int, int]

    def __post_init__(self):
        object.__setattr__(self, "source_box", tuple(int(v) for v in self.source_box))
        object.__setattr__(self, "crop_box", tuple(int(v) for v in self.crop_box))
        object.__setattr__(self, "source_size", tuple(int(v) for v in self.source_size))
        object.__setattr__(self, "output_size", tuple(int(v) for v in self.output_size))

    @property
    def clamped(self) -> bool:
        """请求框是否被图像边界裁剪过。"""
        return self.source_box != self.crop_box

    def to_dict(self) -> dict:
        return {
            "source_box": list(self.source_box),
            "crop_box": list(self.crop_box),
            "source_size": list(self.source_size),
            "output_size": list(self.output_size),
            "clamped": self.clamped,
        }


@dataclass
class LoadedImage:
    """解码后的图片 + 源文件元数据。"""

    image: Any
    meta: FrameMeta

    @property
    def size(self) -> Tuple[int, int]:
        return tuple(self.image.size)

    @property
    def width(self) -> int:
        return int(self.image.width)

    @property
    def height(self) -> int:
        return int(self.image.height)

    @property
    def mode(self) -> str:
        return str(self.image.mode)

    def __iter__(self):
        # 兼容 ``image, meta = load_image(path)`` 的写法。
        yield self.image
        yield self.meta

    def close(self) -> None:
        close = getattr(self.image, "close", None)
        if callable(close):
            close()


@dataclass(frozen=True)
class PipelineResult:
    """单张图片的完整流水线结果，``to_dict`` 后可直接 json.dumps。"""

    source: str
    ok: bool
    status: str
    frame: Optional[FrameMeta] = None
    quality: Optional[QualityResult] = None
    crops: Tuple[CropRecord, ...] = ()
    average_hash: Optional[str] = None
    duplicate_of: Optional[str] = None
    error: Optional[str] = None
    error_type: Optional[str] = None

    def __post_init__(self):
        if self.status not in (PASS, RETRY, REJECT, DUPLICATE, ERROR):
            raise ValueError("PipelineResult.status 非法: %r" % (self.status,))
        object.__setattr__(self, "crops", tuple(self.crops or ()))

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "ok": self.ok,
            "status": self.status,
            "frame": self.frame.to_dict() if self.frame else None,
            "quality": self.quality.to_dict() if self.quality else None,
            "crops": [crop.to_dict() for crop in self.crops],
            "average_hash": self.average_hash,
            "duplicate_of": self.duplicate_of,
            "error": self.error,
            "error_type": self.error_type,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    # 常用字段的只读快捷方式：方便不关心 frame 嵌套的调用方。
    @property
    def output_path(self) -> Optional[str]:
        return self.frame.output_path if self.frame else None

    @property
    def original_sha256(self) -> Optional[str]:
        return self.frame.original_sha256 if self.frame else None

    @property
    def processed_sha256(self) -> Optional[str]:
        return self.frame.processed_sha256 if self.frame else None

    @property
    def size(self) -> Optional[Tuple[int, int]]:
        return self.frame.size if self.frame else None

    @property
    def rotation(self) -> Optional[float]:
        return self.frame.rotation if self.frame else None

    @property
    def scale(self) -> Optional[float]:
        return self.frame.scale if self.frame else None


# ---------------------------------------------------------------------------
# 可替换协议
# ---------------------------------------------------------------------------
@runtime_checkable
class ImageLoader(Protocol):
    def __call__(self, path: str) -> LoadedImage:
        ...


@runtime_checkable
class QualityChecker(Protocol):
    def __call__(self, image: Any, thresholds: Any = None) -> QualityResult:
        ...


@runtime_checkable
class Preprocessor(Protocol):
    def __call__(
        self,
        source: Any,
        output_dir: Optional[str] = None,
        *,
        image_format: str = "JPEG",
        jpeg_quality: int = 90,
        max_side: Optional[int] = None,
        scale: Optional[float] = None,
        rotate: float = 0.0,
    ) -> FrameMeta:
        ...


@runtime_checkable
class AverageHasher(Protocol):
    def __call__(self, image: Any, hash_size: int = 8) -> str:
        ...


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def load_image(path) -> LoadedImage:
    """读取本地图片，返回 ``LoadedImage``；所有失败都转成 ``ImageLoadError``。"""
    Image = _import_pil()
    try:
        text_path = os.fsdecode(os.fspath(path))
    except TypeError as exc:
        raise ImageLoadError("路径必须是 str/PathLike，得到 %r" % (path,)) from exc

    abs_path = os.path.abspath(text_path)
    if not os.path.exists(text_path):
        raise ImageLoadError("图片不存在: %s" % text_path)
    if not os.path.isfile(text_path):
        raise ImageLoadError("不是普通文件: %s" % text_path)

    try:
        with open(text_path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise ImageLoadError("读取文件失败: %s: %s" % (text_path, exc)) from exc
    if not data:
        raise ImageLoadError("图片为空: %s" % text_path)

    digest = hashlib.sha256(data).hexdigest()
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.load()
            image = probe.copy()
            source_format = probe.format or _EXT_TO_FORMAT.get(
                os.path.splitext(text_path)[1].lower(), "UNKNOWN"
            )
            source_mode = probe.mode
    except Image.UnidentifiedImageError as exc:
        raise ImageLoadError("无法识别的图片格式或文件损坏: %s" % text_path) from exc
    except Image.DecompressionBombError as exc:
        raise ImageLoadError("图片像素数超过安全上限: %s" % text_path) from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise ImageLoadError("图片解码失败（可能已损坏）: %s: %s" % (text_path, exc)) from exc

    meta = FrameMeta(
        source_path=abs_path,
        source_width=int(image.width),
        source_height=int(image.height),
        width=int(image.width),
        height=int(image.height),
        source_mode=source_mode,
        source_format=str(source_format).upper(),
        mode=str(image.mode),
        format=str(source_format).upper(),
        original_sha256=digest,
        source_file_size=len(data),
    )
    return LoadedImage(image=image, meta=meta)


# ---------------------------------------------------------------------------
# 画质门禁
# ---------------------------------------------------------------------------
def _classify_quality(width, height, brightness, over_ratio, gradient_variance, th):
    reject_reasons: List[str] = []
    retry_reasons: List[str] = []

    if width < th.min_width:
        reject_reasons.append("reject:width_below_min(%d<%d)" % (width, th.min_width))
    elif width < th.recommended_width:
        retry_reasons.append(
            "retry:width_below_recommended(%d<%d)" % (width, th.recommended_width)
        )
    if height < th.min_height:
        reject_reasons.append("reject:height_below_min(%d<%d)" % (height, th.min_height))
    elif height < th.recommended_height:
        retry_reasons.append(
            "retry:height_below_recommended(%d<%d)" % (height, th.recommended_height)
        )

    bright_hard_min = th.brightness_min - th.brightness_retry_margin
    bright_hard_max = th.brightness_max + th.brightness_retry_margin
    if brightness < bright_hard_min:
        reject_reasons.append(
            "reject:brightness_below_hard_min(%.2f<%.2f)"
            % (brightness, bright_hard_min)
        )
    elif brightness < th.brightness_min:
        retry_reasons.append(
            "retry:brightness_below_target(%.2f<%.2f)"
            % (brightness, th.brightness_min)
        )
    elif brightness > bright_hard_max:
        reject_reasons.append(
            "reject:brightness_above_hard_max(%.2f>%.2f)"
            % (brightness, bright_hard_max)
        )
    elif brightness > th.brightness_max:
        retry_reasons.append(
            "retry:brightness_above_target(%.2f>%.2f)"
            % (brightness, th.brightness_max)
        )

    over_hard_max = th.max_overexposed_ratio + th.overexposed_retry_margin
    if over_ratio > over_hard_max:
        reject_reasons.append(
            "reject:overexposed_ratio_above_hard_max(%.4f>%.4f)"
            % (over_ratio, over_hard_max)
        )
    elif over_ratio > th.max_overexposed_ratio:
        retry_reasons.append(
            "retry:overexposed_ratio_above_target(%.4f>%.4f)"
            % (over_ratio, th.max_overexposed_ratio)
        )

    grad_hard_min = th.min_gradient_variance - th.gradient_retry_margin
    if gradient_variance < grad_hard_min:
        reject_reasons.append(
            "reject:gradient_variance_below_hard_min(%.2f<%.2f)"
            % (gradient_variance, grad_hard_min)
        )
    elif gradient_variance < th.min_gradient_variance:
        retry_reasons.append(
            "retry:gradient_variance_below_target(%.2f<%.2f)"
            % (gradient_variance, th.min_gradient_variance)
        )

    reasons = tuple(reject_reasons + retry_reasons)
    if reject_reasons:
        return REJECT, reasons
    if retry_reasons:
        return RETRY, reasons
    return PASS, reasons


def quality_check(image, thresholds=None) -> QualityResult:
    """检查尺寸、平均亮度、过曝比例、梯度方差，返回 pass/retry/reject。

    ``image`` 可以是 PIL Image、``LoadedImage``，或本地路径。
    清晰度代理是 ``var(dI/dx) + var(dI/dy)``，不是 Laplacian。
    """
    numpy = _import_numpy()
    if isinstance(image, (str, bytes, os.PathLike)):
        image = load_image(image).image
    if isinstance(image, LoadedImage):
        image = image.image

    th = QualityThresholds.from_mapping(thresholds)
    if not hasattr(image, "convert") or not hasattr(image, "size"):
        raise QualityConfigError("quality_check 需要 PIL Image 或 LoadedImage")

    gray = image.convert("L")
    width, height = gray.size
    pixels = numpy.asarray(gray, dtype=numpy.float64)
    if pixels.size == 0:
        raise QualityConfigError("图片没有像素，无法做画质检查")

    brightness = float(pixels.mean())
    over_ratio = float((pixels >= th.overexposed_pixel_value).mean())
    if width > 1 and height > 1:
        grad_y, grad_x = numpy.gradient(pixels)
        gradient_variance = float(numpy.var(grad_x) + numpy.var(grad_y))
    else:
        gradient_variance = 0.0

    status, reasons = _classify_quality(
        width, height, brightness, over_ratio, gradient_variance, th
    )
    metrics = {
        "width": int(width),
        "height": int(height),
        "mean_brightness": brightness,
        "overexposed_ratio": over_ratio,
        "gradient_variance": gradient_variance,
        "pixel_count": int(pixels.size),
    }
    return QualityResult(
        status=status,
        metrics=metrics,
        reasons=reasons,
        thresholds=th.to_dict(),
    )


# ---------------------------------------------------------------------------
# 预处理
# ---------------------------------------------------------------------------
def _prepare_for_jpeg(Image, image):
    """JPEG 不支持 alpha；透明区铺白，避免变成黑块。"""
    has_alpha = image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    )
    if has_alpha:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    if image.mode == "RGB":
        return image
    return image.convert("RGB")


def _prepare_for_png(Image, image):
    """PNG 不支持 CMYK/YCbCr 等模式，先转 RGB；其它模式原样保留。"""
    if image.mode in ("CMYK", "YCbCr", "HSV", "LAB", "F"):
        return image.convert("RGB")
    return image


def _validate_preprocess_args(image_format, jpeg_quality, max_side, scale, rotate):
    fmt = str(image_format).strip().upper()
    if fmt == "JPG":
        fmt = "JPEG"
    if fmt not in SUPPORTED_FORMATS:
        raise PreprocessError(
            "不支持的输出格式 %r；只支持 JPEG/PNG" % (image_format,)
        )
    if isinstance(jpeg_quality, bool) or not isinstance(jpeg_quality, numbers.Integral):
        raise PreprocessError("jpeg_quality 必须是 1..100 的整数")
    if not (1 <= int(jpeg_quality) <= 100):
        raise PreprocessError("jpeg_quality 必须在 1..100")
    if max_side is not None:
        if isinstance(max_side, bool) or not isinstance(max_side, numbers.Integral):
            raise PreprocessError("max_side 必须是正整数")
        if int(max_side) < 1:
            raise PreprocessError("max_side 必须 >= 1")
    if scale is not None:
        if isinstance(scale, bool) or not isinstance(scale, numbers.Real):
            raise PreprocessError("scale 必须是正数")
        if not math.isfinite(float(scale)) or float(scale) <= 0:
            raise PreprocessError("scale 必须 > 0 且有限")
    if max_side is not None and scale is not None:
        raise PreprocessError("max_side 与 scale 只能二选一，避免缩放口径不清")
    if isinstance(rotate, bool) or not isinstance(rotate, numbers.Real):
        raise PreprocessError("rotate 必须是数字（逆时针角度）")
    if not math.isfinite(float(rotate)):
        raise PreprocessError("rotate 必须有限")
    return fmt


def preprocess(
    source,
    output_dir=None,
    *,
    image_format: str = "JPEG",
    jpeg_quality: int = 90,
    max_side: Optional[int] = None,
    scale: Optional[float] = None,
    rotate: float = 0.0,
) -> FrameMeta:
    """生成新图并返回元数据，不覆盖源文件。

    处理顺序：旋转（逆时针，expand=True）-> 缩放 -> 保存。
    ``max_side`` 限制旋转后最长边；``scale`` 是显式倍率，两者不能同时给。
    ``output_dir`` 省略时使用系统临时目录。
    """
    Image = _import_pil()
    fmt = _validate_preprocess_args(image_format, jpeg_quality, max_side, scale, rotate)

    if isinstance(source, LoadedImage):
        loaded = source
    else:
        loaded = load_image(source)

    if output_dir is None:
        out_dir = tempfile.mkdtemp(prefix="photo_pipeline_")
    else:
        try:
            out_dir = os.path.abspath(os.fsdecode(os.fspath(output_dir)))
        except TypeError as exc:
            raise PreprocessError("output_dir 必须是 str/PathLike") from exc
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            raise PreprocessError("创建输出目录失败: %s: %s" % (out_dir, exc)) from exc
    if not os.path.isdir(out_dir):
        raise PreprocessError("输出路径不是目录: %s" % out_dir)

    image = loaded.image
    rotation = float(rotate)
    if rotation:
        try:
            image = image.rotate(
                rotation, expand=True, resample=_resample(Image, "BICUBIC")
            )
        except (OSError, ValueError) as exc:
            raise PreprocessError("旋转失败: %s" % exc) from exc

    actual_scale = 1.0
    if max_side is not None:
        longest = max(image.size)
        if longest > int(max_side):
            actual_scale = float(max_side) / float(longest)
    elif scale is not None:
        actual_scale = float(scale)

    if actual_scale != 1.0:
        new_size = (
            max(1, int(round(image.width * actual_scale))),
            max(1, int(round(image.height * actual_scale))),
        )
        try:
            image = image.resize(new_size, _resample(Image, "LANCZOS"))
        except (OSError, ValueError) as exc:
            raise PreprocessError("缩放失败: %s" % exc) from exc

    if fmt == "JPEG":
        image = _prepare_for_jpeg(Image, image)
        save_kwargs = {"format": "JPEG", "quality": int(jpeg_quality), "optimize": True}
        extension = ".jpg"
    else:
        image = _prepare_for_png(Image, image)
        save_kwargs = {"format": "PNG"}
        extension = ".png"

    buffer = io.BytesIO()
    try:
        image.save(buffer, **save_kwargs)
    except (OSError, ValueError) as exc:
        raise PreprocessError("编码输出图片失败: %s" % exc) from exc
    payload = buffer.getvalue()
    processed_sha = hashlib.sha256(payload).hexdigest()

    source_abs = os.path.abspath(loaded.meta.source_path)
    stem = Path(loaded.meta.source_path).stem or "image"
    base_name = os.path.join(out_dir, "%s_processed" % stem)
    candidate = base_name + extension
    counter = 1
    while os.path.exists(candidate) or os.path.normcase(
        os.path.abspath(candidate)
    ) == os.path.normcase(source_abs):
        candidate = "%s_%d%s" % (base_name, counter, extension)
        counter += 1

    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix=".photo_pipeline_", suffix=extension,
                                         dir=out_dir)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(temp_path, candidate)
        temp_path = None
    except OSError as exc:
        raise PreprocessError("写出输出图片失败: %s: %s" % (candidate, exc)) from exc
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    return FrameMeta(
        source_path=source_abs,
        source_width=loaded.meta.source_width,
        source_height=loaded.meta.source_height,
        width=int(image.width),
        height=int(image.height),
        source_mode=loaded.meta.source_mode,
        source_format=loaded.meta.source_format,
        mode=str(image.mode),
        format=fmt,
        original_sha256=loaded.meta.original_sha256,
        processed_sha256=processed_sha,
        source_file_size=loaded.meta.source_file_size,
        rotation=rotation,
        scale=round(float(actual_scale), 9),
        output_path=os.path.abspath(candidate),
        output_file_size=len(payload),
    )


# ---------------------------------------------------------------------------
# average hash 去重
# ---------------------------------------------------------------------------
def _normalise_hash(value) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("average hash 必须是非空十六进制字符串")
    text = value.lower()
    try:
        int(text, 16)
    except ValueError as exc:
        raise ValueError("average hash 含非十六进制字符: %r" % (value,)) from exc
    return text


def average_hash(image, hash_size: int = 8) -> str:
    """标准 average hash：灰度缩到 NxN，按均值二值化后输出十六进制。"""
    Image = _import_pil()
    if isinstance(image, (str, bytes, os.PathLike)):
        image = load_image(image).image
    if isinstance(image, LoadedImage):
        image = image.image
    if isinstance(hash_size, bool) or not isinstance(hash_size, numbers.Integral):
        raise ValueError("hash_size 必须是正整数")
    if int(hash_size) < 1:
        raise ValueError("hash_size 必须 >= 1")

    try:
        small = image.convert("L").resize(
            (int(hash_size), int(hash_size)), _resample(Image, "LANCZOS")
        )
    except (OSError, ValueError) as exc:
        raise PhotoPipelineError("计算 average hash 失败: %s" % exc) from exc
    # Pillow 12 起 getdata() 进入弃用流程；有 get_flattened_data 时优先用它。
    getter = getattr(small, "get_flattened_data", None)
    pixels = list(getter()) if callable(getter) else list(small.getdata())
    if not pixels:
        raise PhotoPipelineError("图片没有像素，无法计算 average hash")

    mean = sum(pixels) / float(len(pixels))
    bits = 0
    for pixel in pixels:
        bits = (bits << 1) | (1 if pixel >= mean else 0)
    digits = (int(hash_size) * int(hash_size) + 3) // 4
    return format(bits, "0%dx" % digits)


def hamming_distance(hash_a, hash_b) -> int:
    """两个 average hash 的汉明距离。"""
    left = _normalise_hash(hash_a)
    right = _normalise_hash(hash_b)
    if len(left) != len(right):
        raise ValueError("两个 hash 长度不同: %d vs %d" % (len(left), len(right)))
    return bin(int(left, 16) ^ int(right, 16)).count("1")


def is_duplicate(hash_a, hash_b, threshold: int = 0) -> bool:
    """汉明距离 <= threshold 视为重复；threshold=0 表示完全一致。"""
    if isinstance(threshold, bool) or not isinstance(threshold, numbers.Integral):
        raise ValueError("threshold 必须是非负整数")
    if int(threshold) < 0:
        raise ValueError("threshold 必须 >= 0")
    return hamming_distance(hash_a, hash_b) <= int(threshold)


# ---------------------------------------------------------------------------
# 裁剪
# ---------------------------------------------------------------------------
def _coerce_box(box, label: str = "裁剪框") -> Tuple[int, int, int, int]:
    if isinstance(box, (str, bytes)):
        raise CropError("%s 必须是 4 个整数 (left, top, right, bottom)" % label)
    try:
        values = tuple(box)
    except TypeError as exc:
        raise CropError("%s 必须是 4 个整数 (left, top, right, bottom)" % label) from exc
    if len(values) != 4:
        raise CropError("%s 必须是 4 个整数，得到 %d 个" % (label, len(values)))
    for value in values:
        if isinstance(value, bool) or not isinstance(value, numbers.Integral):
            raise CropError("%s 的每个坐标都必须是整数，得到 %r" % (label, value))
    return tuple(int(value) for value in values)


def _normalise_crop_boxes(crop_boxes) -> Tuple[Tuple[int, int, int, int], ...]:
    if crop_boxes is None:
        return ()
    try:
        items = list(crop_boxes)
    except TypeError as exc:
        raise CropError("crop_boxes 必须是裁剪框序列") from exc
    if not items:
        return ()
    if len(items) == 4 and all(
        isinstance(value, numbers.Integral) and not isinstance(value, bool)
        for value in items
    ):
        return (_coerce_box(items),)
    return tuple(_coerce_box(item, "crop_boxes[%d]" % index)
                 for index, item in enumerate(items))


def crop_box(image, box):
    """按 ``(left, top, right, bottom)`` 裁剪并返回 ``(裁剪图, CropRecord)``。

    超出边界的部分会自动裁掉；完全在图像外或反向框会抛 ``CropError``。
    """
    if isinstance(image, (str, bytes, os.PathLike)):
        image = load_image(image).image
    if isinstance(image, LoadedImage):
        image = image.image
    if not hasattr(image, "crop") or not hasattr(image, "size"):
        raise CropError("crop_box 需要 PIL Image、LoadedImage 或本地路径")

    source_size = (int(image.width), int(image.height))
    requested = _coerce_box(box)
    left, top, right, bottom = requested
    if left >= right or top >= bottom:
        raise CropError(
            "裁剪框必须满足 left<right 且 top<bottom，得到 %r" % (requested,)
        )

    width, height = source_size
    clipped_left = max(0, min(width, left))
    clipped_top = max(0, min(height, top))
    clipped_right = max(0, min(width, right))
    clipped_bottom = max(0, min(height, bottom))
    if clipped_left >= clipped_right or clipped_top >= clipped_bottom:
        raise CropError(
            "裁剪框 %r 与图像尺寸 %r 无交集" % (requested, source_size)
        )

    effective = (clipped_left, clipped_top, clipped_right, clipped_bottom)
    cropped = image.crop(effective)
    record = CropRecord(
        source_box=requested,
        crop_box=effective,
        source_size=source_size,
        output_size=(int(cropped.width), int(cropped.height)),
    )
    return cropped, record


# ---------------------------------------------------------------------------
# 流水线
# ---------------------------------------------------------------------------
def _path_text(value) -> str:
    try:
        return os.fsdecode(os.fspath(value))
    except TypeError:
        return str(value)


def _describe_error(action: str, exc: Exception) -> str:
    detail = str(exc).strip()
    if detail:
        return "%s: %s: %s" % (action, type(exc).__name__, detail)
    return "%s: %s" % (action, type(exc).__name__)


def run_file_pipeline(
    sources,
    *,
    output_dir=None,
    thresholds=None,
    image_format: str = "JPEG",
    jpeg_quality: int = 90,
    max_side: Optional[int] = None,
    scale: Optional[float] = None,
    rotate: float = 0.0,
    crop_boxes=None,
    dedup_threshold: int = 0,
    loader: ImageLoader = load_image,
    checker: QualityChecker = quality_check,
    preprocessor: Preprocessor = preprocess,
    hasher: AverageHasher = average_hash,
) -> List[PipelineResult]:
    """处理一张或多张本地图片，返回逐张结构化结果。

    * 质量 reject/retry 的图不进入预处理，也不会产生输出文件；
    * 本次运行内 average hash 距离 <= ``dedup_threshold`` 的图标记 duplicate，
      并记录第一次出现的 ``duplicate_of``；
    * 任一文件失败只影响该条结果，其它图继续处理；
    * ``loader`` / ``checker`` / ``preprocessor`` / ``hasher`` 可替换。
    """
    if isinstance(sources, (str, bytes, os.PathLike)):
        source_list = [sources]
    else:
        try:
            source_list = list(sources)
        except TypeError as exc:
            raise PhotoPipelineError("sources 必须是路径或路径序列") from exc

    if isinstance(dedup_threshold, bool) or not isinstance(
        dedup_threshold, numbers.Integral
    ):
        raise ValueError("dedup_threshold 必须是非负整数")
    if int(dedup_threshold) < 0:
        raise ValueError("dedup_threshold 必须 >= 0")

    # 预处理参数是整批共享的；先校验，避免"全是 reject 时错误配置被静默跳过"。
    _validate_preprocess_args(image_format, jpeg_quality, max_side, scale, rotate)

    if not source_list:
        return []

    boxes = _normalise_crop_boxes(crop_boxes)
    if output_dir is None:
        run_output_dir = tempfile.mkdtemp(prefix="photo_pipeline_")
    else:
        try:
            run_output_dir = os.path.abspath(os.fsdecode(os.fspath(output_dir)))
        except TypeError as exc:
            raise PreprocessError("output_dir 必须是 str/PathLike") from exc
        try:
            os.makedirs(run_output_dir, exist_ok=True)
        except OSError as exc:
            raise PreprocessError(
                "创建输出目录失败: %s: %s" % (run_output_dir, exc)
            ) from exc

    results: List[PipelineResult] = []
    seen = []  # [(average_hash, source_path)]

    for item in source_list:
        source_text = _path_text(item)
        source_abs = os.path.abspath(source_text)
        loaded = None
        quality = None
        opaque_hash = None
        frame = None
        crop_records = []
        try:
            loaded = loader(source_text)
            if not isinstance(loaded, LoadedImage):
                raise TypeError("loader 必须返回 LoadedImage")
            source_abs = loaded.meta.source_path

            checked = checker(loaded.image, thresholds)
            if not isinstance(checked, QualityResult):
                raise TypeError("checker 必须返回 QualityResult")
            quality = checked

            if quality.status in (REJECT, RETRY):
                results.append(
                    PipelineResult(
                        source=source_abs,
                        ok=True,
                        status=quality.status,
                        frame=loaded.meta,
                        quality=quality,
                    )
                )
                continue

            # 归一化后再比较：自定义 hasher 返回坏值时，错误会落在本张结果里。
            opaque_hash = _normalise_hash(hasher(loaded.image))
            duplicate_of = None
            for previous_hash, previous_source in seen:
                if hamming_distance(opaque_hash, previous_hash) <= int(dedup_threshold):
                    duplicate_of = previous_source
                    break
            if duplicate_of is not None:
                results.append(
                    PipelineResult(
                        source=source_abs,
                        ok=True,
                        status=DUPLICATE,
                        frame=loaded.meta,
                        quality=quality,
                        average_hash=opaque_hash,
                        duplicate_of=duplicate_of,
                    )
                )
                continue

            output_frame = preprocessor(
                loaded,
                run_output_dir,
                image_format=image_format,
                jpeg_quality=jpeg_quality,
                max_side=max_side,
                scale=scale,
                rotate=rotate,
            )
            if not isinstance(output_frame, FrameMeta):
                raise TypeError("preprocessor 必须返回 FrameMeta")
            frame = output_frame

            for box in boxes:
                cropped, record = crop_box(frame.output_path, box)
                close = getattr(cropped, "close", None)
                if callable(close):
                    close()
                crop_records.append(record)

            # 只有完整成功（预处理 + 裁剪）的图才进入去重索引，避免后续图
            # 被指向一个实际没有产出/报错的源。
            seen.append((opaque_hash, source_abs))
            results.append(
                PipelineResult(
                    source=source_abs,
                    ok=True,
                    status=PASS,
                    frame=frame,
                    quality=quality,
                    crops=tuple(crop_records),
                    average_hash=opaque_hash,
                )
            )
        except Exception as exc:  # 单张失败不能拖垮整批
            results.append(
                PipelineResult(
                    source=source_abs,
                    ok=False,
                    status=ERROR,
                    frame=frame if frame is not None else getattr(loaded, "meta", None),
                    quality=quality,
                    crops=tuple(crop_records),
                    average_hash=opaque_hash,
                    error=_describe_error("处理失败", exc),
                    error_type=type(exc).__name__,
                )
            )
        finally:
            # 默认 loader 的图片是独立副本；自定义 loader 的图片归调用方管理。
            if loaded is not None and loader is load_image:
                loaded.close()

    return results


def results_to_json(results, indent: int = 2) -> str:
    """把一条或多条结果编码为 JSON 字符串（UTF-8 友好，不转义中文）。"""
    if isinstance(results, PipelineResult):
        results = [results]
    return json.dumps(
        [result.to_dict() for result in results],
        ensure_ascii=False,
        indent=indent,
    )


# ---------------------------------------------------------------------------
# 命令行入口（可选；库调用不需要它）
# ---------------------------------------------------------------------------
def _parse_box_arg(text: str) -> Tuple[int, int, int, int]:
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--crop 需要 4 个整数：L,T,R,B")
    try:
        return tuple(int(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--crop 坐标必须是整数: %s" % text) from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="本地照片处理最小链路：load -> quality -> preprocess -> dedup -> crop -> JSON"
    )
    parser.add_argument("images", nargs="+", help="一张或多张本地图片路径")
    parser.add_argument("--out", default=None, help="输出目录；缺省用系统临时目录")
    parser.add_argument("--format", dest="image_format", default="JPEG",
                        help="输出格式：JPEG 或 PNG（默认 JPEG）")
    parser.add_argument("--quality", dest="jpeg_quality", type=int, default=90,
                        help="JPEG 质量 1..100（默认 90）")
    parser.add_argument("--max-side", type=int, default=None,
                        help="旋转后最长边上限（与 --scale 二选一）")
    parser.add_argument("--scale", type=float, default=None,
                        help="显式缩放倍率（与 --max-side 二选一）")
    parser.add_argument("--rotate", type=float, default=0.0,
                        help="逆时针旋转角度（默认 0）")
    parser.add_argument("--crop", action="append", type=_parse_box_arg, default=None,
                        metavar="L,T,R,B", help="裁剪框，可重复")
    parser.add_argument("--dedup-threshold", type=int, default=0,
                        help="average hash 汉明距离阈值（默认 0）")
    parser.add_argument("--thresholds", default=None,
                        help="JSON 字符串，覆盖 QualityThresholds 字段")
    args = parser.parse_args(argv)

    thresholds = None
    if args.thresholds:
        try:
            thresholds = json.loads(args.thresholds)
        except json.JSONDecodeError as exc:
            parser.error("--thresholds 不是合法 JSON: %s" % exc)
        if not isinstance(thresholds, dict):
            parser.error("--thresholds 必须是 JSON 对象")

    try:
        results = run_file_pipeline(
            args.images,
            output_dir=args.out,
            thresholds=thresholds,
            image_format=args.image_format,
            jpeg_quality=args.jpeg_quality,
            max_side=args.max_side,
            scale=args.scale,
            rotate=args.rotate,
            crop_boxes=args.crop,
            dedup_threshold=args.dedup_threshold,
        )
    except PhotoPipelineError as exc:
        parser.error(str(exc))

    print(results_to_json(results))
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
