"""字段语义契约与版本化定义。

一份 FieldContract 描述采集器对页面某一列的完整语义期望，由四部分组成：
  1. 字段标签（allowed_labels，允许的标签别名，子串匹配）
  2. 单位（expected_unit，units.py 支持子集内的标准单位）
  3. 限定说明（required_qualifiers，如“平均”；required_stat，mean/median）
  4. 数据类型（value_type，number/text）

CSS 选择器（header_selector / cell_selector）只负责定位元素，不参与语义判定：
页面改版保留选择器但改变含义时，本契约检查仍然会拦截。

契约版本不可变：v1 为内置基线；用户批准新映射后生成新版本（parent_version 指向上一版），
旧版本与批准时受影响的记录范围一并保留，可随时回溯。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

STAT_MEAN = "mean"
STAT_MEDIAN = "median"
STAT_UNKNOWN = "unknown"

TYPE_NUMBER = "number"
TYPE_TEXT = "text"


@dataclass(frozen=True)
class FieldContract:
    key: str
    name: str
    header_selector: str
    cell_selector: str
    allowed_labels: tuple[str, ...]
    expected_unit: str | None          # None 表示该字段无单位（如样品编号）
    required_qualifiers: tuple[str, ...]
    required_stat: str                 # mean / median；text 字段用 unknown
    value_type: str                    # number / text

    def __post_init__(self) -> None:
        # JSON 反序列化后序列字段是 list，统一转回 tuple 以保证契约对象可比较、可哈希语义稳定
        if not isinstance(self.allowed_labels, tuple):
            object.__setattr__(self, "allowed_labels", tuple(self.allowed_labels))
        if not isinstance(self.required_qualifiers, tuple):
            object.__setattr__(self, "required_qualifiers", tuple(self.required_qualifiers))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Contract:
    version: str
    table_selector: str
    row_selector: str
    fields: tuple[FieldContract, ...]
    parent_version: str | None = None
    approved_by: str | None = None
    approved_reason: str | None = None
    approved_at: str | None = None

    def field(self, key: str) -> FieldContract:
        for f in self.fields:
            if f.key == key:
                return f
        raise KeyError(key)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Contract":
        fields_ = tuple(FieldContract(**f) for f in data["fields"])
        return cls(
            version=data["version"],
            table_selector=data["table_selector"],
            row_selector=data["row_selector"],
            fields=fields_,
            parent_version=data.get("parent_version"),
            approved_by=data.get("approved_by"),
            approved_reason=data.get("approved_reason"),
            approved_at=data.get("approved_at"),
        )


# ---------------------------------------------------------------------------
# 内置基线契约 v1：青岚大学材料表征实验室“样品测量台账”页面
# 选择器与样例源站约定；语义以标签/单位/限定说明/类型为准。
# ---------------------------------------------------------------------------
BASELINE_CONTRACT = Contract(
    version="v1",
    table_selector="table#measure",
    row_selector="table#measure tbody tr",
    fields=(
        FieldContract(
            key="sample_id",
            name="样品编号",
            header_selector="table#measure th.col-sample",
            cell_selector="td.col-sample",
            allowed_labels=("样品编号", "样品号", "样本编号"),
            expected_unit=None,
            required_qualifiers=(),
            required_stat=STAT_UNKNOWN,
            value_type=TYPE_TEXT,
        ),
        FieldContract(
            key="dry_weight",
            name="样品干重",
            header_selector="table#measure th.col-dry",
            cell_selector="td.col-dry",
            allowed_labels=("干重", "样品干重"),
            expected_unit="g",
            required_qualifiers=("平均",),
            required_stat=STAT_MEAN,
            value_type=TYPE_NUMBER,
        ),
        FieldContract(
            key="crude_protein",
            name="粗蛋白含量",
            header_selector="table#measure th.col-protein",
            cell_selector="td.col-protein",
            allowed_labels=("粗蛋白", "粗蛋白含量", "粗蛋白质"),
            expected_unit="%",
            required_qualifiers=("平均",),
            required_stat=STAT_MEAN,
            value_type=TYPE_NUMBER,
        ),
        FieldContract(
            key="moisture",
            name="含水率",
            header_selector="table#measure th.col-moisture",
            cell_selector="td.col-moisture",
            allowed_labels=("含水率", "含水量", "水分"),
            expected_unit="%",
            required_qualifiers=("平均",),
            required_stat=STAT_MEAN,
            value_type=TYPE_NUMBER,
        ),
    ),
)


def derive_contract(
    base: Contract,
    overrides: dict[str, FieldContract],
    *,
    version: str,
    approved_by: str,
    approved_reason: str,
    approved_at: str,
) -> Contract:
    """基于旧契约生成新版本：未批准的字段保持原规则不变。"""
    new_fields = tuple(overrides.get(f.key, f) for f in base.fields)
    return Contract(
        version=version,
        table_selector=base.table_selector,
        row_selector=base.row_selector,
        fields=new_fields,
        parent_version=base.version,
        approved_by=approved_by,
        approved_reason=approved_reason,
        approved_at=approved_at,
    )
