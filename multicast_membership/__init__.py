"""多播组成员管理模块（IGMPv3/MLDv2 风格的 INCLUDE/EXCLUDE 过滤聚合）。

场景：同一接口上的多个接收者订阅同一个组播组，各自携带完整的源过滤条件：

* INCLUDE 模式：只接收显式列出的源；
* EXCLUDE 模式：接收除列出源之外的全部源。

本模块负责：

1. 保存每个 ``(接收者, 接口, 组)`` 的完整过滤条件、记录级到期时刻、
   逐源到期时刻以及离开事件；
2. 把同一 ``(接口, 组)`` 上所有接收者的条件聚合为一条上游订阅，
   结果仍然表达为 INCLUDE/EXCLUDE 模式，且仅当模式或源集合确实变化时
   才产生一条 ``SubscriptionUpdate``；
3. 数据到达时，根据各接收者自己的过滤条件选出本地真正想接收该源的接收者；
4. 到期处理分两阶段：先统一移除所有到期项，再为每个受影响的
   ``(接口, 组)`` 发布至多一条净变化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Hashable, Iterable, Mapping, Optional

__all__ = [
    "FilterMode",
    "Filter",
    "SubscriptionUpdate",
    "MembershipManager",
]


class FilterMode(Enum):
    """源过滤模式。"""

    INCLUDE = "include"
    EXCLUDE = "exclude"


@dataclass(frozen=True)
class Filter:
    """一个接收者对某个 ``(接口, 组)`` 的完整源过滤条件。

    属性：
        mode: INCLUDE 表示只收 ``sources`` 中的源；
            EXCLUDE 表示收除 ``sources`` 外的全部源。
        sources: 源集合（不可变）。
        expires_at: 整条记录的到期时刻；``None`` 表示无记录级定时器。
        source_expiry: 逐源到期时刻映射 ``源 -> 时刻``，键必须属于
            ``sources``；到期后只移除对应源，记录本身保留。
    """

    mode: FilterMode
    sources: frozenset = frozenset()
    expires_at: Optional[float] = None
    source_expiry: Mapping[Hashable, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.mode, FilterMode):
            raise TypeError("mode 必须是 FilterMode 枚举成员")
        if not isinstance(self.sources, frozenset):
            object.__setattr__(self, "sources", frozenset(self.sources))
        expiry = dict(self.source_expiry)
        unknown = set(expiry) - set(self.sources)
        if unknown:
            raise ValueError(
                "source_expiry 中存在不属于 sources 的源: "
                f"{sorted(unknown, key=str)!r}"
            )
        object.__setattr__(self, "source_expiry", expiry)

    def wants(self, source: Hashable) -> bool:
        """该过滤条件是否想接收给定源。"""
        if self.mode is FilterMode.INCLUDE:
            return source in self.sources
        return source not in self.sources


@dataclass(frozen=True)
class SubscriptionUpdate:
    """发往上游的一条订阅更新。

    ``withdrawn`` 为 ``True`` 表示该 ``(接口, 组)`` 已无任何接收者，
    上游应撤销订阅；此时 ``mode``/``sources`` 携带撤销前的最后状态。
    """

    interface: Hashable
    group: Hashable
    mode: FilterMode
    sources: frozenset
    withdrawn: bool = False


@dataclass
class _Record:
    """内部可变记录：一个接收者在一个 (接口, 组) 上的成员状态。"""

    receiver: Hashable
    interface: Hashable
    group: Hashable
    mode: FilterMode
    sources: frozenset
    expires_at: Optional[float]
    source_expiry: dict  # source -> expiry time

    def wants(self, source: Hashable) -> bool:
        """该接收者记录自身是否想接收给定源。"""
        if self.mode is FilterMode.INCLUDE:
            return source in self.sources
        return source not in self.sources


class MembershipManager:
    """成员关系管理器。

    所有时间参数均为抽象逻辑时钟（测试中可直接用 int/float 虚拟时钟），
    只要求相互之间可比较，模块不接触真实系统时间。
    """

    def __init__(self) -> None:
        # (receiver, interface, group) -> _Record
        self._records: dict[tuple[Hashable, Hashable, Hashable], _Record] = {}
        # (interface, group) -> 当前已发布给上游的 Filter（定时器字段为空）
        self._upstream: dict[tuple[Hashable, Hashable], Filter] = {}
        # 历次产生的订阅更新日志
        self._updates: list[SubscriptionUpdate] = []

    # ------------------------------------------------------------------ #
    # 输入：提交完整过滤条件 / 离开事件
    # ------------------------------------------------------------------ #
    def submit_filter(
        self,
        receiver: Hashable,
        interface: Hashable,
        group: Hashable,
        mode: FilterMode,
        sources: Optional[Iterable] = None,
        expires_at: Optional[float] = None,
        source_expiry: Optional[Mapping[Hashable, float]] = None,
    ) -> Optional[SubscriptionUpdate]:
        """提交（整体替换）某接收者在某 ``(接口, 组)`` 上的完整过滤条件。

        每次提交都是完整状态，而不是增量补丁。若新状态与现有状态完全
        相同（含定时器），不产生更新并返回 ``None``。

        ``sources`` 省略时，若给出了 ``source_expiry``，则源集合取其键。
        返回因本次提交产生的订阅更新；聚合结果未变时返回 ``None``。
        """
        if not isinstance(mode, FilterMode):
            raise TypeError("mode 必须是 FilterMode 枚举成员")
        expiry_map = dict(source_expiry) if source_expiry is not None else {}
        if sources is None:
            source_set = frozenset(expiry_map)
        else:
            source_set = frozenset(sources)
        unknown = set(expiry_map) - set(source_set)
        if unknown:
            raise ValueError(
                "source_expiry 中存在不属于 sources 的源: "
                f"{sorted(unknown, key=str)!r}"
            )

        key = (receiver, interface, group)
        new_record = _Record(
            receiver=receiver,
            interface=interface,
            group=group,
            mode=mode,
            sources=source_set,
            expires_at=expires_at,
            source_expiry=expiry_map,
        )
        old_record = self._records.get(key)
        if old_record is not None and self._record_equals(old_record, new_record):
            return None
        self._records[key] = new_record
        return self._republish(interface, group)

    def leave(
        self,
        receiver: Hashable,
        interface: Hashable,
        group: Hashable,
    ) -> Optional[SubscriptionUpdate]:
        """处理接收者的离开事件：删除其在该 ``(接口, 组)`` 上的整条记录。

        接收者本就不存在时返回 ``None``；否则返回由此产生的订阅更新，
        聚合结果未变时同样返回 ``None``。
        """
        key = (receiver, interface, group)
        if key not in self._records:
            return None
        del self._records[key]
        return self._republish(interface, group)

    # ------------------------------------------------------------------ #
    # 到期处理：先统一移除，再发布净变化
    # ------------------------------------------------------------------ #
    def advance(self, now: float) -> list[SubscriptionUpdate]:
        """把逻辑时钟推进到 ``now``，处理所有 ``expires_at <= now`` 的到期项。

        处理分两阶段：

        1. 统一扫描并移除全部到期项——记录级定时器到期则删除整条记录；
           逐源定时器到期则只从该记录中移除对应源（记录保留，
           INCLUDE 可能因此变成空集合）；
        2. 对所有受影响的 ``(接口, 组)`` 重新聚合并发布净变化，
           每个 ``(接口, 组)`` 至多产生一条更新；净变化为空则不产生。

        返回本次实际发布的更新列表（可能为空）。
        """
        affected: set[tuple[Hashable, Hashable]] = set()

        # 阶段一：统一移除所有到期项
        for key, record in list(self._records.items()):
            if record.expires_at is not None and record.expires_at <= now:
                del self._records[key]
                affected.add((record.interface, record.group))
                continue

            expired_sources = {
                source
                for source, deadline in record.source_expiry.items()
                if deadline <= now
            }
            if expired_sources:
                record.sources = frozenset(
                    record.sources - expired_sources
                )
                for source in expired_sources:
                    del record.source_expiry[source]
                affected.add((record.interface, record.group))

        # 阶段二：逐 (接口, 组) 发布净变化，顺序确定
        updates: list[SubscriptionUpdate] = []
        for interface, group in sorted(affected, key=_sort_pair):
            update = self._republish(interface, group)
            if update is not None:
                updates.append(update)
        return updates

    # ------------------------------------------------------------------ #
    # 查询：上游订阅状态与本地投递判定
    # ------------------------------------------------------------------ #
    def upstream_filter(
        self, interface: Hashable, group: Hashable
    ) -> Optional[Filter]:
        """返回当前发布给上游的聚合过滤条件；无接收者时返回 ``None``。"""
        return self._upstream.get((interface, group))

    def upstream_wants(
        self, interface: Hashable, group: Hashable, source: Hashable
    ) -> bool:
        """上游订阅是否想收某个 ``(接口, 组, 源)`` 的流量。"""
        aggregated = self._upstream.get((interface, group))
        if aggregated is None:
            return False
        return aggregated.wants(source)

    def receivers_for(
        self, interface: Hashable, group: Hashable, source: Hashable
    ) -> tuple:
        """数据到达时，返回本地真正想接收该源的接收者元组。

        结果按接收者的 ``str`` 排序，保证确定顺序；无人想收时返回空元组。
        判定只依据各接收者自己的过滤条件，与上游聚合状态无关。
        """
        wanted = [
            record.receiver
            for record in self._records.values()
            if record.interface == interface
            and record.group == group
            and record.wants(source)
        ]
        return tuple(sorted(wanted, key=str))

    def record_filter(
        self,
        receiver: Hashable,
        interface: Hashable,
        group: Hashable,
    ) -> Optional[Filter]:
        """返回某接收者当前的完整过滤条件（含定时器）；不存在返回 ``None``。"""
        record = self._records.get((receiver, interface, group))
        if record is None:
            return None
        return Filter(
            mode=record.mode,
            sources=record.sources,
            expires_at=record.expires_at,
            source_expiry=dict(record.source_expiry),
        )

    @property
    def updates(self) -> tuple[SubscriptionUpdate, ...]:
        """历次订阅更新日志的只读副本。"""
        return tuple(self._updates)

    def pop_updates(self) -> list[SubscriptionUpdate]:
        """取出并清空订阅更新日志。"""
        drained = self._updates
        self._updates = list()
        return drained

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    @staticmethod
    def _record_equals(left: _Record, right: _Record) -> bool:
        return (
            left.mode is right.mode
            and left.sources == right.sources
            and left.expires_at == right.expires_at
            and left.source_expiry == right.source_expiry
        )

    def _aggregate(
        self, interface: Hashable, group: Hashable
    ) -> Optional[Filter]:
        """聚合某 (接口, 组) 上全部接收者的过滤条件。

        规则（等价于 IGMPv3/MLDv2 的组记录合并）：

        * 没有任何接收者 -> ``None``（上游应撤销）；
        * 全部为 INCLUDE -> ``INCLUDE(各源集合之并)``；
        * 至少一个 EXCLUDE ->
          ``EXCLUDE(各 EXCLUDE 源集合之交 - 各 INCLUDE 源集合之并)``。
        """
        records = [
            record
            for record in self._records.values()
            if record.interface == interface and record.group == group
        ]
        if not records:
            return None

        include_union: set = set()
        exclude_intersection: Optional[set] = None
        has_exclude = False

        for record in records:
            if record.mode is FilterMode.INCLUDE:
                include_union |= set(record.sources)
            else:
                has_exclude = True
                if exclude_intersection is None:
                    exclude_intersection = set(record.sources)
                else:
                    exclude_intersection &= set(record.sources)

        if not has_exclude:
            return Filter(FilterMode.INCLUDE, frozenset(include_union))

        excluded = (exclude_intersection or set()) - include_union
        return Filter(FilterMode.EXCLUDE, frozenset(excluded))

    def _republish(
        self, interface: Hashable, group: Hashable
    ) -> Optional[SubscriptionUpdate]:
        """重新聚合并与已发布状态比较；仅在确实变化时产生一条更新。"""
        key = (interface, group)
        new_filter = self._aggregate(interface, group)
        old_filter = self._upstream.get(key)

        if new_filter == old_filter:
            return None

        if new_filter is None:
            update = SubscriptionUpdate(
                interface=interface,
                group=group,
                mode=old_filter.mode,
                sources=old_filter.sources,
                withdrawn=True,
            )
            del self._upstream[key]
        else:
            update = SubscriptionUpdate(
                interface=interface,
                group=group,
                mode=new_filter.mode,
                sources=new_filter.sources,
            )
            self._upstream[key] = new_filter

        self._updates.append(update)
        return update


def _sort_pair(pair: tuple[Hashable, Hashable]) -> tuple[str, str]:
    return str(pair[0]), str(pair[1])
