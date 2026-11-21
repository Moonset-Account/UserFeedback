"""单个 rank 的指令序列与非阻塞操作句柄。

指令只有两种：

* :class:`Initiate` —— 向某个通信组发起一次带标签的非阻塞集合操作，
  返回的 :class:`Handle` 可用于后续等待；
* :class:`Wait` —— 等待自己此前发起的某次操作完成（即该组本次配对
  位置的所有成员都已发起且标签一致）。

使用 :class:`Program` 构建器按顺序追加指令，句柄与等待的配对关系会在
构建期完成校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from typing import Iterator


@dataclass(frozen=True)
class Handle:
    """一次非阻塞集合操作的本地句柄，仅对所属 rank 有意义。"""

    rank: int
    local_index: int


@dataclass(frozen=True)
class Initiate:
    """向 ``group`` 组发起一次标签为 ``tag`` 的非阻塞集合操作。"""

    group: str
    tag: object
    handle: Handle


@dataclass(frozen=True)
class Wait:
    """等待 ``handle`` 对应的操作完成。"""

    handle: Handle


Instruction = Initiate | Wait


class Program:
    """一个 rank 的有序指令序列构建器。"""

    def __init__(self, rank: int) -> None:
        if not isinstance(rank, int) or isinstance(rank, bool):
            raise ValueError("rank 必须是 int")
        self._rank = rank
        self._instructions: list[Instruction] = []
        self._counter = count()

    @property
    def rank(self) -> int:
        return self._rank

    @property
    def instructions(self) -> tuple[Instruction, ...]:
        return tuple(self._instructions)

    def initiate(self, group: str, tag: object) -> Handle:
        """追加一次发起指令并返回该操作的本地句柄。"""
        if not isinstance(group, str) or not group:
            raise ValueError("通信组名必须是非空字符串")
        handle = Handle(rank=self._rank, local_index=next(self._counter))
        self._instructions.append(Initiate(group=group, tag=tag, handle=handle))
        return handle

    def wait(self, handle: Handle) -> None:
        """追加一条等待指令；句柄必须来自本 rank 此前的发起。"""
        if not isinstance(handle, Handle):
            raise ValueError("wait 需要一个 Handle 对象")
        if handle.rank != self._rank:
            raise ValueError(
                f"rank {self._rank} 不能等待 rank {handle.rank} 的操作句柄"
            )
        known = {
            instruction.handle
            for instruction in self._instructions
            if isinstance(instruction, Initiate)
        }
        if handle not in known:
            raise ValueError(
                f"rank {self._rank} 等待的句柄 {handle} 不指向本序列中此前发起的操作"
            )
        self._instructions.append(Wait(handle=handle))

    def __iter__(self) -> Iterator[Instruction]:
        return iter(self._instructions)

    def __len__(self) -> int:
        return len(self._instructions)
