"""小张量训练协调模块。

公开 API：

- :class:`PhysicalStorage`：一段连续的物理权重存储（扁平浮点缓冲）。
- :class:`View`：由 offset / shape / strides 定义在存储之上的逻辑视图，
  可以重绑到等长的其它存储。
- :class:`TrainingCoordinator`：按训练步收集各逻辑算子的梯度贡献，
  把贡献映射回物理位置并完成一次且仅一次的动量更新。
- :class:`InvalidViewError` / :class:`InvalidContributionError`：
  视图映射或贡献内容非法时抛出。
- :class:`StepResult`：一次 ``commit_step`` 的结果快照。
"""

from .storage import InvalidViewError, PhysicalStorage, View
from .coordinator import (
    InvalidContributionError,
    StepResult,
    TrainingCoordinator,
)

__all__ = [
    "PhysicalStorage",
    "View",
    "InvalidViewError",
    "TrainingCoordinator",
    "InvalidContributionError",
    "StepResult",
]
