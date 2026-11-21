"""成功执行结束后返回的运行结果。"""

from __future__ import annotations

from dataclasses import dataclass

from .events import Event
from .program import Handle


@dataclass(frozen=True)
class OpRecord:
    """一次已完成集合操作的记录。"""

    handle: Handle
    rank: int
    group: str
    ordinal: int
    tag: object
    members: tuple[int, ...]


@dataclass(frozen=True)
class RunResult:
    """一次成功运行的全部产出。

    ``ops_by_rank`` 按每个 rank 自己的发起顺序记录操作；``events`` 为
    执行器内部产生的结构化事件序列，可用于验证调度交错。
    """

    ops_by_rank: dict[int, tuple[OpRecord, ...]]
    events: tuple[Event, ...]

    def op_by_handle(self, handle: Handle) -> OpRecord:
        """按发起句柄取回对应的完成记录。"""
        for record in self.ops_by_rank.get(handle.rank, ()):
            if record.handle == handle:
                return record
        raise KeyError(f"找不到句柄 {handle} 对应的操作记录")
