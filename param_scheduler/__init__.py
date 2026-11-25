"""参数组调度组件。

本包实现训练运行器使用的参数组学习率调度器，核心语义：

* 参数可被暂停（freeze），暂停期间不参与更新、不计有效更新次数；
* 参数按身份（稳定的 ID）在学习率组之间迁移，累计的有效更新次数跟随参数；
* 每个组给出按参数自身有效更新次数取值的学习率安排；
* 以步编号确认完成，确认时同时更新参数计数并产出下一个步的计划；
* 重复确认同一步沿用首次确认的结果（幂等）；
* 断点发生在“调组已发生、该步尚未确认”时，恢复后按参数已有计数在新组取学习率；
* 第一次解冻且从未更新的参数，从第一步（计数 0 对应的学习率）开始算。
"""

from .scheduler import (
    Schedule,
    Scheduler,
    SchedulerError,
    StepPlan,
    StepResult,
)

__all__ = [
    "Schedule",
    "Scheduler",
    "SchedulerError",
    "StepPlan",
    "StepResult",
]
