"""作业控制器核心逻辑。

控制器是无状态的计算单元：所有持久信息都放在
:class:`~job_controller.store.RunStateStore` 中，因此销毁并重建控制器
实例（保留存储）即可真实模拟进程重启。

规划入口 :meth:`JobController.plan` 的处理顺序：

1. 计算补看窗口
   ``window_start = max(last_confirmed_utc, now_utc - lookback)``，
   窗口为闭区间 ``[window_start, now_utc]``；
2. 枚举窗口覆盖到的每个当地日期，用 :class:`LocalTimeMap` 解析计划
   挂钟时刻：0 个 UTC 为前拨跳过（GapNotice），1~2 个 UTC 分别作为
   独立候选（回拨歧义时刻自然产生两个候选）；
3. 对窗口内每个候选时刻，结合既有运行记录决定
   新建 / 幂等重发 / 跳过，并把新决定写回存储。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Literal

from .store import RunRecord, RunStatus, RunStateStore
from .time_map import LocalTimeMap

# 创建标识命名空间（固定值，保证跨进程、跨重启可复现）
_CREATION_NAMESPACE = uuid.UUID("6f3f1b2a-8c4d-4e7f-9a2b-1c5d6e7f8091")


def creation_id_for(schedule_id: str, scheduled_utc: datetime) -> str:
    """由（日程 ID, 计划 UTC 时刻）确定性派生创建标识。

    同一输入永远得到同一字符串：控制器发出创建请求后立即重启，
    重新计算到同一时刻时会得到相同标识，运行端据此把重复请求识别为
    同一实例的重放而不是二次创建。
    """

    if scheduled_utc.tzinfo is None:
        raise ValueError("scheduled_utc 必须是带时区的 UTC 时刻")
    normalized = scheduled_utc.astimezone(timezone.utc)
    urn = (
        f"urn:job-controller:schedule={schedule_id}"
        f":instant={normalized.isoformat()}"
    )
    return str(uuid.uuid5(_CREATION_NAMESPACE, urn))


@dataclass(frozen=True)
class Schedule:
    """每日定时作业日程（当地挂钟时间配置）。"""

    schedule_id: str
    local_time: time
    """计划执行的当地挂钟时间（naive ``time``）。"""

    allow_concurrent: bool = False
    """是否允许同一日程的多个实例并发运行。"""

    lookback: timedelta = timedelta(days=1)
    """重启后允许补看的历史窗口长度；必须为正。"""

    def __post_init__(self) -> None:
        if not self.schedule_id:
            raise ValueError("schedule_id 不能为空")
        if self.local_time.tzinfo is not None:
            raise ValueError("local_time 必须是不带时区的当地挂钟时间")
        if self.lookback <= timedelta(0):
            raise ValueError("lookback 必须为正的时间长度")


@dataclass(frozen=True)
class Decision:
    """单个计划时刻上的控制器决定（基类）。"""

    schedule_id: str
    scheduled_utc: datetime
    creation_id: str | None
    kind: str

    def __post_init__(self) -> None:
        if self.scheduled_utc.tzinfo is None:
            raise ValueError("scheduled_utc 必须是带时区的 UTC 时刻")


@dataclass(frozen=True)
class CreateDecision(Decision):
    """新时刻：发出创建请求（记录已落盘为 CREATE_REQUESTED）。"""

    kind: Literal["create"] = "create"
    creation_id: str | None = None


@dataclass(frozen=True)
class RecreateDecision(Decision):
    """重启后重算到已发过请求、尚未确认的时刻：同一标识幂等重发。"""

    kind: Literal["recreate"] = "recreate"
    creation_id: str | None = None


@dataclass(frozen=True)
class SkipDecision(Decision):
    """跳过该时刻，不发起创建。"""

    reason: str = ""
    """取值：``terminal:<status>``、``running``、``concurrency``。"""

    status: RunStatus | None = None
    """跳定时存储中该时刻的既有状态（如有）。"""

    blocking_scheduled_utc: datetime | None = None
    """并发冲突时占用名额的另一实例时刻。"""

    kind: Literal["skip"] = "skip"
    creation_id: str | None = None


@dataclass(frozen=True)
class GapNotice:
    """春令时前拨导致计划挂钟时刻不存在的审计信息（不产生实例）。"""

    schedule_id: str
    local_wall_time: datetime
    reason: str = "nonexistent_local_time"


@dataclass(frozen=True)
class PlanResult:
    """一次规划的完整结果。"""

    schedule_id: str
    window_start: datetime
    window_end: datetime
    decisions: tuple[Decision, ...] = field(default_factory=tuple)
    gap_notices: tuple[GapNotice, ...] = field(default_factory=tuple)

    @property
    def creates(self) -> tuple[CreateDecision, ...]:
        return tuple(d for d in self.decisions if isinstance(d, CreateDecision))

    @property
    def recreates(self) -> tuple[RecreateDecision, ...]:
        return tuple(
            d for d in self.decisions if isinstance(d, RecreateDecision)
        )

    @property
    def skips(self) -> tuple[SkipDecision, ...]:
        return tuple(d for d in self.decisions if isinstance(d, SkipDecision))


class JobController:
    """无状态作业控制器；状态全部委托给 ``store``。"""

    def __init__(
        self,
        store: RunStateStore,
        time_map: LocalTimeMap,
    ) -> None:
        self._store = store
        self._time_map = time_map

    def plan(
        self,
        schedule: Schedule,
        now_utc: datetime,
        last_confirmed_utc: datetime,
    ) -> PlanResult:
        """计算 ``now_utc`` 时刻应对该日程执行的全部动作。

        ``last_confirmed_utc`` 是最近一次确认点：早于确认点的历史不再
        重放。实际补看窗口起点取
        ``max(last_confirmed_utc, now_utc - lookback)``。
        """

        self._validate_utc(now_utc, "now_utc")
        self._validate_utc(last_confirmed_utc, "last_confirmed_utc")
        if last_confirmed_utc > now_utc:
            raise ValueError("last_confirmed_utc 不能晚于 now_utc")

        window_start = max(
            last_confirmed_utc, now_utc - schedule.lookback
        )
        decisions: list[Decision] = []
        gap_notices: list[GapNotice] = []

        local_start = self._time_map.to_local(window_start)
        local_end = self._time_map.to_local(now_utc)
        day = local_start.date()
        last_day = local_end.date()

        while day <= last_day:
            wall_time = datetime.combine(day, schedule.local_time)
            instants = self._time_map.resolve(wall_time)

            if not instants:
                # 只对窗口实际覆盖到的挂钟时刻发审计通知
                if local_start <= wall_time <= local_end:
                    gap_notices.append(
                        GapNotice(
                            schedule_id=schedule.schedule_id,
                            local_wall_time=wall_time,
                        )
                    )
            else:
                for instant in instants:
                    if window_start <= instant <= now_utc:
                        decisions.append(
                            self._decide_for_instant(schedule, instant)
                        )
            day += timedelta(days=1)

        decisions.sort(key=lambda d: d.scheduled_utc)
        gap_notices.sort(key=lambda g: g.local_wall_time)
        return PlanResult(
            schedule_id=schedule.schedule_id,
            window_start=window_start,
            window_end=now_utc,
            decisions=tuple(decisions),
            gap_notices=tuple(gap_notices),
        )

    def _decide_for_instant(
        self, schedule: Schedule, instant: datetime
    ) -> Decision:
        sid = schedule.schedule_id
        existing = self._store.get(sid, instant)

        if existing is not None:
            return self._decide_from_existing(existing)

        # 全新时刻：先做禁并发检查
        if not schedule.allow_concurrent:
            blocker = self._find_in_flight_blocker(sid, instant)
            if blocker is not None:
                record = RunRecord(
                    schedule_id=sid,
                    scheduled_utc=instant,
                    status=RunStatus.SKIPPED_CONCURRENCY,
                    note=(
                        "blocked by in-flight instance at "
                        f"{blocker.scheduled_utc.isoformat()}"
                    ),
                )
                self._store.put(record)
                return SkipDecision(
                    schedule_id=sid,
                    scheduled_utc=instant,
                    creation_id=None,
                    reason="concurrency",
                    status=RunStatus.SKIPPED_CONCURRENCY,
                    blocking_scheduled_utc=blocker.scheduled_utc,
                )

        creation_id = creation_id_for(sid, instant)
        self._store.put(
            RunRecord(
                schedule_id=sid,
                scheduled_utc=instant,
                status=RunStatus.CREATE_REQUESTED,
                creation_id=creation_id,
            )
        )
        return CreateDecision(
            schedule_id=sid,
            scheduled_utc=instant,
            creation_id=creation_id,
        )

    def _decide_from_existing(self, record: RunRecord) -> Decision:
        sid = record.schedule_id
        instant = record.scheduled_utc

        if record.status is RunStatus.CREATE_REQUESTED:
            # 请求已发出但未确认：重启后用同一标识重发，运行端去重
            return RecreateDecision(
                schedule_id=sid,
                scheduled_utc=instant,
                creation_id=record.creation_id,
            )

        if record.status is RunStatus.RUNNING:
            return SkipDecision(
                schedule_id=sid,
                scheduled_utc=instant,
                creation_id=record.creation_id,
                reason="running",
                status=RunStatus.RUNNING,
            )

        # 终态时刻不再产生新动作
        return SkipDecision(
            schedule_id=sid,
            scheduled_utc=instant,
            creation_id=record.creation_id,
            reason=f"terminal:{record.status.value}",
            status=record.status,
        )

    def _find_in_flight_blocker(
        self, schedule_id: str, instant: datetime
    ) -> RunRecord | None:
        current_wall_time = self._time_map.to_local(instant)
        for record in self._store.list_for_schedule(schedule_id):
            if record.scheduled_utc == instant:
                continue
            # 回拨歧义下，同一挂钟时刻的两次实际出现属于同一逻辑触发，
            # 互不占用并发名额；只让“不同挂钟时刻”的在途实例阻塞。
            if self._time_map.to_local(record.scheduled_utc) == current_wall_time:
                continue
            if record.status.in_flight:
                return record
        return None

    @staticmethod
    def _validate_utc(value: datetime, name: str) -> None:
        if value.tzinfo is None:
            raise ValueError(f"{name} 必须是带时区的 UTC 时刻")
        value.astimezone(timezone.utc)  # 能转换即视为合法 aware 时刻
