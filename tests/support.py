"""测试公用小工具（不是测试用例本身）。"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Sequence, Tuple

from grouping_sets import ResultRow


def index_rows(rows: Iterable[ResultRow]):
    """按 ``(set_index, grouping_values)`` 建索引，便于精确断言。"""

    return {(row.set_index, row.grouping_values): row for row in rows}


def values_of(rows: Iterable[ResultRow]):
    """把结果行缩成 ``(set_index, grouping_values, aggregates)`` 的可读元组集合。"""

    return {
        (row.set_index, row.grouping_values, row.aggregates) for row in rows
    }


def snapshot_values(result) -> Dict[int, set]:
    """按分组集合序号汇总快照内容。"""

    out: Dict[int, set] = {}
    for block in result.snapshot:
        for row in block:
            out.setdefault(row.set_index, set()).add(
                (row.grouping_values, row.grouping_bits, row.aggregates)
            )
    return out


def row_at(snapshot: Sequence[Sequence[ResultRow]], set_index: int, key: Tuple[Any, ...]):
    """从快照里取指定集合、指定分组键的行；不存在返回 None。"""

    for row in snapshot[set_index]:
        if row.grouping_values == key:
            return row
    return None


def add_row_sets(engine, rows):
    return engine.apply(adds=rows)


def plain(rows: Iterable[ResultRow]):
    """只保留 ``(grouping_values, aggregates)``，忽略位图。"""

    return [(row.grouping_values, row.aggregates) for row in rows]
