"""分批分组集合引擎。

对外只有 :class:`GroupingSetsEngine` 一个类。它持有全部聚合状态，
每次 :meth:`GroupingSetsEngine.apply` 接收一批"新增行"和"撤回行"，
返回本批真正发生变化的结果行（新增/更新与删除）。

核心不变量
----------

1. 状态按 ``(分组集合序号, 分组键)`` 组织。分组集合序号来自规格列表的位置，
   因此"同一个分组集合列两遍"会得到两份彼此独立的状态、各输出一份结果；
   而一次撤回对每个状态至多扣减一次。
2. 每个分组键下保存的是"行载荷"的多重集合（:class:`collections.Counter`），
   载荷是该行各聚合列的输入值。COUNT / SUM / MIN 每次由载荷多重集重新计算，
   所以撤回、重复最小值、全空聚合列都天然正确，不需要反向聚合。
3. 一批内的新增与撤回先合成净增量，再整体校验、整体提交；任一撤回无法匹配
   （严格模式）时状态保持不变。
4. 结果行身份是 ``(分组集合序号, 分组值元组)``；每批前后逐行比较，
   只有内容真正变化的行才出现在 ``upserts`` / ``deletes`` 里。
5. 空分组集合（总计行）永远输出一行：没有数据时 COUNT 为 0，SUM / MIN 为
   ``None``。非空分组集合的组在最后一行被撤回后从结果中消失。
"""

from __future__ import annotations

import decimal
import math
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from .errors import (
    AggregateTypeError,
    MissingColumnError,
    NonFiniteValueError,
    SchemaError,
    UnmatchedRetractionError,
    UnhashableGroupingValueError,
)
from .model import (
    BatchResult,
    ResultRow,
    diff_snapshots,
    make_row,
    sort_key_for,
)
from .schema import GroupingSetsSpec
from .values import normalize_value

# COUNT(*) 的载荷槽标记：只要行存在就算一行，与数据里的任何值都无关。
_COUNT_STAR = object()

_MISSING = object()


def _repeat(value: Any, multiplicity: int):
    """惰性重复 ``value`` ``multiplicity`` 次（供 ``math.fsum`` 消费）。"""

    for _ in range(multiplicity):
        yield value


def _scaled(value: Any, multiplicity: int) -> Any:
    """``value * multiplicity``；``bool`` 按 ``int`` 处理以免输出变成布尔。"""

    if isinstance(value, bool):
        value = int(value)
    return value * multiplicity


class GroupingSetsEngine:
    """维护分组集合聚合状态、支持分批增删的引擎。

    参数：
        spec: 已校验的 :class:`~grouping_sets.schema.GroupingSetsSpec`。
        strict: 为 True（默认）时，撤回一个不存在的行会抛
            :class:`~grouping_sets.errors.UnmatchedRetractionError`，且整批不生效；
            为 False 时忽略无法匹配的撤回，并把次数记在
            :attr:`BatchResult.unmatched_retractions` 里。

    输入行必须是 ``Mapping``；行里可以含有规格之外的列，会被忽略。
    """

    def __init__(self, spec: GroupingSetsSpec, *, strict: bool = True) -> None:
        if not isinstance(spec, GroupingSetsSpec):
            raise SchemaError("spec must be a GroupingSetsSpec instance")
        self.spec = spec
        self.strict = bool(strict)

        columns = spec.grouping_columns
        self._set_columns: Tuple[Tuple[int, ...], ...] = tuple(
            tuple(spec.column_index(name) for name in grouping_set.columns)
            for grouping_set in spec.grouping_sets
        )
        self._set_bits: Tuple[Tuple[int, ...], ...] = tuple(
            tuple(0 if name in grouping_set.columns else 1 for name in columns)
            for grouping_set in spec.grouping_sets
        )
        self._agg_funcs = tuple(agg.func for agg in spec.aggregates)
        self._agg_sources = tuple(agg.column for agg in spec.aggregates)

        # 每个分组集合一份状态：{(分组键): Counter({载荷: 行数})}
        self._states: List[Dict[Tuple[Any, ...], Counter]] = [
            {} for _ in spec.grouping_sets
        ]
        self._batch_index = -1
        self._snapshot: Tuple[Tuple[ResultRow, ...], ...] = self._build_snapshot(
            self._states
        )

    # -- 只读视图 -----------------------------------------------------

    @property
    def batch_index(self) -> int:
        """已经成功应用的批次数减一；从未 apply 过时为 -1。"""

        return self._batch_index

    def current_snapshot(self) -> Tuple[Tuple[ResultRow, ...], ...]:
        """最近一次成功提交后的完整结果快照。"""

        return self._snapshot

    def current_rows(self) -> Tuple[ResultRow, ...]:
        return tuple(row for block in self._snapshot for row in block)

    def stored_row_counts(self) -> Tuple[int, ...]:
        """每个分组集合状态里当前保存的输入行总数（诊断用）。"""

        return tuple(
            sum(sum(counter.values()) for counter in state.values())
            for state in self._states
        )

    def group_counts(self) -> Tuple[int, ...]:
        """每个分组集合当前的有效分组数（空分组集合无数据时为 0）。"""

        return tuple(
            sum(1 for counter in state.values() if counter) for state in self._states
        )

    def reset(self) -> None:
        """清空所有状态并回到初始快照。"""

        self._states = [{} for _ in self.spec.grouping_sets]
        self._batch_index = -1
        self._snapshot = self._build_snapshot(self._states)

    # -- 主入口 -------------------------------------------------------

    def apply(
        self,
        adds: Iterable[Mapping[str, Any]] = (),
        retracts: Iterable[Mapping[str, Any]] = (),
    ) -> BatchResult:
        """应用一批新增行与撤回行，返回本批变化与最新快照。

        * 同一批里完全相同的增删会相互抵消；
        * 严格模式下，只要有一个撤回在"批前状态 + 本批净新增"里找不到匹配，
          整批拒绝（:class:`UnmatchedRetractionError`），状态不变；
        * 本批执行后逐行比较快照，只有真正变化的行才出现在
          ``upserts`` / ``deletes`` 中；``deletes`` 携带消失前的值。
        """

        if isinstance(adds, Mapping) or isinstance(retracts, Mapping):
            raise SchemaError(
                "apply(adds=..., retracts=...) expects iterables of rows, "
                "not a single mapping; wrap it in a list"
            )
        add_parts = [self._prepare_row(row) for row in adds]
        retract_parts = [self._prepare_row(row) for row in retracts]

        deltas: List[Dict[Tuple[Any, ...], Counter]] = [
            {} for _ in self.spec.grouping_sets
        ]
        for parts, sign in ((add_parts, 1), (retract_parts, -1)):
            for grouping_keys, payload in parts:
                for set_index, key in enumerate(grouping_keys):
                    counter = deltas[set_index].get(key)
                    if counter is None:
                        counter = deltas[set_index][key] = Counter()
                    counter[payload] += sign

        if self.strict:
            self._check_retractions(deltas)

        unmatched = 0
        unmatched_by_set: Tuple[int, ...] = ()
        if not self.strict:
            shortfalls = self._unmatched_shortfalls(deltas)
            unmatched_by_set = tuple(shortfalls)
            # 所有分组集合看到的是同一批输入行，因此缺口数一致；
            # 用最大值汇报"有多少条撤回行被忽略"，避免集合数放大计数。
            unmatched = max(shortfalls, default=0)

        new_states = self._apply_deltas(deltas)
        snapshot = self._build_snapshot(new_states)
        upserts, deletes = diff_snapshots(self._snapshot, snapshot)

        self._states = new_states
        self._snapshot = snapshot
        self._batch_index += 1
        return BatchResult(
            batch_index=self._batch_index,
            upserts=upserts,
            deletes=deletes,
            snapshot=snapshot,
            unmatched_retractions=unmatched,
            unmatched_by_set=unmatched_by_set,
        )

    # 单行便捷入口
    def add(self, row: Mapping[str, Any]) -> BatchResult:
        return self.apply(adds=(row,))

    def retract(self, row: Mapping[str, Any]) -> BatchResult:
        return self.apply(retracts=(row,))

    # -- 输入处理 -----------------------------------------------------

    def _prepare_row(
        self, row: Mapping[str, Any]
    ) -> Tuple[Tuple[Tuple[Any, ...], ...], Tuple[Any, ...]]:
        """把一个输入行变成 (每个分组集合的分组键, 聚合载荷)。"""

        if not isinstance(row, Mapping):
            raise SchemaError(
                "input rows must be mappings, got %s" % type(row).__name__
            )

        grouping_values = []
        for name in self.spec.grouping_columns:
            value = row.get(name, _MISSING)
            if value is _MISSING:
                raise MissingColumnError("input row is missing column %r" % (name,))
            grouping_values.append(self._normalize_grouping_value(name, value))

        grouping_keys = tuple(
            tuple(grouping_values[index] for index in column_indices)
            for column_indices in self._set_columns
        )

        payload = []
        for func, source in zip(self._agg_funcs, self._agg_sources):
            if source == "*":
                payload.append(_COUNT_STAR)
                continue
            value = row.get(source, _MISSING)
            if value is _MISSING:
                raise MissingColumnError("input row is missing column %r" % (source,))
            payload.append(self._normalize_measure(source, value))

        return grouping_keys, tuple(payload)

    @staticmethod
    def _normalize_grouping_value(column: str, value: Any) -> Any:
        try:
            return normalize_value(value)
        except NonFiniteValueError as exc:
            raise NonFiniteValueError(
                "grouping column %r: %s" % (column, exc)
            ) from None
        except UnhashableGroupingValueError as exc:
            raise UnhashableGroupingValueError(
                "grouping column %r: %s" % (column, exc)
            ) from None

    @classmethod
    def _normalize_measure(cls, column: str, value: Any) -> Any:
        """聚合输入的规范化：``None`` 保留，NaN 拒绝，容器递归处理。"""

        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, float):
            if value != value:  # NaN
                raise NonFiniteValueError(
                    "aggregate column %r contains NaN, which has no well-defined "
                    "MIN/ORDER BY semantics" % (column,)
                )
            return value
        if isinstance(value, decimal.Decimal):
            if not value.is_finite():
                raise NonFiniteValueError(
                    "aggregate column %r contains non-finite Decimal %r"
                    % (column, value)
                )
            return value
        if isinstance(value, (list, tuple)):
            return tuple(cls._normalize_measure(column, item) for item in value)
        if isinstance(value, (set, frozenset)):
            return frozenset(cls._normalize_measure(column, item) for item in value)
        if isinstance(value, dict):
            return tuple(
                sorted(
                    (
                        (cls._normalize_measure(column, k), cls._normalize_measure(column, v))
                        for k, v in value.items()
                    ),
                    key=repr,
                )
            )
        try:
            hash(value)
        except TypeError as exc:
            raise AggregateTypeError(
                "aggregate column %r has a value that is not hashable: %r"
                % (column, value)
            ) from exc
        return value

    # -- 撤回校验 -----------------------------------------------------

    def _check_retractions(
        self, deltas: Sequence[Mapping[Tuple[Any, ...], Counter]]
    ) -> None:
        for set_index, bucket in enumerate(deltas):
            state = self._states[set_index]
            for key, counter in bucket.items():
                existing = state.get(key)
                for payload, delta in counter.items():
                    if delta >= 0:
                        continue
                    available = 0 if existing is None else existing.get(payload, 0)
                    if available + delta < 0:
                        raise UnmatchedRetractionError(
                            self._unmatched_message(
                                set_index, key, payload, -delta, available
                            )
                        )

    def _unmatched_shortfalls(
        self, deltas: Sequence[Mapping[Tuple[Any, ...], Counter]]
    ) -> List[int]:
        """非严格模式：统计每个分组集合状态里被忽略的撤回行数。"""

        shortfalls: List[int] = []
        for set_index, bucket in enumerate(deltas):
            state = self._states[set_index]
            total = 0
            for key, counter in bucket.items():
                existing = state.get(key)
                for payload, delta in counter.items():
                    if delta >= 0:
                        continue
                    available = 0 if existing is None else existing.get(payload, 0)
                    shortfall = -(available + delta)
                    if shortfall > 0:
                        total += shortfall
            shortfalls.append(total)
        return shortfalls

    # -- 状态提交 -----------------------------------------------------

    def _apply_deltas(
        self, deltas: Sequence[Mapping[Tuple[Any, ...], Counter]]
    ) -> List[Dict[Tuple[Any, ...], Counter]]:
        new_states: List[Dict[Tuple[Any, ...], Counter]] = []
        for set_index, state in enumerate(self._states):
            copied = dict(state)
            for key, delta in deltas[set_index].items():
                counter = copied.get(key)
                counter = Counter() if counter is None else Counter(counter)
                for payload, n in delta.items():
                    if n == 0:
                        continue
                    counter[payload] += n
                    if counter[payload] <= 0:
                        del counter[payload]
                if counter:
                    copied[key] = counter
                else:
                    copied.pop(key, None)
            new_states.append(copied)
        return new_states

    # -- 快照 ---------------------------------------------------------

    def _build_snapshot(
        self, states: Sequence[Mapping[Tuple[Any, ...], Counter]]
    ) -> Tuple[Tuple[ResultRow, ...], ...]:
        blocks = []
        width = len(self.spec.grouping_columns)
        for set_index, grouping_set in enumerate(self.spec.grouping_sets):
            state = states[set_index]
            bits = self._set_bits[set_index]
            column_indices = self._set_columns[set_index]
            rows = []
            if not grouping_set.columns:
                # 空分组集合永远输出一行，哪怕状态为空。
                counter = state.get(())
                rows.append(
                    make_row(
                        set_index,
                        (None,) * width,
                        bits,
                        self._aggregate_values(
                            counter if counter is not None else Counter()
                        ),
                    )
                )
            else:
                for key in sorted(state, key=sort_key_for):
                    counter = state[key]
                    if not counter:
                        continue
                    rows.append(
                        make_row(
                            set_index,
                            self._expand_key(key, column_indices, width),
                            bits,
                            self._aggregate_values(counter),
                        )
                    )
            blocks.append(tuple(rows))
        return tuple(blocks)

    @staticmethod
    def _expand_key(
        key: Tuple[Any, ...], column_indices: Tuple[int, ...], width: int
    ) -> Tuple[Any, ...]:
        """把分组键展开成全部分组列宽度，被省略的列填 ``None``。

        这些 ``None`` 就是"小计空值"；它们与数据 NULL 的区分完全由位图给出。
        """

        out: List[Any] = [None] * width
        for index, value in zip(column_indices, key):
            out[index] = value
        return tuple(out)

    def _aggregate_values(self, counter: Counter) -> Tuple[Any, ...]:
        """由载荷多重集重新计算本行所有聚合值。

        每个聚合槽先收集 ``(值, 重数)`` 对（跳过空值），再按函数归约：

        * COUNT 是重数之和；
        * SUM 对整数 / ``Decimal`` 用 ``值 * 重数``，对浮点用 ``math.fsum``
          （按重数展开迭代，但惰性展开，不额外占内存）；
        * MIN 只关心去重后的值，重数无关。

        因此"一行数据出现 N 次"不需要展开成 N 个元素，也不会因为 N 很大而爆内存。
        """

        if not counter:
            # 无数据：COUNT 为 0，SUM / MIN 为空（SQL 语义）。
            return tuple(0 if func == "count" else None for func in self._agg_funcs)

        per_slot: List[List[Tuple[Any, int]]] = []
        for slot in range(len(self._agg_funcs)):
            pairs: List[Tuple[Any, int]] = []
            for payload, multiplicity in counter.items():
                value = payload[slot]
                if value is None:
                    continue
                pairs.append((value, multiplicity))
            per_slot.append(pairs)

        out = []
        for func, pairs in zip(self._agg_funcs, per_slot):
            if func == "count":
                out.append(sum(multiplicity for _, multiplicity in pairs))
            elif func == "sum":
                out.append(self._sum(pairs))
            elif func == "min":
                out.append(self._min(pairs))
            else:  # pragma: no cover - schema 已校验
                raise SchemaError("unsupported aggregate function: %r" % (func,))
        return tuple(out)

    @staticmethod
    def _sum(pairs: Sequence[Tuple[Any, int]]) -> Any:
        if not pairs:
            return None
        has_float = any(type(value) is float for value, _ in pairs)
        has_decimal = any(isinstance(value, decimal.Decimal) for value, _ in pairs)
        if has_float and has_decimal:
            raise AggregateTypeError("cannot SUM a mix of float and Decimal values")
        if has_float:
            try:
                return math.fsum(
                    item
                    for value, multiplicity in pairs
                    for item in _repeat(value, multiplicity)
                )
            except TypeError as exc:
                raise AggregateTypeError("cannot SUM values: %r" % (exc,)) from exc
        if has_decimal:
            total = decimal.Decimal(0)
            try:
                for value, multiplicity in pairs:
                    total += _scaled(value, multiplicity)
            except (TypeError, decimal.DecimalException) as exc:
                raise AggregateTypeError("cannot SUM values: %r" % (exc,)) from exc
            return total
        total = 0
        try:
            for value, multiplicity in pairs:
                total += _scaled(value, multiplicity)
        except TypeError as exc:
            raise AggregateTypeError("cannot SUM values: %r" % (exc,)) from exc
        return total

    @staticmethod
    def _min(pairs: Sequence[Tuple[Any, int]]) -> Any:
        if not pairs:
            return None
        try:
            # 重数不影响最小值，只看去重后的值。
            return min(value for value, _ in pairs)
        except TypeError as exc:
            raise AggregateTypeError(
                "cannot MIN over mixed value types: %r" % (exc,)
            ) from exc

    # -- 错误信息 -----------------------------------------------------

    def _unmatched_message(
        self,
        set_index: int,
        key: Tuple[Any, ...],
        payload: Tuple[Any, ...],
        wanted: int,
        available: int,
    ) -> str:
        grouping_set = self.spec.grouping_sets[set_index]
        columns = ", ".join(grouping_set.columns) if grouping_set.columns else "<empty>"
        return (
            "cannot retract row from grouping set #%d (%s) with key %r: "
            "wanted %d matching row(s), only %d available"
            % (set_index, columns, key, wanted, available)
        )


def rows_as_dicts(
    spec: GroupingSetsSpec, rows: Iterable[ResultRow]
) -> List[Dict[str, Any]]:
    """把结果行展开成 ``{列名: 值}`` 列表（含位图与集合序号，便于断言）。"""

    return [row.as_dict(spec.grouping_columns, spec.aggregate_outputs) for row in rows]


def format_rows(spec: GroupingSetsSpec, rows: Iterable[ResultRow]) -> str:
    """把结果行格式化成对齐文本表，供示例脚本与调试使用。"""

    rows = list(rows)
    headers = list(spec.output_columns) + ["grouping", "set"]
    body = []
    for row in rows:
        cells = [repr(value) for value in row.grouping_values]
        cells += [repr(value) for value in row.aggregates]
        cells += [str(row.grouping), str(row.set_index)]
        body.append(cells)
    widths = [len(head) for head in headers]
    for cells in body:
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(head.ljust(widths[i]) for i, head in enumerate(headers))]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for cells in body:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)))
    return "\n".join(lines)
