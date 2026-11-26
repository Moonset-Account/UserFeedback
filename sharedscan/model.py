"""受限关系计划的领域模型。

值模型
------
* 单元格值为 ``None`` 表示 SQL 意义上的 NULL（缺失值）。
* 类型族由 :func:`type_family` 推断：``"int"`` / ``"float"`` / ``"str"`` /
  ``"bool"``；``None`` 没有类型族。
* bool 与 int 是不同的类型族，解释器不会把 ``True`` 当成 ``1``。

请求（:class:`Request`）描述一个受限聚合报表：受限关系（表 + 分区集合）、
分组键、行级筛选（WHERE）、聚合集合，以及 HAVING 与空输入/空组语义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Union


# --------------------------------------------------------------------------- #
# 值与类型族
# --------------------------------------------------------------------------- #
Scalar = Union[int, float, str, bool, None]
Row = dict[str, Scalar]


def type_family(value: Scalar) -> Optional[str]:
    """返回值的类型族；NULL(``None``) 返回 ``None``。

    bool 必须先于 int 判断，因为 ``isinstance(True, int)`` 为真。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    raise TypeError(f"不支持的字面量类型: {type(value)!r}")


# --------------------------------------------------------------------------- #
# 筛选谓词（受限形式：原子比较的 AND / OR 组合）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Cmp:
    """列与字面量的原子比较。"""

    column: str
    op: str  # "=" | "!=" | "<" | "<=" | ">" | ">="
    literal: Scalar


@dataclass(frozen=True)
class IsNull:
    """列 IS NULL。"""

    column: str


@dataclass(frozen=True)
class IsNotNull:
    """列 IS NOT NULL。"""

    column: str


@dataclass(frozen=True)
class And:
    left: "Predicate"
    right: "Predicate"


@dataclass(frozen=True)
class Or:
    left: "Predicate"
    right: "Predicate"


Predicate = Union[Cmp, IsNull, IsNotNull, And, Or]


# --------------------------------------------------------------------------- #
# 聚合与请求
# --------------------------------------------------------------------------- #
class AggFn(str, Enum):
    COUNT_STAR = "count_star"
    COUNT = "count"          # 计数非空值
    COUNT_DISTINCT = "count_distinct"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


@dataclass(frozen=True)
class Agg:
    """一个聚合项。

    ``column`` 对 ``COUNT_STAR`` 可为 ``None``。
    ``include_null`` 只对 ``COUNT_DISTINCT`` 有意义：为 ``True`` 时 NULL
    计为一个去重值（标准 SQL 语义），为 ``False`` 时忽略 NULL。
    """

    alias: str
    fn: AggFn
    column: Optional[str]
    include_null: bool = True


class EmptyInputPolicy(str, Enum):
    """无分组键（grand total）请求在零输入行时的输出策略。"""

    ONE_NULL_ROW = "one_null_row"   # 标准 SQL：输出一行，聚合为各自的空值
    ZERO_ROWS = "zero_rows"         # 输出零行


class EmptyGroupPolicy(str, Enum):
    """有分组键请求对“域中存在但数据中缺席”的组的策略。"""

    DROP = "drop"                   # 只输出实际出现的组（标准 GROUP BY）
    KEEP_DOMAIN = "keep_domain"     # 用空聚合值补齐 key_domain 中的组


@dataclass(frozen=True)
class Semantics:
    """请求间可能不同的语义开关。

    这些差异本身不阻塞共享：行级筛选在共享扫描里以条件聚合承载，
    空输入/空组策略在每个请求的投影阶段承载。
    """

    empty_input: EmptyInputPolicy = EmptyInputPolicy.ONE_NULL_ROW
    empty_group: EmptyGroupPolicy = EmptyGroupPolicy.DROP


@dataclass(frozen=True)
class Request:
    """一个原始报表请求。"""

    name: str
    table: str
    partitions: tuple[str, ...]
    group_keys: tuple[str, ...]
    aggs: tuple[Agg, ...]
    where: Optional[Predicate] = None
    having: Optional[Predicate] = None
    semantics: Semantics = field(default_factory=Semantics)
    #: 分组键的完整域（用于 KEEP_DOMAIN），元素为按 group_keys 排列的值元组。
    key_domain: tuple[tuple[Scalar, ...], ...] = ()


# --------------------------------------------------------------------------- #
# 目录（受限关系计划的元数据与数据）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Table:
    name: str
    schema: dict[str, str]            # 列名 -> 类型族
    partitions: dict[str, list[Row]]  # 分区名 -> 行


@dataclass(frozen=True)
class Catalog:
    tables: dict[str, Table]
    #: 标示某个 (表, 列) 在分区之间取值互斥（跨分区 COUNT DISTINCT 可直接相加）。
    exclusive_distinct: frozenset[tuple[str, str]]


def make_catalog(
    tables: dict[str, Table],
    exclusive_distinct: Optional[set[tuple[str, str]] | frozenset[tuple[str, str]]] = None,
) -> Catalog:
    """构造目录；``exclusive_distinct`` 为跨分区互斥的 (表, 去重列) 集合。"""
    return Catalog(
        tables=dict(tables),
        exclusive_distinct=frozenset(exclusive_distinct or frozenset()),
    )
