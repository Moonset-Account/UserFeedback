"""结构化样本的数据模型。

样本由若干 :class:`Record` 组成，每条记录拥有：

* 唯一标识 ``rid``；
* 字段字典 ``fields``；
* 每个字段的声明 :class:`FieldSpec`（类型、最小长度、是否可选）。

引用完整性与长度约束属于“声明的约束”，缩减变换不得破坏它们
（见 :mod:`reducer.validator` 与 :mod:`reducer.engine`）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

FieldKind = Literal["string", "reference", "references", "any"]


@dataclass(frozen=True)
class FieldSpec:
    """字段声明。

    kind 含义：

    * ``string``：字符串，受 ``min_length`` 约束；
    * ``reference``：单引用，值为被引用记录的 id；
    * ``references``：多引用，值为 id 列表；
    * ``any``：其他类型，不做内容缩减。
    """

    name: str
    kind: FieldKind = "any"
    min_length: int = 0
    optional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "min_length": self.min_length,
            "optional": self.optional,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FieldSpec":
        return cls(
            name=data["name"],
            kind=data["kind"],
            min_length=int(data.get("min_length", 0)),
            optional=bool(data.get("optional", False)),
        )


@dataclass
class Record:
    rid: str
    fields: dict[str, Any]
    specs: dict[str, FieldSpec]

    def reference_targets(self) -> list[str]:
        """返回该记录声明的全部引用目标 id。"""
        targets: list[str] = []
        for spec in self.specs.values():
            if spec.name not in self.fields:
                continue
            value = self.fields[spec.name]
            if spec.kind == "reference":
                if isinstance(value, str):
                    targets.append(value)
            elif spec.kind == "references":
                if isinstance(value, list):
                    targets.extend(v for v in value if isinstance(v, str))
        return targets

    def to_dict(self) -> dict[str, Any]:
        return {
            "rid": self.rid,
            "fields": dict(self.fields),
            "specs": [spec.to_dict() for spec in self.specs.values()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Record":
        specs = {s["name"]: FieldSpec.from_dict(s) for s in data.get("specs", [])}
        return cls(rid=data["rid"], fields=dict(data["fields"]), specs=specs)


@dataclass
class Sample:
    """记录列表；记录顺序是样本内容的一部分（影响规范化哈希）。"""

    records: list[Record]

    # ---- 基本访问 -------------------------------------------------
    def ids(self) -> list[str]:
        return [r.rid for r in self.records]

    def get(self, rid: str) -> Record:
        for record in self.records:
            if record.rid == rid:
                return record
        raise KeyError(rid)

    def contains(self, rid: str) -> bool:
        return any(r.rid == rid for r in self.records)

    # ---- 序列化与内容哈希 ----------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {"records": [r.to_dict() for r in self.records]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Sample":
        return cls(records=[Record.from_dict(r) for r in data["records"]])

    def copy(self) -> "Sample":
        return Sample.from_dict(self.to_dict())

    def canonical_json(self) -> str:
        """规范化 JSON：键排序、紧凑分隔，保证同内容同字节。"""
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def content_hash(self) -> str:
        """候选内容指纹（SHA-256），缓存绑定三要素之一。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
