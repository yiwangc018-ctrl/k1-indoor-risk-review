#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""照片链路与 YOLO / 文本 LLM 之间的可替换结构契约。

设计边界
--------
* 只使用标准库（dataclasses / typing / json / math / numbers），不依赖
  Pillow、numpy、cv2、ultralytics，也不加载模型或访问网络。
* 本文件**不实现检测**：它只定义检测器、场景事实、规则决策之间交换的
  数据形状、序列化格式和校验规则，方便以后把 YOLO、CPU 兜底检测器或
  fake detector 换进来而不改下游。
* ``NullDetector`` 是明确的“未配置”实现：返回空检测 + 错误码
  ``DETECTOR_NOT_CONFIGURED``，绝不编造框、类别或置信度。
* ``build_text_context`` 只产出纯文本 JSON，供 Qwen 等**文本**模型使用；
  它不包含图片、图片路径、base64 或像素数组。文本模型不能看图，这一点
  必须在数据契约里写死。

数据流（当前只到契约层）::

    image -> Detector.detect(...) -> DetectionSet
                                   -> SceneFact[]（由规则/结构层生成）
                                   -> Decision（由规则仲裁层生成）
    DetectionSet + metadata -> build_text_context(...) -> 纯文本 JSON -> 文本 LLM

序列化与校验
------------
四个数据类都提供 ``to_dict`` / ``from_dict`` / ``to_json`` / ``from_json``。
``from_dict`` 遇到未知字段会报错，不会静默丢弃；box 必须是 4 个有限数值
且满足 ``x1 < x2``、``y1 < y2``，confidence / score 必须在 0..1，level /
status 只能取本文件声明的枚举值。

坐标约定：``box`` 是原图像素坐标的 ``(x1, y1, x2, y2)``，不是
``(x, y, w, h)``，也不是归一化坐标。若检测器内部用 letterbox 坐标，必须在
检测器实现里先反算回原图坐标，再填充 ``Detection``。
"""
from __future__ import annotations

import json
import math
import numbers
from collections.abc import Mapping as MappingABC
from dataclasses import MISSING, dataclass, field, fields
from typing import Any, Mapping, Optional, Protocol, Tuple, runtime_checkable

__all__ = [
    "SCHEMA_VERSION",
    "TEXT_CONTEXT_SCHEMA",
    "DETECTOR_NOT_CONFIGURED",
    "SOURCE_DETECTOR",
    "PRESENT",
    "ABSENT",
    "UNKNOWN",
    "LOW",
    "MEDIUM",
    "HIGH",
    "SCENE_STATUSES",
    "DECISION_LEVELS",
    "VisionContractError",
    "Detection",
    "DetectionSet",
    "SceneFact",
    "Decision",
    "Detector",
    "NullDetector",
    "build_text_context",
]

SCHEMA_VERSION = "vision.contracts.v1"
TEXT_CONTEXT_SCHEMA = "vision.text_context.v1"

# NullDetector 的唯一错误码：检测器没有配置，而不是“检测结果为空”。
DETECTOR_NOT_CONFIGURED = "DETECTOR_NOT_CONFIGURED"

SOURCE_DETECTOR = "detector"

# SceneFact.status 枚举。unknown 是显式未知，不能当成 absent。
PRESENT = "present"
ABSENT = "absent"
UNKNOWN = "unknown"
SCENE_STATUSES = (PRESENT, ABSENT, UNKNOWN)

# Decision.level 枚举。unknown 不能当成 low。
LOW = "low"
MEDIUM = "medium"
HIGH = "high"
DECISION_LEVELS = (LOW, MEDIUM, HIGH, UNKNOWN)

# 文本上下文里禁止出现的图片载荷字段（大小写不敏感、递归检查）。
_IMAGE_PAYLOAD_KEYS = frozenset(
    {
        "image",
        "image_path",
        "image_bytes",
        "image_base64",
        "image_data",
        "image_url",
        "image_array",
        "image_tensor",
        "raw_image",
        "photo_base64",
        "pixel_data",
        "pixels",
        "base64",
        "img",
    }
)

Box = Tuple[float, float, float, float]


class VisionContractError(ValueError):
    """契约校验失败。继承 ValueError，调用方用普通 ``except ValueError`` 即可。"""


# ---------------------------------------------------------------------------
# 基础校验
# ---------------------------------------------------------------------------
def _label(path: Optional[str], name: str) -> str:
    return "%s.%s" % (path, name) if path else name


def _require_str(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise VisionContractError(
            "%s 必须是非空字符串，得到 %s" % (name, type(value).__name__)
        )
    if not value.strip():
        raise VisionContractError("%s 必须是非空字符串" % name)
    return value


def _require_optional_str(value: Any, name: str) -> Optional[str]:
    if value is None:
        return None
    return _require_str(value, name)


def _require_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise VisionContractError("%s 必须是 bool，得到 %r" % (name, value))


def _require_number(value: Any, name: str) -> float:
    # bool 是 int 的子类，必须显式排除，否则 confidence=True 会被当成 1。
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise VisionContractError("%s 必须是数字，得到 %r" % (name, value))
    number = float(value)
    if not math.isfinite(number):
        raise VisionContractError("%s 必须是有限数字，得到 %r" % (name, value))
    return number


def _require_unit_interval(value: Any, name: str) -> float:
    number = _require_number(value, name)
    if not (0.0 <= number <= 1.0):
        raise VisionContractError("%s 必须在 0..1，得到 %r" % (name, value))
    return number


def _require_box(value: Any, name: str) -> Box:
    """校验并归一化 xyxy box：4 个有限数值、非负、x1<x2、y1<y2。"""
    if isinstance(value, (str, bytes)):
        raise VisionContractError(
            "%s 必须是 4 个数字 (x1, y1, x2, y2)，得到字符串" % name
        )
    try:
        raw = tuple(value)
    except TypeError as exc:
        raise VisionContractError(
            "%s 必须是 4 个数字 (x1, y1, x2, y2)" % name
        ) from exc
    if len(raw) != 4:
        raise VisionContractError(
            "%s 必须正好 4 个数字 (x1, y1, x2, y2)，得到 %d 个" % (name, len(raw))
        )
    x1, y1, x2, y2 = (
        _require_number(raw[0], "%s[0]" % name),
        _require_number(raw[1], "%s[1]" % name),
        _require_number(raw[2], "%s[2]" % name),
        _require_number(raw[3], "%s[3]" % name),
    )
    if x1 < 0.0 or y1 < 0.0:
        raise VisionContractError(
            "%s 的左上角坐标不能为负，得到 %r" % (name, (x1, y1, x2, y2))
        )
    if x2 <= x1 or y2 <= y1:
        raise VisionContractError(
            "%s 必须满足 x1<x2 且 y1<y2，得到 %r" % (name, (x1, y1, x2, y2))
        )
    return (x1, y1, x2, y2)


def _require_optional_box(value: Any, name: str) -> Optional[Box]:
    if value is None:
        return None
    return _require_box(value, name)


def _require_enum(value: Any, allowed: Tuple[str, ...], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise VisionContractError(
            "%s 必须是 %s 之一，得到 %r"
            % (name, "/".join(allowed), value)
        )
    return value


def _as_tuple(value: Any, name: str) -> tuple:
    """把序列转成 tuple；字符串、bytes、映射和 None 都视为非法序列。"""
    if isinstance(value, (str, bytes, MappingABC)) or value is None:
        raise VisionContractError(
            "%s 必须是序列，得到 %s" % (name, type(value).__name__)
        )
    try:
        return tuple(value)
    except TypeError as exc:
        raise VisionContractError(
            "%s 必须是序列，得到 %s" % (name, type(value).__name__)
        ) from exc


def _normalise_detections(values: Any) -> Tuple["Detection", ...]:
    items = _as_tuple(values, "DetectionSet.detections")
    for index, item in enumerate(items):
        if not isinstance(item, Detection):
            raise VisionContractError(
                "DetectionSet.detections[%d] 必须是 Detection，得到 %s"
                % (index, type(item).__name__)
            )
    return tuple(items)


def _normalise_errors(values: Any) -> Tuple[str, ...]:
    items = _as_tuple(values, "DetectionSet.errors")
    errors = []
    for index, item in enumerate(items):
        errors.append(_require_str(item, "DetectionSet.errors[%d]" % index))
    return tuple(errors)


def _normalise_facts(values: Any) -> Tuple["SceneFact", ...]:
    items = _as_tuple(values, "Decision.items")
    for index, item in enumerate(items):
        if not isinstance(item, SceneFact):
            raise VisionContractError(
                "Decision.items[%d] 必须是 SceneFact，得到 %s"
                % (index, type(item).__name__)
            )
    return tuple(items)


def _normalise_json(value: Any, path: str) -> Any:
    """把 provenance / metadata 归一化成可 json.dumps 的内置类型。"""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise VisionContractError("%s 不能包含 NaN/Infinity" % path)
        return value
    if isinstance(value, MappingABC):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise VisionContractError(
                    "%s 的键必须是非空字符串，得到 %r" % (path, key)
                )
            result[key] = _normalise_json(item, "%s.%s" % (path, key))
        return result
    if isinstance(value, (list, tuple)):
        return [
            _normalise_json(item, "%s[%d]" % (path, index))
            for index, item in enumerate(value)
        ]
    raise VisionContractError(
        "%s 只接受 JSON 可序列化值，得到 %s" % (path, type(value).__name__)
    )


def _validate_from_dict(cls, data: Any) -> Mapping:
    """from_dict 的公共入口：必须是映射，且不能有未知/缺失的必填字段。"""
    if not isinstance(data, MappingABC):
        raise VisionContractError(
            "%s.from_dict 需要映射，得到 %s" % (cls.__name__, type(data).__name__)
        )
    known = {item.name: item for item in fields(cls)}
    unknown = [key for key in data if key not in known]
    if unknown:
        raise VisionContractError(
            "%s.from_dict 收到未知字段: %s"
            % (cls.__name__, ", ".join(sorted(repr(key) for key in unknown)))
        )
    missing = [
        name
        for name, item in known.items()
        if item.default is MISSING
        and item.default_factory is MISSING
        and name not in data
    ]
    if missing:
        raise VisionContractError(
            "%s.from_dict 缺少必填字段: %s"
            % (cls.__name__, ", ".join(sorted(missing)))
        )
    return data


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------
class _Contract:
    """所有契约对象的序列化接口。"""

    def to_dict(self) -> dict:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    @classmethod
    def from_dict(cls, data):  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, payload):
        if not isinstance(payload, (str, bytes, bytearray)):
            raise VisionContractError(
                "%s.from_json 需要 JSON 字符串，得到 %s"
                % (cls.__name__, type(payload).__name__)
            )
        try:
            data = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise VisionContractError(
                "%s.from_json 不是合法 JSON: %s" % (cls.__name__, exc)
            ) from exc
        return cls.from_dict(data)


@dataclass(frozen=True)
class Detection(_Contract):
    """单个检测框。

    字段
    ----
    box: ``(x1, y1, x2, y2)``，原图像素坐标，xyxy 而非 xywh。
    class_name: 类别名；必须来自检测器的类别表，不能由 LLM 编造。
    confidence: 检测置信度，0..1；**不是**风险概率。
    source: 产生该框的来源（默认约定 ``detector``）。
    image_id: 框所属图片的稳定标识，用于把证据反查回原图。
    """

    box: Box
    class_name: str
    confidence: float
    source: str
    image_id: str

    def __post_init__(self):
        object.__setattr__(self, "box", _require_box(self.box, "Detection.box"))
        object.__setattr__(
            self, "class_name", _require_str(self.class_name, "Detection.class_name")
        )
        object.__setattr__(
            self,
            "confidence",
            _require_unit_interval(self.confidence, "Detection.confidence"),
        )
        object.__setattr__(
            self, "source", _require_str(self.source, "Detection.source")
        )
        object.__setattr__(
            self, "image_id", _require_str(self.image_id, "Detection.image_id")
        )

    def to_dict(self) -> dict:
        return {
            "box": list(self.box),
            "class_name": self.class_name,
            "confidence": self.confidence,
            "source": self.source,
            "image_id": self.image_id,
        }

    @classmethod
    def from_dict(cls, data) -> "Detection":
        data = _validate_from_dict(cls, data)
        return cls(
            box=data["box"],
            class_name=data["class_name"],
            confidence=data["confidence"],
            source=data["source"],
            image_id=data["image_id"],
        )


@dataclass(frozen=True)
class DetectionSet(_Contract):
    """一张图片的检测结果信封。

    ``errors`` 为空表示检测器正常执行；非空表示降级/未配置/推理失败。
    **空检测 + errors 不能解释成“没有风险”**，下游必须保留 degraded 语义。
    """

    image_id: str
    model: str
    provider: str
    detections: Tuple[Detection, ...] = ()
    latency_ms: float = 0.0
    errors: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(
            self, "image_id", _require_str(self.image_id, "DetectionSet.image_id")
        )
        object.__setattr__(
            self, "model", _require_str(self.model, "DetectionSet.model")
        )
        object.__setattr__(
            self, "provider", _require_str(self.provider, "DetectionSet.provider")
        )
        object.__setattr__(
            self, "detections", _normalise_detections(self.detections)
        )
        latency = _require_number(
            self.latency_ms, "DetectionSet.latency_ms"
        )
        if latency < 0.0:
            raise VisionContractError(
                "DetectionSet.latency_ms 不能为负，得到 %r" % (self.latency_ms,)
            )
        object.__setattr__(self, "latency_ms", latency)
        object.__setattr__(self, "errors", _normalise_errors(self.errors))

    @property
    def detection_count(self) -> int:
        return len(self.detections)

    @property
    def degraded(self) -> bool:
        """有错误码即视为降级；不要用空检测推断无风险。"""
        return bool(self.errors)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def error_codes(self) -> Tuple[str, ...]:
        return self.errors

    def to_dict(self) -> dict:
        return {
            "image_id": self.image_id,
            "model": self.model,
            "provider": self.provider,
            "detections": [item.to_dict() for item in self.detections],
            "latency_ms": self.latency_ms,
            "errors": list(self.errors),
        }

    @classmethod
    def from_dict(cls, data) -> "DetectionSet":
        data = _validate_from_dict(cls, data)
        detections = data.get("detections", ())
        if not isinstance(detections, (list, tuple)):
            raise VisionContractError(
                "DetectionSet.detections 必须是 Detection 映射的列表"
            )
        return cls(
            image_id=data["image_id"],
            model=data["model"],
            provider=data["provider"],
            detections=tuple(Detection.from_dict(item) for item in detections),
            latency_ms=data.get("latency_ms", 0.0),
            errors=data.get("errors", ()),
        )


@dataclass(frozen=True)
class SceneFact(_Contract):
    """一条场景事实。

    ``status`` 只允许 present / absent / unknown；``score`` 是事实置信度
    0..1，不是风险分数。``unknown`` 必须保留，不能折叠成 absent。
    """

    category: str
    status: str
    score: float
    evidence_image: Optional[str] = None
    evidence_box: Optional[Box] = None
    needs_review: bool = False

    def __post_init__(self):
        object.__setattr__(
            self, "category", _require_str(self.category, "SceneFact.category")
        )
        object.__setattr__(
            self,
            "status",
            _require_enum(self.status, SCENE_STATUSES, "SceneFact.status"),
        )
        object.__setattr__(
            self, "score", _require_unit_interval(self.score, "SceneFact.score")
        )
        object.__setattr__(
            self,
            "evidence_image",
            _require_optional_str(
                self.evidence_image, "SceneFact.evidence_image"
            ),
        )
        object.__setattr__(
            self,
            "evidence_box",
            _require_optional_box(self.evidence_box, "SceneFact.evidence_box"),
        )
        object.__setattr__(
            self,
            "needs_review",
            _require_bool(self.needs_review, "SceneFact.needs_review"),
        )

    @property
    def has_evidence(self) -> bool:
        return self.evidence_image is not None or self.evidence_box is not None

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "status": self.status,
            "score": self.score,
            "evidence_image": self.evidence_image,
            "evidence_box": (
                list(self.evidence_box) if self.evidence_box is not None else None
            ),
            "needs_review": self.needs_review,
        }

    @classmethod
    def from_dict(cls, data) -> "SceneFact":
        data = _validate_from_dict(cls, data)
        return cls(
            category=data["category"],
            status=data["status"],
            score=data["score"],
            evidence_image=data.get("evidence_image"),
            evidence_box=data.get("evidence_box"),
            needs_review=data.get("needs_review", False),
        )


@dataclass(frozen=True)
class Decision(_Contract):
    """规则仲裁后的等级结论。

    ``items`` 是参与判级的 SceneFact 列表；``degraded`` 标记链路是否降级；
    ``provenance`` 是 JSON 对象，保留模型/provider/错误码等来源信息，便于
    报告和复现。等级只能由规则层写入，LLM/VLM 不能直接改 level。
    """

    level: str
    items: Tuple[SceneFact, ...] = ()
    degraded: bool = False
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(
            self,
            "level",
            _require_enum(self.level, DECISION_LEVELS, "Decision.level"),
        )
        object.__setattr__(self, "items", _normalise_facts(self.items))
        object.__setattr__(
            self, "degraded", _require_bool(self.degraded, "Decision.degraded")
        )
        provenance = _normalise_json(self.provenance, "Decision.provenance")
        if not isinstance(provenance, dict):
            raise VisionContractError(
                "Decision.provenance 必须是 JSON 对象，得到 %s"
                % type(self.provenance).__name__
            )
        object.__setattr__(self, "provenance", provenance)

    @property
    def needs_review(self) -> bool:
        return any(item.needs_review for item in self.items)

    @property
    def unknown_categories(self) -> Tuple[str, ...]:
        return tuple(
            item.category for item in self.items if item.status == UNKNOWN
        )

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "items": [item.to_dict() for item in self.items],
            "degraded": self.degraded,
            "provenance": _normalise_json(
                self.provenance, "Decision.provenance"
            ),
        }

    @classmethod
    def from_dict(cls, data) -> "Decision":
        data = _validate_from_dict(cls, data)
        items = data.get("items", ())
        if not isinstance(items, (list, tuple)):
            raise VisionContractError(
                "Decision.items 必须是 SceneFact 映射的列表"
            )
        return cls(
            level=data["level"],
            items=tuple(SceneFact.from_dict(item) for item in items),
            degraded=data.get("degraded", False),
            provenance=data.get("provenance", {}),
        )


# ---------------------------------------------------------------------------
# 检测器协议与空实现
# ---------------------------------------------------------------------------
@runtime_checkable
class Detector(Protocol):
    """可替换检测器的最小接口。

    实现方只负责“有没有、在哪、像不像”，不判风险等级、不给建议。
    真实 YOLO 实现应在内部完成预处理和坐标反算；本仓库当前不提供。
    """

    def detect(
        self,
        image: Any,
        image_id: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> DetectionSet:
        ...


class NullDetector:
    """未配置检测器：返回空检测 + ``DETECTOR_NOT_CONFIGURED``。

    它不读取图片、不加载模型、不伪造任何 Detection。下游看到 errors 非空
    时必须按 degraded 处理，不能把空列表当成“照片里没有风险物”。
    """

    name = "null"
    model = "null"
    provider = "null"
    configured = False

    def detect(
        self,
        image: Any = None,
        image_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> DetectionSet:
        # image / metadata 只用于保持 Detector 签名；空实现不会读取或解码它们。
        del image, metadata
        if image_id is None:
            image_id = "unknown"
        return DetectionSet(
            image_id=image_id,
            model=self.model,
            provider=self.provider,
            detections=(),
            latency_ms=0.0,
            errors=(DETECTOR_NOT_CONFIGURED,),
        )


# ---------------------------------------------------------------------------
# 给文本 LLM 的纯文本上下文
# ---------------------------------------------------------------------------
def _normalise_metadata(metadata: Any) -> dict:
    if metadata is None:
        return {}
    if not isinstance(metadata, MappingABC):
        raise VisionContractError(
            "metadata 必须是映射，得到 %s" % type(metadata).__name__
        )
    normalised = _normalise_json(metadata, "metadata")
    if not isinstance(normalised, dict):
        raise VisionContractError("metadata 必须是 JSON 对象")
    return normalised


def _reject_image_payload(value: Any, path: str) -> None:
    """递归拒绝 metadata 里的图片载荷字段，保证文本上下文真的没有图片。"""
    if isinstance(value, MappingABC):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _IMAGE_PAYLOAD_KEYS:
                raise VisionContractError(
                    "%s 含图片字段 %r；文本 LLM 上下文不允许图片、图片路径、"
                    "base64 或像素数据" % (path, key)
                )
            _reject_image_payload(item, "%s.%s" % (path, key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_image_payload(item, "%s[%d]" % (path, index))
    elif isinstance(value, str) and value[:11].lower() == "data:image/":
        raise VisionContractError(
            "%s 含 data:image/ 内联图片；文本 LLM 上下文不允许图片数据" % path
        )


def _metadata_str(metadata: Mapping, key: str) -> Optional[str]:
    if key not in metadata:
        return None
    return _require_str(metadata[key], "metadata.%s" % key)


def _context_items(detections: Any) -> tuple:
    if isinstance(detections, DetectionSet):
        return detections.detections
    if isinstance(detections, Detection):
        return (detections,)
    items = _as_tuple(detections, "build_text_context.detections")
    for index, item in enumerate(items):
        if not isinstance(item, Detection):
            raise VisionContractError(
                "build_text_context.detections[%d] 必须是 Detection，得到 %s"
                % (index, type(item).__name__)
            )
    return tuple(items)


def build_text_context(detections: Any, metadata: Any = None) -> str:
    """生成给文本 Qwen 的纯文本 JSON 字符串。

    参数
    ----
    detections: ``DetectionSet``，或 ``Detection`` 的可迭代对象。
    metadata: 房间、相机、姿态等附加上下文，必须是 JSON 对象。

    约束
    ----
    * 返回值是 JSON 文本，不含任何图片字段、图片路径、base64 或像素数组。
    * ``modality`` 固定为 ``text``，``contains_image`` 固定为 ``False``。
    * 保留 ``errors`` / ``degraded``；空检测会附带“不等于没有风险”的警告。
    * metadata 里出现 ``image`` / ``image_path`` / ``image_base64`` 等键时
      直接报错，不做静默丢弃。
    """
    meta = _normalise_metadata(metadata)
    _reject_image_payload(meta, "metadata")

    if isinstance(detections, DetectionSet):
        items = detections.detections
        image_id = detections.image_id
        model = detections.model
        provider = detections.provider
        latency_ms = detections.latency_ms
        errors = detections.errors
    else:
        items = _context_items(detections)
        metadata_image_id = _metadata_str(meta, "image_id")
        if metadata_image_id is not None:
            image_id = metadata_image_id
        elif items:
            image_id = items[0].image_id
        else:
            image_id = "unknown"
        model = _metadata_str(meta, "model")
        provider = _metadata_str(meta, "provider")
        latency_ms = 0.0
        errors = ()

    warnings = []
    if errors:
        warnings.append(
            "检测器降级或未配置（%s）：不得把空检测当成没有风险。"
            % ", ".join(errors)
        )
    if not items:
        warnings.append("检测列表为空：只能说明未看到，不能推断不存在。")

    payload = {
        "schema": TEXT_CONTEXT_SCHEMA,
        "modality": "text",
        "contains_image": False,
        "instruction": (
            "只根据以下文本事实回答；不要声称看过照片。"
            "空检测只表示未看到，不等于不存在。"
        ),
        "image_id": image_id,
        "model": model,
        "provider": provider,
        "latency_ms": latency_ms,
        "errors": list(errors),
        "degraded": bool(errors),
        "detection_count": len(items),
        "detections": [item.to_dict() for item in items],
        "metadata": meta,
        "warnings": warnings,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
