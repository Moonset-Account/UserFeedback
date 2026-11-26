"""事件台账：存储事件并按入账时刻提供历史可见视图。"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, List, Optional

from .events import Event


class Ledger:
    """只增不改的事件台账。

    visible(as_of) 返回在指定入账点可见的事件（recorded_at <= as_of），
    用于"按任一历史入账点复算"。as_of 为 None 表示当前全部可见。
    """

    def __init__(self, events: Iterable[Event] = ()) -> None:
        self._events: List[Event] = []
        for event in events:
            self.add(event)

    def add(self, event: Event) -> None:
        if any(e.event_id == event.event_id for e in self._events):
            raise ValueError(f"duplicate event_id: {event.event_id}")
        self._events.append(event)

    def visible(self, as_of: Optional[datetime] = None) -> List[Event]:
        """返回 as_of 入账点可见的事件，按 (发生时刻, 入账时刻, id) 排序。"""
        evs = [e for e in self._events if as_of is None or e.recorded_at <= as_of]
        return sorted(evs, key=lambda e: (e.occurred_at, e.recorded_at, e.event_id))
