"""结果行模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Sequence, Tuple

GroupingBits = Tuple[int, ...]
GroupingValues = Tuple[Any, ...]
AggregateValues = Tuple[Any, ...]


def _sort_atom(value: Any) -> Tuple[int, str, str]:
    """给任意规范化值一个全序、确定的排序键。

    先按"是否 None"、再按类型名、最后按 ``repr``，因此混合类型的分组键
    也不会在排序时抛 ``TypeError``；同一进程内结果确定。
    """

    return (0 if value is None else 1, type(value).__name__, repr(value))


def sort_key_for(values: Sequence[Any]) -> Tuple[Tuple[int, str, str], ...]:
    return tuple(_sort_atom(v) for v in values)


@dataclass(frozen=True)
class ResultRow:
    """一条分组集合结果行（不可变，可直接比较与去重）。

    ``grouping_values`` 按规格分组列顺序排好；被本分组集合省略的列为 ``None``。
    ``grouping_bits`` 是 GROUPING 位图（0=参与分组，1=被省略）。
    """

    set_index: int
    grouping_values: GroupingValues
    grouping_bits: GroupingBits
    aggregates: AggregateValues

    @property
    def grouping(self) -> int:
        """位图打包成的整数（第一个分组列是最高位）。"""

        value = 0
        for bit in self.grouping_bits:
            value = (value << 1) | bit
        return value

    @property
    def is_grand_total(self) -> bool:
        return all(bit == 1 for bit in self.grouping_bits)

    def as_dict(
        self,
        grouping_columns: Sequence[str],
        aggregate_outputs: Sequence[str],
    ) -> Dict[str, Any]:
        """展开成 ``{输出列名: 值}``，另附 ``__grouping__`` / ``__set_index__``。"""

        out: Dict[str, Any] = {}
        for name, value in zip(grouping_columns, self.grouping_values):
            out[name] = value
        for name, value in zip(aggregate_outputs, self.aggregates):
            out[name] = value
        out["__grouping__"] = self.grouping_bits
        out["__set_index__"] = self.set_index
        return out


@dataclass(frozen=True)
class BatchResult:
    """一次 ``apply`` 的返回值。

    Attributes:
        batch_index: 这是第几次成功批量调用（从 0 开始）。
        upserts: 本批之后新增或内容真正发生变化的结果行，已排序。
        deletes: 本批之前存在、之后消失的结果行（携带消失前的值），已排序。
        snapshot: 本批之后的完整快照：外层按分组集合序号，内层按行排序。
        unmatched_retractions: 非严格模式下被忽略的撤回行数（严格模式恒 0）。
        unmatched_by_set: 上面那个数在每个分组集合状态里的分布；由于所有集合
            看到同一批输入行，正常情况下各项相等。
    """

    batch_index: int
    upserts: Tuple[ResultRow, ...]
    deletes: Tuple[ResultRow, ...]
    snapshot: Tuple[Tuple[ResultRow, ...], ...]
    unmatched_retractions: int = 0
    unmatched_by_set: Tuple[int, ...] = ()

    @property
    def changed(self) -> bool:
        """本批是否真的改变了任何结果行。"""

        return bool(self.upserts or self.deletes)

    @property
    def rows(self) -> Tuple[ResultRow, ...]:
        """完整快照拍平，按分组集合顺序再按行顺序。"""

        return tuple(row for block in self.snapshot for row in block)


def snapshot_map(
    snapshot: Sequence[Sequence[ResultRow]],
) -> Dict[Tuple[int, GroupingValues], ResultRow]:
    """把快照索引成 ``{(set_index, grouping_values): row}``。"""

    return {
        (row.set_index, row.grouping_values): row for block in snapshot for row in block
    }


def diff_snapshots(
    before: Sequence[Sequence[ResultRow]],
    after: Sequence[Sequence[ResultRow]],
) -> Tuple[Tuple[ResultRow, ...], Tuple[ResultRow, ...]]:
    """比较两份快照，返回排序后的 ``(upserts, deletes)``。"""

    before_map = snapshot_map(before)
    after_map = snapshot_map(after)
    upserts = [
        row
        for key, row in after_map.items()
        if before_map.get(key) != row
    ]
    deletes = [row for key, row in before_map.items() if key not in after_map]
    upserts.sort(key=lambda row: (row.set_index, sort_key_for(row.grouping_values)))
    deletes.sort(key=lambda row: (row.set_index, sort_key_for(row.grouping_values)))
    return tuple(upserts), tuple(deletes)


def make_row(
    set_index: int,
    grouping_values: GroupingValues,
    grouping_bits: GroupingBits,
    aggregates: AggregateValues,
) -> ResultRow:
    return ResultRow(
        set_index=set_index,
        grouping_values=tuple(grouping_values),
        grouping_bits=tuple(grouping_bits),
        aggregates=tuple(aggregates),
    )


def row_as_mapping(row: ResultRow, grouping_columns: Sequence[str]) -> Mapping[str, Any]:
    """只展开分组列与位图的轻量映射。"""

    out = dict(zip(grouping_columns, row.grouping_values))
    out["__grouping__"] = row.grouping_bits
    return out


__all__ = [
    "BatchResult",
    "GroupingBits",
    "GroupingValues",
    "ResultRow",
    "diff_snapshots",
    "make_row",
    "row_as_mapping",
    "snapshot_map",
    "sort_key_for",
]
