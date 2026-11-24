"""控制器切换下的迁移票据日志。

现场可能在三个时刻切换控制器：

1. 目的地占位成功之后；
2. 旧副本释放之后；
3. 控制器把回执写入日志之前。

因此同一串日志前缀可以对应多种“实际落位”：票据上只看得到源节点、
目的地候选与已记录阶段，真正落在哪个节点要按票据查询节点才能确认。
节点只支持按票据号查询，一次查询返回该票据在该节点上的真实状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from itertools import product

from .model import PlanningError


class LogEventKind(str, Enum):
    """日志中可能出现的事件阶段。"""

    RESERVED = "reserved"          # 占位成功，回执已落日志
    RELEASED = "released"          # 旧副本释放，回执已落日志
    RESERVE_PENDING = "reserve_pending"   # 占位成功但回执尚未写入
    RELEASE_PENDING = "release_pending"   # 释放完成但回执尚未写入


@dataclass(frozen=True)
class LogEvent:
    """一条日志事件（pending 表示切换发生在回执写入之前）。"""

    ticket: str
    kind: LogEventKind
    source: str
    candidates: tuple[str, ...]

    def prefix(self) -> tuple[str, str, str, tuple[str, ...]]:
        """两份现场共用同一日志前缀时，该元组完全相同。"""
        return (self.ticket, self.kind.value, self.source,
                tuple(sorted(self.candidates)))


@dataclass(frozen=True)
class MigrationTicket:
    """一张迁移票据及其日志记录。"""

    ticket: str
    pod: str
    source: str
    candidates: tuple[str, ...]
    events: tuple[LogEvent, ...]

    def log_prefix(self) -> tuple:
        """控制器视角可见的完整日志前缀（不含真实落位）。"""
        return tuple(event.prefix() for event in self.events)


@dataclass(frozen=True)
class World:
    """一个可能现场：每张票据的实际落位节点。"""

    placement: dict[str, str]

    def log_prefix(self, tickets: tuple[MigrationTicket, ...]) -> tuple:
        return tuple(t.log_prefix() for t in tickets)


def possible_worlds(tickets: list[MigrationTicket] | tuple[MigrationTicket, ...]
                    ) -> tuple[World, ...]:
    """枚举与日志前缀一致的全部可能现场并去重。

    两种实际落位只要票据 -> 节点的映射不同就算不同现场；日志前缀本身
    （事件阶段、源、候选集合）对所有现场完全相同。
    """
    tickets = tuple(tickets)
    if not tickets:
        raise PlanningError("至少需要一张迁移票据")
    seen_tickets: set[str] = set()
    prefixes = []
    option_lists: list[tuple[str, ...]] = []
    for ticket in tickets:
        if ticket.ticket in seen_tickets:
            raise PlanningError(f"票据号重复 {ticket.ticket}")
        seen_tickets.add(ticket.ticket)
        if not ticket.candidates:
            raise PlanningError(f"票据 {ticket.ticket} 没有候选目的地")
        if len(set(ticket.candidates)) != len(ticket.candidates):
            raise PlanningError(f"票据 {ticket.ticket} 候选节点重复")
        for event in ticket.events:
            if event.ticket != ticket.ticket:
                raise PlanningError("日志事件与票据号不一致")
            if tuple(sorted(event.candidates)) != tuple(sorted(ticket.candidates)):
                raise PlanningError("票据内候选节点必须一致")
        prefixes.append(ticket.log_prefix())
        option_lists.append(tuple(sorted(ticket.candidates)))

    worlds: dict[tuple[str, ...], World] = {}
    for combo in product(*option_lists):
        placement = {tickets[i].ticket: node for i, node in enumerate(combo)}
        key = tuple(placement[t.ticket] for t in tickets)
        worlds.setdefault(key, World(placement))
    return tuple(worlds.values())


def shared_log_prefix(tickets: tuple[MigrationTicket, ...],
                      worlds: tuple[World, ...]) -> bool:
    """验证给定现场在控制器日志层面无法区分（前缀完全相同）。"""
    if len(worlds) < 2:
        return False
    prefix = worlds[0].log_prefix(tickets)
    return all(w.log_prefix(tickets) == prefix for w in worlds[1:])
