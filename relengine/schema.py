"""关系引擎的模式对象：表与外键声明。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

# 删除策略常量。
# CASCADE：引用行随被引用行一起删除（在同一条语句内传递闭包）。
# CHECK：不级联，延迟到整条语句结束时统一检查，幸存行仍引用已删除目标即阻塞。
CASCADE = "CASCADE"
CHECK = "CHECK"

_VALID_ACTIONS = frozenset({CASCADE, CHECK})


def normalize_pk(pk: Any) -> Tuple[str, ...]:
    """把调用方给出的主键定义规范化为元组。"""
    if isinstance(pk, str):
        if not pk:
            raise ValueError("primary key column name must not be empty")
        return (pk,)
    if isinstance(pk, tuple) and pk and all(isinstance(c, str) and c for c in pk):
        return pk
    raise TypeError("primary key must be a column name string or a non-empty tuple of them")


def normalize_key(pk_columns: Tuple[str, ...], raw_key: Any) -> Tuple[Any, ...]:
    """把调用方给出的主键值规范化为与 pk_columns 等长的元组。"""
    if isinstance(raw_key, tuple):
        if len(raw_key) != len(pk_columns):
            raise ValueError(
                f"composite key {raw_key!r} does not match primary key {pk_columns}"
            )
        return raw_key
    if len(pk_columns) == 1:
        return (raw_key,)
    raise ValueError(
        f"table with composite primary key {pk_columns} requires a tuple key, got {raw_key!r}"
    )


@dataclass(frozen=True)
class ForeignKey:
    """一条外键约束。

    name         约束名，库内唯一
    table        引用方表名
    columns      引用方外键列（有序）
    ref_table    被引用表名
    ref_columns  被引用列（有序，本引擎要求即被引用表的主键）
    on_delete    CASCADE 或 CHECK
    """

    name: str
    table: str
    columns: Tuple[str, ...]
    ref_table: str
    ref_columns: Tuple[str, ...]
    on_delete: str


class Table:
    """一张表：主键列、已知列集合，以及按主键元组存放的行数据。"""

    def __init__(self, name: str, pk: Any) -> None:
        self.name = name
        self.pk_columns: Tuple[str, ...] = normalize_pk(pk)
        # 列允许在 insert 时按需扩展，初始至少包含主键列。
        self.columns: list[str] = list(self.pk_columns)
        self.rows: Dict[Tuple[Any, ...], Dict[str, Any]] = {}

    def key_of(self, row: Dict[str, Any]) -> Tuple[Any, ...]:
        try:
            key = tuple(row[col] for col in self.pk_columns)
        except KeyError as exc:
            raise ValueError(
                f"table {self.name!r} row is missing primary key column {exc.args[0]!r}"
            ) from exc
        if any(value is None for value in key):
            raise ValueError(f"table {self.name!r} primary key columns must not be NULL")
        return key
