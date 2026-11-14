"""确定性调度器：按 (逻辑时刻, 优先级, 入队序号) 依次执行事件。

不依赖墙钟与随机数，同一场景重复运行产生完全一致的决策序列。
"""

from __future__ import annotations

import heapq
from typing import Callable, List

from .model import Decision


class Scheduler:
    def __init__(self) -> None:
        self.clock = 0
        self._counter = 0
        self._queue: List = []
        self.decisions: List[Decision] = []

    def schedule(self, time: int, action: Callable[["Scheduler"], None], priority: int = 0) -> None:
        self._counter += 1
        heapq.heappush(self._queue, (time, priority, self._counter, action))

    def run(self) -> List[Decision]:
        while self._queue:
            time, _, _, action = heapq.heappop(self._queue)
            self.clock = time
            action(self)
        return self.decisions

    def record(self, **kwargs) -> Decision:
        decision = Decision(order=len(self.decisions) + 1, **kwargs)
        self.decisions.append(decision)
        return decision
