"""带业务有效期与录入提交号的双时态关系表。

时间模型
========

* **业务有效期（valid time）**：事实在业务上生效的半开区间
  ``[valid_begin, valid_end)``，左闭右开，因此 ``valid_begin`` 命中、
  ``valid_end`` 不命中。
* **录入提交号（transaction/commit time）**：每笔录入由
  :meth:`BitemporalTable.commit` 分配一个严格递增的整数提交号，
  旧提交永远不可变，新提交只能追加。

一个主键可以在任意提交中多次补录任意半开区间，也可以用
:meth:`BitemporalTable.retract` 在区间内撤销事实（逻辑删除，记为墓碑）。

查询规则
========

查询时只看提交号 ``<= as_of`` 的录入；在覆盖某业务时间点的这些录入里，
**以最近一笔（提交号最大者）为准**。该笔为撤销则视为空档；左右未被
后续录入覆盖的部分继续沿用原值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Hashable, Optional

__all__ = ["BitemporalTable", "Record", "Segment"]

# 内部使用的墓碑哨兵；对外空档统一用 ``None`` 值表示。
_TOMBSTONE = object()


@dataclass(frozen=True)
class Record:
    """一条不可变的录入事实（或撤销）。

    Attributes:
        key: 业务主键。
        valid_begin: 业务有效期起点（含）。
        valid_end: 业务有效期终点（不含）。
        value: 录入的值；``None`` 仅用于内部墓碑，不对外返回。
        commit: 录入提交号，严格递增。
        retracted: 该条是否为区间撤销（墓碑）。
    """

    key: Hashable
    valid_begin: Any
    valid_end: Any
    value: Any
    commit: int
    retracted: bool


@dataclass(frozen=True)
class Segment:
    """业务时间轴上的一段连续片段。

    Attributes:
        begin: 片段起点（含），在查询区间内。
        end: 片段终点（不含），在查询区间内。
        value: 该段的值；``None`` 表示空档（无事实或事实已被撤销）。
        gap: 是否为空档。
    """

    begin: Any
    end: Any
    value: Any
    gap: bool


class BitemporalTable:
    """双时态关系表，维护主键在两条时间轴上的事实演变。"""

    def __init__(self) -> None:
        # 所有录入按提交顺序追加，提交后不可变。
        self._records: list[Record] = []
        self._next_commit = 1

    # ------------------------------------------------------------------ #
    # 写入：提交号严格递增，只追加
    # ------------------------------------------------------------------ #
    def commit(
        self,
        key: Hashable,
        valid_begin: Any,
        valid_end: Any,
        value: Any,
    ) -> int:
        """在一笔新提交中为主键录入一个业务有效期事实。

        新事实只在其区间内覆盖旧事实；区间左右未覆盖部分沿用原值。
        提交号由表自动分配并返回。

        Args:
            key: 业务主键。
            valid_begin: 业务有效期起点（含）。
            valid_end: 业务有效期终点（不含），必须严格晚于起点。
            value: 任意非空值（``None`` 保留给空档语义，禁止录入）。

        Returns:
            新分配的提交号（从 1 开始递增）。

        Raises:
            ValueError: 区间为空（``valid_begin >= valid_end``）或值为 ``None``。
        """
        if value is None:
            raise ValueError("value 不能为 None；None 保留给空档语义")
        self._validate_interval(valid_begin, valid_end)
        commit = self._next_commit
        self._records.append(
            Record(key, valid_begin, valid_end, value, commit, False)
        )
        self._next_commit += 1
        return commit

    def retract(
        self,
        key: Hashable,
        valid_begin: Any,
        valid_end: Any,
    ) -> int:
        """在一笔新提交中撤销主键在指定业务区间内的事实。

        撤销同样只作用于给定区间；区间内最近一笔事实被逻辑删除后，
        若更早的提交在该处仍有事实，则沿用上一笔原值，否则成为空档。
        撤销本身也可被之后的新录入覆盖。

        Returns:
            新分配的提交号。

        Raises:
            ValueError: 区间为空（``valid_begin >= valid_end``）。
        """
        self._validate_interval(valid_begin, valid_end)
        commit = self._next_commit
        self._records.append(
            Record(key, valid_begin, valid_end, _TOMBSTONE, commit, True)
        )
        self._next_commit += 1
        return commit

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    @property
    def head(self) -> int:
        """当前最新提交号；尚无录入时为 0。"""
        return self._next_commit - 1

    def value_at(
        self,
        key: Hashable,
        valid_time: Any,
        as_of: Optional[int] = None,
    ) -> Any:
        """按两个时间点查值。

        Args:
            key: 业务主键。
            valid_time: 业务时间点（按 ``begin <= t < end`` 判定命中）。
            as_of: 录入视图提交号，只考虑提交号 ``<= as_of`` 的录入；
                ``None`` 表示当前最新视图。

        Returns:
            视图中该业务时间点上最近一笔录入的值；无事实或事实已被
            撤销时返回 ``None``。

        Raises:
            ValueError: ``as_of`` 为负。
        """
        as_of = self._resolve_as_of(as_of)
        winner: Optional[Record] = None
        for record in self._records:
            if record.commit > as_of:
                break  # 录入按提交号追加，后续均不可见
            if record.key != key:
                continue
            if record.valid_begin <= valid_time < record.valid_end:
                if winner is None or record.commit > winner.commit:
                    winner = record
        if winner is None or winner.retracted:
            return None
        return winner.value

    def slice(
        self,
        key: Hashable,
        valid_begin: Any,
        valid_end: Any,
        as_of: Optional[int] = None,
    ) -> list[Segment]:
        """在指定提交视图中取出一段业务时间内的连续片段。

        按视图内的事实把查询区间 ``[valid_begin, valid_end)`` 切成若干
        连续片段：

        * 无事实或已撤销的位置产生**空档片段**（``value is None``、
          ``gap=True``），同样在返回列表中；
        * 相邻且值相等（含相邻空档）的片段合并为一段。

        Args:
            key: 业务主键。
            valid_begin: 查询区间起点（含）。
            valid_end: 查询区间终点（不含），必须严格晚于起点。
            as_of: 录入视图提交号；``None`` 表示当前最新视图。

        Returns:
            覆盖整个查询区间、首尾相接的 :class:`Segment` 列表；
            区间内完全无事实时返回单个空档片段。

        Raises:
            ValueError: 查询区间为空或 ``as_of`` 为负。
        """
        self._validate_interval(valid_begin, valid_end)
        as_of = self._resolve_as_of(as_of)

        # 收集视图内、与查询区间相交的录入边界。任意两个相邻边界
        # 之间不存在事实变化，取区间左端点代表整段查值即可。
        cuts = {valid_begin, valid_end}
        visible: list[Record] = []
        for record in self._records:
            if record.commit > as_of:
                break
            if record.key != key:
                continue
            if record.valid_end <= valid_begin or record.valid_begin >= valid_end:
                continue
            visible.append(record)
            if valid_begin < record.valid_begin < valid_end:
                cuts.add(record.valid_begin)
            if valid_begin < record.valid_end < valid_end:
                cuts.add(record.valid_end)

        boundaries = sorted(cuts)
        raw: list[Segment] = []
        for left, right in zip(boundaries, boundaries[1:]):
            value = self.value_at(key, left, as_of)
            raw.append(
                Segment(
                    begin=left,
                    end=right,
                    value=value,
                    gap=value is None,
                )
            )

        return self._merge(raw)

    def records(self, key: Optional[Hashable] = None) -> tuple[Record, ...]:
        """只读地查看已提交录入（按提交号顺序），主要用于测试与审计。

        传入 ``key`` 时只返回该主键的录入；墓碑记录的 ``retracted``
        为 ``True``，其 ``value`` 字段不具业务含义。
        """
        if key is None:
            return tuple(self._records)
        return tuple(r for r in self._records if r.key == key)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_interval(valid_begin: Any, valid_end: Any) -> None:
        if not valid_begin < valid_end:
            raise ValueError(
                "半开区间要求 valid_begin < valid_end，"
                f"得到 {valid_begin!r} .. {valid_end!r}"
            )

    def _resolve_as_of(self, as_of: Optional[int]) -> int:
        if as_of is None:
            return self.head
        if isinstance(as_of, bool) or not isinstance(as_of, int):
            raise TypeError("as_of 必须为整数或 None")
        if as_of < 0:
            raise ValueError("as_of 不能为负")
        # 视图可以指向尚未发生的提交：按当前最新提交截断。
        return min(as_of, self.head)

    @staticmethod
    def _merge(segments: list[Segment]) -> list[Segment]:
        """合并相邻等值段；空档（值均为 None）同样合并。"""
        merged: list[Segment] = []
        for segment in segments:
            if merged and merged[-1].value == segment.value:
                previous = merged[-1]
                merged[-1] = Segment(
                    begin=previous.begin,
                    end=segment.end,
                    value=previous.value,
                    gap=previous.gap,
                )
            else:
                merged.append(segment)
        return merged
