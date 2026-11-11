"""采集结果数据模型（仅由解析公开清单得到）。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Issue:
    """采集过程中发现的具体问题，path 为清单内 JSON 指针式路径。"""

    code: str
    severity: str  # "error" | "warning"
    message: str
    path: str
    ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ImageDescription:
    """单张图像资源描述（图像标识 ≠ Canvas 标识 ≠ 章节引用）。"""

    image_id: str
    type: str  # Image
    label: str | None
    format: str | None
    width: int | None
    height: int | None
    service_id: str | None
    service_type: str | None
    service_profile: str | None
    thumbnail_url: str | None
    is_choice_alternative: bool
    source_path: str  # 该图像在清单中的 JSON 路径，作为来源证据
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["issues"] = [i.to_dict() for i in self.issues]
        return d


@dataclass
class CanvasPage:
    """一个 Canvas = 标本册中的一个页面/一件标本。"""

    canvas_id: str
    label: str | None
    width: int | None
    height: int | None
    source_path: str
    images: list[ImageDescription] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "canvas_id": self.canvas_id,
            "label": self.label,
            "width": self.width,
            "height": self.height,
            "source_path": self.source_path,
            "images": [i.to_dict() for i in self.images],
            "issues": [i.to_dict() for i in self.issues],
        }


@dataclass
class RangeNode:
    """层级 Range（章节），可引用 Canvas，也可嵌套其他 Range。"""

    range_id: str
    label: str | None
    source_path: str
    referenced_canvas_ids: list[str] = field(default_factory=list)
    child_range_ids: list[str] = field(default_factory=list)
    parent_range_ids: list[str] = field(default_factory=list)
    cyclic: bool = False
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "range_id": self.range_id,
            "label": self.label,
            "source_path": self.source_path,
            "referenced_canvas_ids": self.referenced_canvas_ids,
            "child_range_ids": self.child_range_ids,
            "parent_range_ids": self.parent_range_ids,
            "cyclic": self.cyclic,
            "issues": [i.to_dict() for i in self.issues],
        }


@dataclass
class CollectionResult:
    manifest_id: str | None
    manifest_label: str | None
    manifest_type: str | None
    source: str
    source_kind: str  # "http" | "file"
    status: str  # "ok" | "partial" | "failed"
    canvases: list[CanvasPage]
    ranges: list[RangeNode]
    issues: list[Issue]
    stats: dict[str, Any]
    raw_manifest: dict[str, Any] | None = None

    def to_dict(self, include_raw: bool = False) -> dict[str, Any]:
        d = {
            "manifest_id": self.manifest_id,
            "manifest_label": self.manifest_label,
            "manifest_type": self.manifest_type,
            "source": self.source,
            "source_kind": self.source_kind,
            "status": self.status,
            "canvases": [c.to_dict() for c in self.canvases],
            "ranges": [r.to_dict() for r in self.ranges],
            "issues": [i.to_dict() for i in self.issues],
            "stats": self.stats,
        }
        if include_raw and self.raw_manifest is not None:
            d["raw_manifest"] = self.raw_manifest
        return d
