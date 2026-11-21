"""执行器抛出的异常类型与等待链边。

三种失败场景各自对应一个异常类：

* :class:`TagMismatchError` —— 同一组同一次发起的位置上出现不同标签；
* :class:`MemberExitedError` —— 某个成员的指令序列已经结束，却始终没有
  发起本组本次配对所需要的操作；
* :class:`DeadlockError` —— 所有未结束 rank 都在阻塞等待，等待关系构成
  闭合环，每条 :class:`WaitEdge` 标明缺少哪个组的第几次发起。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WaitEdge:
    """等待链上的一条有向边。

    ``waiter`` 正在等待自己在 ``group`` 组的第 ``ordinal`` 次发起
    （``ordinal`` 从 1 开始计数）收齐，当前缺少成员 ``missing_rank``
    在该位置的发起。
    """

    waiter: int
    group: str
    ordinal: int
    missing_rank: int


class CollectiveError(Exception):
    """集合通信执行器所有错误的基类。"""


class TagMismatchError(CollectiveError):
    """同组同一次发起的配对位置上出现了不一致的标签。"""

    def __init__(
        self,
        *,
        group: str,
        ordinal: int,
        rank: int,
        tag: object,
        existing_tag: object,
        members: tuple[int, ...],
    ) -> None:
        self.group = group
        self.ordinal = ordinal
        self.rank = rank
        self.tag = tag
        self.existing_tag = existing_tag
        self.members = tuple(members)
        super().__init__(
            f"标签不一致: 组 {group!r} 第 {ordinal} 次发起处, "
            f"rank {rank} 的标签 {tag!r} 与已到达的标签 {existing_tag!r} 不同 "
            f"(组成员: {list(self.members)})"
        )


class MemberExitedError(CollectiveError):
    """等待中的成员已经结束，却缺席了本次配对所需的发起。"""

    def __init__(
        self,
        *,
        waiter: int,
        group: str,
        ordinal: int,
        missing_rank: int,
        missing_members: tuple[int, ...],
        members: tuple[int, ...],
    ) -> None:
        self.waiter = waiter
        self.group = group
        self.ordinal = ordinal
        self.missing_rank = missing_rank
        self.missing_members = tuple(missing_members)
        self.members = tuple(members)
        super().__init__(
            f"成员缺席: rank {waiter} 等待组 {group!r} 第 {ordinal} 次发起收齐, "
            f"但成员 rank {missing_rank} 已经结束且从未发起该操作 "
            f"(当前仍缺席: {list(self.missing_members)})"
        )

    def as_wait_edge(self) -> WaitEdge:
        """以等待链边的形式描述这次缺席。"""
        return WaitEdge(
            waiter=self.waiter,
            group=self.group,
            ordinal=self.ordinal,
            missing_rank=self.missing_rank,
        )


class DeadlockError(CollectiveError):
    """所有未结束 rank 均在等待，等待关系形成闭合环。"""

    def __init__(self, *, chain: tuple[WaitEdge, ...]) -> None:
        self.chain = tuple(chain)
        lines = ["所有未结束 rank 都在等待对方先发起, 闭合等待链如下:"]
        size = len(self.chain)
        for index, edge in enumerate(self.chain):
            next_waiter = self.chain[(index + 1) % size].waiter
            lines.append(
                f"  rank {edge.waiter} -> rank {next_waiter}: "
                f"rank {edge.waiter} 等待组 {edge.group!r} 第 {edge.ordinal} 次发起, "
                f"缺少 rank {edge.missing_rank} 的发起"
            )
        super().__init__("\n".join(lines))
