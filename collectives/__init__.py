"""单进程驱动的集合通信执行器。

公开 API 一览::

    from collectives import (
        Group, Program, Executor, RunResult, OpRecord, Handle,
        Initiate, Wait,
        Initiated, SlotCompleted, RankBlocked, RankUnblocked, RankEnded,
        CollectiveError, TagMismatchError, MemberExitedError,
        DeadlockError, WaitEdge,
    )
"""

from __future__ import annotations

from .errors import (
    CollectiveError,
    DeadlockError,
    MemberExitedError,
    TagMismatchError,
    WaitEdge,
)
from .events import (
    Event,
    Initiated,
    RankBlocked,
    RankEnded,
    RankUnblocked,
    SlotCompleted,
)
from .executor import Executor
from .groups import Group
from .program import Handle, Initiate, Instruction, Program, Wait
from .result import OpRecord, RunResult

__all__ = [
    "Group",
    "Program",
    "Executor",
    "RunResult",
    "OpRecord",
    "Handle",
    "Initiate",
    "Wait",
    "Instruction",
    "Event",
    "Initiated",
    "SlotCompleted",
    "RankBlocked",
    "RankUnblocked",
    "RankEnded",
    "CollectiveError",
    "TagMismatchError",
    "MemberExitedError",
    "DeadlockError",
    "WaitEdge",
]
