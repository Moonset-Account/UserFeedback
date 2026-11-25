"""规格（schema）解析：分组列、分组集合、聚合列。

规格在构造时一次性校验并冻结；之后所有批次复用同一份规格，
因此调用方可以安全地在多批、多引擎之间共享 ``GroupingSetsSpec`` 对象。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

from .errors import DuplicateColumnError, SchemaError, UnknownColumnError

# 支持的聚合函数。
AGGREGATE_FUNCS = ("count", "sum", "min")

# COUNT 允许的"整行"源列：COUNT(*) 只数行，不读任何数据列。
COUNT_STAR = "*"


@dataclass(frozen=True)
class AggregateColumn:
    """一个聚合列的定义。

    Attributes:
        func: ``"count"`` / ``"sum"`` / ``"min"``，大小写与首尾空格不敏感。
        column: 输入行里的源列名；``"*"`` 仅对 COUNT 合法。
        output: 输出行里的列名。

    注意：``column`` 不需要出现在 ``grouping_columns`` 里——聚合源列
    与分组列是两套列。
    """

    func: str
    column: str
    output: str

    def __post_init__(self) -> None:
        if self.func not in AGGREGATE_FUNCS:
            raise SchemaError("unsupported aggregate function: %r" % (self.func,))
        if not isinstance(self.column, str) or not self.column:
            raise SchemaError("aggregate source column must be a non-empty string")
        if not isinstance(self.output, str) or not self.output:
            raise SchemaError("aggregate output column must be a non-empty string")
        if self.column == COUNT_STAR and self.func != "count":
            raise SchemaError(
                "aggregate source '*' is only allowed for COUNT, not %r" % (self.func,)
            )


@dataclass(frozen=True)
class GroupingSet:
    """一个分组集合。

    Attributes:
        columns: 按规格顺序排列的列名元组；空元组表示空分组集合（总计行）。
        label: 便于调试的标签，不参与语义、不参与去重。
    """

    columns: Tuple[str, ...]
    label: str = ""

    @property
    def is_grand_total(self) -> bool:
        return not self.columns


class GroupingSetsSpec:
    """已校验、已冻结的分组集合规格。

    参数：
        grouping_columns: 全部可选的分组列，顺序即输出列顺序。
        grouping_sets: 每个元素是一个分组集合（列名序列）。允许重复出现同样的
            集合——它们各自独立维护状态、各输出一份结果。
        aggregates: 聚合列定义；``AggregateColumn`` 或
            ``(func, column)`` / ``(func, column, output)`` 元组。
        include_grand_total: 为 True 时在末尾自动追加一个空分组集合。

    校验（任一失败抛 :class:`~grouping_sets.errors.SchemaError` 子类）：

    * 分组列名必须是互不相同的非空字符串；
    * 每个分组集合必须只引用已声明的分组列；
    * 同一个分组集合内不允许重复列（重复列不增加语义，只会制造歧义）；
    * 聚合函数只能是 count / sum / min；``*`` 只对 count 合法；
    * 聚合输出列名全局唯一，且不得与分组列重名。
    """

    __slots__ = (
        "grouping_columns",
        "grouping_sets",
        "aggregates",
        "_column_index",
        "_aggregate_outputs",
    )

    def __init__(
        self,
        grouping_columns: Sequence[str],
        grouping_sets: Iterable[Sequence[str]] = (),
        aggregates: Iterable = (),
        *,
        include_grand_total: bool = False,
    ) -> None:
        columns = self._normalize_grouping_columns(grouping_columns)
        sets = self._normalize_grouping_sets(grouping_sets, columns)
        if include_grand_total:
            sets = sets + (GroupingSet(columns=(), label="grand-total"),)
        aggs = self._normalize_aggregates(aggregates, columns)

        self.grouping_columns: Tuple[str, ...] = columns
        self.grouping_sets: Tuple[GroupingSet, ...] = sets
        self.aggregates: Tuple[AggregateColumn, ...] = aggs
        self._column_index = {name: i for i, name in enumerate(columns)}
        self._aggregate_outputs: Tuple[str, ...] = tuple(a.output for a in aggs)

    # -- 属性 ---------------------------------------------------------

    @property
    def aggregate_outputs(self) -> Tuple[str, ...]:
        return self._aggregate_outputs

    @property
    def output_columns(self) -> Tuple[str, ...]:
        """结果行的列顺序：分组列（按规格顺序）+ 聚合输出列。"""

        return self.grouping_columns + self._aggregate_outputs

    def column_index(self, name: str) -> int:
        try:
            return self._column_index[name]
        except KeyError:
            raise UnknownColumnError("unknown grouping column: %r" % (name,)) from None

    # -- 校验 ---------------------------------------------------------

    @staticmethod
    def _normalize_grouping_columns(grouping_columns: Sequence[str]) -> Tuple[str, ...]:
        if isinstance(grouping_columns, (str, bytes)):
            raise SchemaError(
                "grouping_columns must be a sequence of names, not a string"
            )
        out = []
        seen = set()
        for name in grouping_columns:
            if not isinstance(name, str) or not name:
                raise SchemaError("grouping column names must be non-empty strings")
            if name in seen:
                raise DuplicateColumnError("duplicate grouping column: %r" % (name,))
            seen.add(name)
            out.append(name)
        return tuple(out)

    @staticmethod
    def _normalize_grouping_sets(
        grouping_sets: Iterable[Sequence[str]], columns: Tuple[str, ...]
    ) -> Tuple[GroupingSet, ...]:
        allowed = set(columns)
        result = []
        for raw in grouping_sets:
            if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
                raise SchemaError(
                    "each grouping set must be a sequence of column names, got %r"
                    % (raw,)
                )
            seen = set()
            for name in raw:
                if not isinstance(name, str):
                    raise SchemaError(
                        "grouping set entries must be strings: %r" % (raw,)
                    )
                if name not in allowed:
                    raise UnknownColumnError(
                        "grouping set references unknown column: %r" % (name,)
                    )
                if name in seen:
                    raise DuplicateColumnError(
                        "column %r appears twice in one grouping set" % (name,)
                    )
                seen.add(name)
            result.append(GroupingSet(columns=tuple(raw)))
        return tuple(result)

    @staticmethod
    def _normalize_aggregates(
        aggregates: Iterable, columns: Tuple[str, ...]
    ) -> Tuple[AggregateColumn, ...]:
        out = []
        seen_outputs = set()
        for raw in aggregates:
            agg = coerce_aggregate(raw)
            if agg.output in seen_outputs:
                raise DuplicateColumnError(
                    "duplicate aggregate output column: %r" % (agg.output,)
                )
            if agg.output in columns:
                raise SchemaError(
                    "aggregate output %r collides with a grouping column"
                    % (agg.output,)
                )
            seen_outputs.add(agg.output)
            out.append(agg)
        return tuple(out)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "GroupingSetsSpec(grouping_columns=%r, grouping_sets=%r, aggregates=%r)" % (
            self.grouping_columns,
            tuple(gs.columns for gs in self.grouping_sets),
            self.aggregates,
        )


def coerce_aggregate(raw: object) -> AggregateColumn:
    if isinstance(raw, AggregateColumn):
        return raw
    if isinstance(raw, (str, bytes)):
        raise SchemaError(
            "aggregate definition must not be a bare string: %r" % (raw,)
        )
    if isinstance(raw, Sequence):
        if len(raw) == 2:
            return _build_aggregate(raw[0], raw[1], None)
        if len(raw) == 3:
            return _build_aggregate(raw[0], raw[1], raw[2])
    raise SchemaError(
        "aggregate definition must be AggregateColumn or a 2/3-element sequence, got %r"
        % (raw,)
    )


def _build_aggregate(func: object, column: object, output: object) -> AggregateColumn:
    if not isinstance(func, str):
        raise SchemaError("aggregate function must be a string: %r" % (func,))
    norm = func.strip().lower()
    if norm not in AGGREGATE_FUNCS:
        raise SchemaError(
            "unsupported aggregate function %r; expected one of %s"
            % (func, ", ".join(AGGREGATE_FUNCS))
        )
    if not isinstance(column, str) or not column:
        raise SchemaError("aggregate source column must be a non-empty string")
    if output is None:
        output = column
    if not isinstance(output, str) or not output:
        raise SchemaError("aggregate output column must be a non-empty string")
    return AggregateColumn(func=norm, column=column, output=output)


def normalize_aggregate_specs(
    aggregates: Optional[Iterable],
) -> Tuple[AggregateColumn, ...]:
    """便利函数：把聚合定义规范化成元组（不做与分组列的冲突校验）。"""

    if aggregates is None:
        return ()
    return tuple(coerce_aggregate(raw) for raw in aggregates)
