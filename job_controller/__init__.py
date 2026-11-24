"""作业控制器。

处理以当地时间配置的每日定时作业，覆盖 DST 换表语义：

* 秋令时回拨导致同一挂钟时刻出现两次时，每次实际出现分别作为一个候选；
* 春令时前拨导致计划挂钟时刻不存在时，该时刻没有任何候选（仅产生
  :class:`~job_controller.controller.GapNotice` 便于审计）；
* 重启后在日程允许的历史窗口内补算，候选范围为
  ``[now_utc - lookback, now_utc]``；
* 最近确认点之前的历史状态不再重放；
* 禁并发日程遇到仍在运行（或创建请求已发出但未确认）的其它实例时，
  按时刻记录跳过；
* 创建标识由（作业 ID, UTC 时刻）确定性派生，控制器在发出创建请求后
  立即重启、重新计算同一时刻时会得到相同标识，运行端据此识别重复请求。
"""

from .time_map import (
    LocalTimeMap,
    SyntheticZone,
    Transition,
    ZoneInfoLocalTimeMap,
)
from .store import InMemoryRunStateStore, RunRecord, RunStatus
from .controller import (
    CreateDecision,
    Decision,
    GapNotice,
    JobController,
    PlanResult,
    RecreateDecision,
    Schedule,
    SkipDecision,
    creation_id_for,
)

__all__ = [
    "LocalTimeMap",
    "SyntheticZone",
    "Transition",
    "ZoneInfoLocalTimeMap",
    "InMemoryRunStateStore",
    "RunRecord",
    "RunStatus",
    "Schedule",
    "JobController",
    "Decision",
    "CreateDecision",
    "RecreateDecision",
    "SkipDecision",
    "GapNotice",
    "PlanResult",
    "creation_id_for",
]
