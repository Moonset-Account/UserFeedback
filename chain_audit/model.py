"""证据链核查模块的单据与报告数据模型。

所有数量均为整数毫克，区间为闭区间 ``[lo, hi]``。
单据是模块唯一的输入形式，每张单据带可查编号 ``doc_id``，
矛盾报告中出现的编号都能回溯到这里的具体单据。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

# 未给出明确上限时使用的全局毫克上限，仅用于约束传播的有限域。
GLOBAL_CAP = 1_000_000


class InputError(ValueError):
    """单据本身 malformed（非矛盾），例如区间倒置、节点跨批次、成环。"""


def _as_int(value, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"{what} 必须是整数毫克，收到 {value!r}")
    return value


def _check_interval(lo: int, hi: int, what: str) -> None:
    lo = _as_int(lo, f"{what} 下界")
    hi = _as_int(hi, f"{what} 上界")
    if lo < 0:
        raise InputError(f"{what} 下界不能为负: {lo}")
    if lo > hi:
        raise InputError(f"{what} 区间倒置: [{lo}, {hi}]")


def _nonempty(doc_id: str) -> None:
    if not isinstance(doc_id, str) or not doc_id:
        raise InputError("doc_id 必须是非空字符串")


@dataclass(frozen=True)
class IntakeDoc:
    """入库单：一批原样物证 ``node_id`` 的可核实总量区间。"""

    doc_id: str
    batch: str
    node_id: str
    amount_lo: int
    amount_hi: int

    def __post_init__(self) -> None:
        _nonempty(self.doc_id)
        _check_interval(self.amount_lo, self.amount_hi, f"入库单 {self.doc_id}")


@dataclass(frozen=True)
class SplitDecl:
    """拆分声明：``parent_id`` 被拆成 ``child_ids`` 中的多个送检子样。"""

    doc_id: str
    batch: str
    parent_id: str
    child_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonempty(self.doc_id)
        if not self.parent_id:
            raise InputError(f"拆分声明 {self.doc_id} 缺少 parent_id")
        children = tuple(self.child_ids)
        if len(children) < 2:
            raise InputError(f"拆分声明 {self.doc_id} 至少要有两个子样")
        if len(set(children)) != len(children):
            raise InputError(f"拆分声明 {self.doc_id} 子样重复")
        object.__setattr__(self, "child_ids", children)


@dataclass(frozen=True)
class MixDecl:
    """混样声明：多个 ``parent_ids`` 合并为一个 ``child_id``。"""

    doc_id: str
    batch: str
    parent_ids: tuple[str, ...]
    child_id: str

    def __post_init__(self) -> None:
        _nonempty(self.doc_id)
        parents = tuple(self.parent_ids)
        if len(parents) < 2:
            raise InputError(f"混样声明 {self.doc_id} 至少要有两个来源")
        if len(set(parents)) != len(parents):
            raise InputError(f"混样声明 {self.doc_id} 来源重复")
        if not self.child_id:
            raise InputError(f"混样声明 {self.doc_id} 缺少 child_id")
        object.__setattr__(self, "parent_ids", parents)


@dataclass(frozen=True)
class MeasurementDoc:
    """实验室独立测量单：对节点 ``node_id`` 的盲测毫克区间。"""

    doc_id: str
    batch: str
    node_id: str
    amount_lo: int
    amount_hi: int

    def __post_init__(self) -> None:
        _nonempty(self.doc_id)
        _check_interval(self.amount_lo, self.amount_hi, f"测量单 {self.doc_id}")


@dataclass(frozen=True)
class DisclosedLinkDoc:
    """复核岗分拣单中披露的一条父子关系（可能只披露部分关系）。

    若给出毫克区间，则该条边上的流量被限制在区间内；
    不给区间时只声明“存在这条边”。
    """

    doc_id: str
    batch: str
    parent_id: str
    child_id: str
    amount_lo: Optional[int] = None
    amount_hi: Optional[int] = None

    def __post_init__(self) -> None:
        _nonempty(self.doc_id)
        if not self.parent_id or not self.child_id:
            raise InputError(f"分拣单 {self.doc_id} 父子节点不能为空")
        if (self.amount_lo is None) != (self.amount_hi is None):
            raise InputError(f"分拣单 {self.doc_id} 区间必须同时给出或同时省略")
        if self.amount_lo is not None:
            _check_interval(
                self.amount_lo, self.amount_hi, f"分拣单 {self.doc_id}"
            )


@dataclass(frozen=True)
class BlindGenerationDoc:
    """盲号世代绑定：某批次中 (盲号, 世代) 对应哪个规范节点。

    跨批次重用的盲号世代不同，对应不同节点，核查时不得混同。
    """

    doc_id: str
    batch: str
    blind_id: str
    generation: int
    node_id: str

    def __post_init__(self) -> None:
        _nonempty(self.doc_id)
        _as_int(self.generation, f"盲号单 {self.doc_id} 世代")
        if self.generation < 0:
            raise InputError(f"盲号单 {self.doc_id} 世代不能为负")
        if not self.blind_id or not self.node_id:
            raise InputError(f"盲号单 {self.doc_id} 字段不完整")


@dataclass(frozen=True)
class SourceShare:
    """某个原样来源在一个子样中的合法份额区间。"""

    source_id: str
    amount_lo: int
    amount_hi: int
    fraction_lo: Optional[Fraction]
    fraction_hi: Optional[Fraction]
    exact: bool

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "amount_lo": self.amount_lo,
            "amount_hi": self.amount_hi,
            "fraction_lo": (
                str(self.fraction_lo) if self.fraction_lo is not None else None
            ),
            "fraction_hi": (
                str(self.fraction_hi) if self.fraction_hi is not None else None
            ),
            "exact": self.exact,
        }


@dataclass(frozen=True)
class SourceAttribution:
    """``node_id`` 的全部合法来源（无唯一来源时保留所有合法来源区间）。"""

    node_id: str
    amount_lo: int
    amount_hi: int
    shares: tuple[SourceShare, ...]
    unique: bool

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(s.source_id for s in self.shares)


@dataclass(frozen=True)
class ContradictionReport:
    """总量不守恒等矛盾：由哪些可查单据形成，以及触发原因。"""

    doc_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    failed_variable: Optional[str]
    minimal: bool

    def to_dict(self) -> dict:
        return {
            "doc_ids": list(self.doc_ids),
            "reasons": list(self.reasons),
            "failed_variable": self.failed_variable,
            "minimal": self.minimal,
        }


@dataclass(frozen=True)
class IngestReport:
    """一次增量收窄（ingest）后的整体状态。"""

    version: int
    status: str  # "satisfiable" | "unsatisfiable" | "unknown"
    contradiction: Optional[ContradictionReport]
    doc_count: int


@dataclass(frozen=True)
class NodeInfo:
    node_id: str
    batch: str
    kind: str  # source | split | mix | derived | measured
    amount_lo: int
    amount_hi: int
    is_terminal: bool


__all__ = [
    "GLOBAL_CAP",
    "InputError",
    "IntakeDoc",
    "SplitDecl",
    "MixDecl",
    "MeasurementDoc",
    "DisclosedLinkDoc",
    "BlindGenerationDoc",
    "SourceShare",
    "SourceAttribution",
    "ContradictionReport",
    "IngestReport",
    "NodeInfo",
]
