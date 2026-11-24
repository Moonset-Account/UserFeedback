"""作业实例状态存储。

控制器把每个“实际出现的计划时刻”对应一条运行记录，主键为
``(schedule_id, scheduled_utc)``。存储抽象成协议后，重启恢复语义可以
用同一个内存实现验证：控制器实例销毁重建、存储保留，即可模拟
“发出创建请求后立即重启”。

状态机
------
::

    CREATE_REQUESTED ──运行端确认──▶ RUNNING ──成功──▶ SUCCEEDED
                                          └──失败──▶ FAILED
    （禁并发冲突，未发起创建）──────────────▶ SKIPPED_CONCURRENCY

换表跳过的挂钟时刻不存在任何实际实例，因此不产生运行记录，只由控制器
在规划结果中给出 GapNotice 审计信息。

终态：``SUCCEEDED`` / ``FAILED`` / ``SKIPPED_CONCURRENCY``。重算到终态
时刻时控制器只回显既有决定，不会重新创建。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Protocol, runtime_checkable


class RunStatus(enum.Enum):
    """单个计划时刻上的作业实例状态。"""

    CREATE_REQUESTED = "create_requested"
    """创建请求已发出、运行端尚未确认（重启后可凭同一创建标识重放）。"""

    RUNNING = "running"
    """运行端已确认开始执行。"""

    SUCCEEDED = "succeeded"
    """执行成功（终态）。"""

    FAILED = "failed"
    """执行失败（终态，控制器不自动重试）。"""

    SKIPPED_CONCURRENCY = "skipped_concurrency"
    """禁并发日程下因其它实例仍在运行而跳过（终态）。"""

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES

    @property
    def in_flight(self) -> bool:
        """是否占用并发名额（创建已发出或正在运行）。"""
        return self in _IN_FLIGHT_STATUSES


_TERMINAL_STATUSES = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.SKIPPED_CONCURRENCY,
    }
)
_IN_FLIGHT_STATUSES = frozenset(
    {RunStatus.CREATE_REQUESTED, RunStatus.RUNNING}
)


@dataclass(frozen=True)
class RunRecord:
    """一个计划时刻上的作业运行记录。"""

    schedule_id: str
    scheduled_utc: datetime
    status: RunStatus
    creation_id: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if self.scheduled_utc.tzinfo is None:
            raise ValueError("scheduled_utc 必须是带时区的 UTC 时刻")
        if self.status in _IN_FLIGHT_STATUSES and not self.creation_id:
            raise ValueError(
                f"{self.status.value} 状态的记录必须携带 creation_id"
            )


@runtime_checkable
class RunStateStore(Protocol):
    """运行记录存储协议；键为 (日程 ID, 计划 UTC 时刻)。"""

    def get(
        self, schedule_id: str, scheduled_utc: datetime
    ) -> RunRecord | None: ...

    def list_for_schedule(self, schedule_id: str) -> list[RunRecord]: ...

    def put(self, record: RunRecord) -> None: ...


@dataclass
class InMemoryRunStateStore:
    """进程内存储；跨控制器实例保留即可模拟重启后的状态恢复。"""

    _records: dict[tuple[str, datetime], RunRecord] = field(default_factory=dict)

    def get(
        self, schedule_id: str, scheduled_utc: datetime
    ) -> RunRecord | None:
        return self._records.get((schedule_id, scheduled_utc))

    def list_for_schedule(self, schedule_id: str) -> list[RunRecord]:
        return sorted(
            (
                record
                for (sid, _when), record in self._records.items()
                if sid == schedule_id
            ),
            key=lambda r: r.scheduled_utc,
        )

    def put(self, record: RunRecord) -> None:
        self._records[(record.schedule_id, record.scheduled_utc)] = record

    def all_records(self) -> Iterable[RunRecord]:
        return list(self._records.values())
