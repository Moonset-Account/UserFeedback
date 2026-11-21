"""执行过程中产生的结构化事件，便于测试交错调度与外部观察。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Initiated:
    """某个 rank 向通信组发起了一次非阻塞操作（无论是否当场收齐）。"""

    rank: int
    group: str
    ordinal: int
    tag: object


@dataclass(frozen=True)
class SlotCompleted:
    """某个组的某次发起位置已经收齐，配对完成。"""

    group: str
    ordinal: int
    tag: object
    members: tuple[int, ...]


@dataclass(frozen=True)
class RankBlocked:
    """某个 rank 执行等待指令时操作尚未完成，进入阻塞。"""

    rank: int
    group: str
    ordinal: int


@dataclass(frozen=True)
class RankUnblocked:
    """等待的配对位置收齐，某个 rank 解除阻塞。"""

    rank: int
    group: str
    ordinal: int


@dataclass(frozen=True)
class RankEnded:
    """某个 rank 的指令序列全部执行完毕。"""

    rank: int


Event = Initiated | SlotCompleted | RankBlocked | RankUnblocked | RankEnded
