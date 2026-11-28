"""去标识日期映射器：按人整体平移事件日期，满足发布间隔约束。

典型用法
--------

>>> mapper = DateMapper(
...     event_dates={1: {"birth": date(2000, 1, 1), "sample": date(2020, 5, 1)}},
...     offset_bounds={1: (-30, 30)},
...     proposed_offsets={1: 0},
...     gap_constraints=[
...         GapConstraint((1, "birth"), (1, "sample"), min_gap_days=0),
...     ],
... )
>>> plan = mapper.finalize()
>>> mapper.map_event(1, "sample")  # 定稿后可任意重复、分批调用

同一份定稿计划可通过 :meth:`DateMapper.to_plan_dict` /
:meth:`DateMapper.from_plan_dict` 在进程间传递，保证分批导出时相同事件
始终得到同一天。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .solver import Edge, InfeasibleError, SolveResult, ZERO, solve_offsets

PLAN_FORMAT_VERSION = 1

EventRef = Tuple[Any, Any]


@dataclass(frozen=True)
class GapConstraint:
    """一对事件在发布数据中的间隔约束。

    要求 ``min_gap_days <= (日期(b_event) - 日期(a_event)) <= max_gap_days``，
    天数按日历日差计算。min/max 任一端为 None 表示该侧无限制。
    """

    a_event: EventRef
    b_event: EventRef
    min_gap_days: Optional[int] = None
    max_gap_days: Optional[int] = None

    def __post_init__(self) -> None:
        if self.min_gap_days is None and self.max_gap_days is None:
            raise ValueError("间隔约束至少要给出 min_gap_days 或 max_gap_days")
        if self.min_gap_days is not None and not _is_real_int(self.min_gap_days):
            raise TypeError("间隔天数必须是整数")
        if self.max_gap_days is not None and not _is_real_int(self.max_gap_days):
            raise TypeError("间隔天数必须是整数")
        if (
            self.min_gap_days is not None
            and self.max_gap_days is not None
            and self.min_gap_days > self.max_gap_days
        ):
            raise ValueError("min_gap_days 不能大于 max_gap_days")


def _is_real_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _to_ordinal(value: Any) -> int:
    if _is_real_int(value):
        return value
    if isinstance(value, _dt.date):
        return value.toordinal()
    raise TypeError("事件日期必须是 datetime.date 或整数（序数日）")


class DateMapper:
    """按人整体平移事件日期的去标识映射器。

    参数
    ----
    event_dates: ``{人号: {事件号: 日期}}``，日期取 ``datetime.date`` 或整数。
    offset_bounds: ``{人号: (允许的最小偏移, 允许的最大偏移)}``，整数天，闭区间。
    proposed_offsets: ``{人号: 原先拟用的偏移}``，用于最小化最大偏移改动。
    gap_constraints: 发布后事件对的最小/最大允许间隔（天）。
    """

    def __init__(
        self,
        event_dates: Mapping[Any, Mapping[Any, Any]],
        offset_bounds: Mapping[Any, Tuple[int, int]],
        proposed_offsets: Mapping[Any, int],
        gap_constraints: Sequence[GapConstraint] = (),
    ) -> None:
        self._event_ordinals: Dict[Any, Dict[Any, int]] = {}
        self._event_date_kind: Dict[EventRef, str] = {}

        for person, events in event_dates.items():
            if not isinstance(events, Mapping):
                raise TypeError(f"人 {person!r} 的事件必须是映射")
            bucket: Dict[Any, int] = {}
            for event_id, day in events.items():
                ordinal = _to_ordinal(day)
                if event_id in bucket:
                    raise ValueError(f"人 {person!r} 的事件 {event_id!r} 重复")
                bucket[event_id] = ordinal
                self._event_date_kind[(person, event_id)] = (
                    "date" if isinstance(day, _dt.date) else "int"
                )
            self._event_ordinals[person] = bucket

        persons = list(self._event_ordinals.keys())
        person_set = set(persons)

        self._lower: Dict[Any, int] = {}
        self._upper: Dict[Any, int] = {}
        for person in persons:
            if person not in offset_bounds:
                raise ValueError(f"缺少人 {person!r} 的偏移范围")
            bounds = offset_bounds[person]
            if not isinstance(bounds, tuple) or len(bounds) != 2:
                raise TypeError(f"人 {person!r} 的偏移范围必须是 (下界, 上界)")
            low, high = bounds
            if not (_is_real_int(low) and _is_real_int(high)):
                raise TypeError("偏移边界必须是整数")
            if low > high:
                raise ValueError(f"人 {person!r} 的偏移下界大于上界")
            self._lower[person] = low
            self._upper[person] = high

        extra_bound_people = set(offset_bounds) - person_set
        if extra_bound_people:
            raise ValueError(f"偏移范围引用了没有事件的人: {sorted(map(repr, extra_bound_people))}")

        self._proposed: Dict[Any, int] = {}
        for person in persons:
            if person not in proposed_offsets:
                raise ValueError(f"缺少人 {person!r} 的拟用偏移")
            value = proposed_offsets[person]
            if not _is_real_int(value):
                raise TypeError("拟用偏移必须是整数")
            self._proposed[person] = value
        extra_proposed = set(proposed_offsets) - person_set
        if extra_proposed:
            raise ValueError(f"拟用偏移引用了没有事件的人: {sorted(map(repr, extra_proposed))}")

        self._constraints: List[GapConstraint] = list(gap_constraints)
        for constraint in self._constraints:
            for ref in (constraint.a_event, constraint.b_event):
                if ref not in self._event_date_kind:
                    raise ValueError(f"间隔约束引用了未知事件 {ref!r}")

        self._offsets: Optional[Dict[Any, int]] = None
        self._max_change: Optional[int] = None

    # ------------------------------------------------------------------ #
    # 求解
    # ------------------------------------------------------------------ #
    def _build_base_edges(self) -> List[Edge]:
        persons = list(self._event_ordinals.keys())
        person_to_node = {person: index + 1 for index, person in enumerate(persons)}
        edges: List[Edge] = []

        for person in persons:
            node = person_to_node[person]
            edges.append(Edge(ZERO, node, self._upper[person], ("bound", person, "upper")))
            edges.append(Edge(node, ZERO, -self._lower[person], ("bound", person, "lower")))

        for index, constraint in enumerate(self._constraints):
            pa, ea = constraint.a_event
            pb, eb = constraint.b_event
            da = self._event_ordinals[pa][ea]
            db = self._event_ordinals[pb][eb]
            node_a = person_to_node[pa]
            node_b = person_to_node[pb]
            origin = ("gap", index)

            # (db + x_b) - (da + x_a) >= min  =>  x_a - x_b <= db - da - min
            if constraint.min_gap_days is not None:
                edges.append(
                    Edge(
                        node_b,
                        node_a,
                        db - da - constraint.min_gap_days,
                        origin,
                    )
                )
            # (db + x_b) - (da + x_a) <= max  =>  x_b - x_a <= da - db + max
            if constraint.max_gap_days is not None:
                edges.append(
                    Edge(
                        node_a,
                        node_b,
                        da - db + constraint.max_gap_days,
                        origin,
                    )
                )
        return edges

    def finalize(self) -> "FinalizedPlan":
        """求解并定稿；无解时抛 :class:`InfeasibleError`。

        重复调用是幂等的，始终返回同一份定稿计划。
        """

        if self._offsets is None:
            persons = list(self._event_ordinals.keys())
            result: SolveResult = solve_offsets(
                persons=persons,
                lower_bounds=self._lower,
                upper_bounds=self._upper,
                proposed=self._proposed,
                base_edges=self._build_base_edges(),
            )
            self._offsets = dict(result.offsets)
            self._max_change = result.max_change
        return self.plan()

    # ------------------------------------------------------------------ #
    # 定稿后的查询与分批导出
    # ------------------------------------------------------------------ #
    def plan(self) -> "FinalizedPlan":
        self._require_finalized()
        return FinalizedPlan(
            offsets=dict(self._offsets),
            max_change=self._max_change,
            event_ordinals={p: dict(e) for p, e in self._event_ordinals.items()},
            event_date_kind=dict(self._event_date_kind),
        )

    def _require_finalized(self) -> None:
        if self._offsets is None:
            raise RuntimeError("映射尚未定稿，请先调用 finalize()")

    def map_event_ordinal(self, person: Any, event_id: Any) -> int:
        """返回事件发布日期的整数序数（原始序数 + 该人定稿偏移）。"""

        self._require_finalized()
        try:
            return self._event_ordinals[person][event_id] + self._offsets[person]
        except KeyError:
            raise KeyError(f"未知事件 {(person, event_id)!r}") from None

    def map_event(self, person: Any, event_id: Any) -> Any:
        """返回事件的发布日期；输入是 date 则返回 date，输入是整数则返回整数。

        同一份定稿下无论调用多少次、分多少批导出，相同事件必然得到同一天。
        """

        self._require_finalized()
        ordinal = self.map_event_ordinal(person, event_id)
        if self._event_date_kind[(person, event_id)] == "date":
            return _dt.date.fromordinal(ordinal)
        return ordinal

    def map_all(self) -> Dict[Any, Dict[Any, Any]]:
        """一次性返回全部事件的发布日期，类型与各自输入保持一致。"""

        self._require_finalized()
        return {
            person: {
                event_id: self.map_event(person, event_id)
                for event_id in self._event_ordinals[person]
            }
            for person in self._event_ordinals
        }

    @property
    def offsets(self) -> Dict[Any, int]:
        self._require_finalized()
        return dict(self._offsets)

    @property
    def max_change(self) -> int:
        self._require_finalized()
        return self._max_change

    # ------------------------------------------------------------------ #
    # 跨进程/分批导出：定稿计划的序列化与装载
    # ------------------------------------------------------------------ #
    def to_plan_dict(self) -> Dict[str, Any]:
        """把定稿计划导出为纯 JSON 兼容字典。"""

        self._require_finalized()
        return {
            "format_version": PLAN_FORMAT_VERSION,
            "offsets": [[person, offset] for person, offset in self._offsets.items()],
            "max_change": self._max_change,
            "events": [
                [
                    person,
                    [
                        [event_id, ordinal, self._event_date_kind[(person, event_id)]]
                        for event_id, ordinal in events.items()
                    ],
                ]
                for person, events in self._event_ordinals.items()
            ],
        }

    @classmethod
    def from_plan_dict(cls, plan: Mapping[str, Any]) -> "DateMapper":
        """只凭定稿字典重建映射器，用于后续批次导出。

        重建后的映射器已处于定稿状态，对相同事件给出与首批完全一致的日期，
        不依赖原始输入，也不会重新求解。
        """

        if not isinstance(plan, Mapping):
            raise TypeError("计划必须是映射对象")
        if plan.get("format_version") != PLAN_FORMAT_VERSION:
            raise ValueError("不支持的计划格式版本")

        event_dates: Dict[Any, Dict[Any, Any]] = {}
        kinds: Dict[EventRef, str] = {}
        for person, event_list in plan["events"]:
            bucket: Dict[Any, Any] = {}
            for event_id, ordinal, kind in event_list:
                if not _is_real_int(ordinal):
                    raise TypeError("计划中的事件序数必须是整数")
                if kind == "date":
                    bucket[event_id] = _dt.date.fromordinal(ordinal)
                elif kind == "int":
                    bucket[event_id] = ordinal
                else:
                    raise ValueError(f"未知的日期类型标记 {kind!r}")
                kinds[(person, event_id)] = kind
            event_dates[person] = bucket

        offsets_raw = plan["offsets"]
        offsets = {person: offset for person, offset in offsets_raw}

        mapper = cls.__new__(cls)
        mapper._event_ordinals = {
            person: {event_id: _to_ordinal(day) for event_id, day in events.items()}
            for person, events in event_dates.items()
        }
        mapper._event_date_kind = kinds
        # 装载的计划不再参与求解，边界/拟用值仅作占位，不参与任何计算。
        mapper._lower = {person: offset for person, offset in offsets.items()}
        mapper._upper = {person: offset for person, offset in offsets.items()}
        mapper._proposed = {person: offset for person, offset in offsets.items()}
        mapper._constraints = []
        mapper._offsets = dict(offsets)
        mapper._max_change = plan["max_change"]
        return mapper


@dataclass(frozen=True)
class FinalizedPlan:
    """定稿计划的只读视图。"""

    offsets: Dict[Any, int]
    max_change: int
    event_ordinals: Dict[Any, Dict[Any, int]]
    event_date_kind: Dict[EventRef, str]
