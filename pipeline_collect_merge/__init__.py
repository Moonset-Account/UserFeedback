"""流水线分片收集与合并模块。

公开 API：
    - Interval: 半开区间 [lo, hi)
    - ShardAttempt: 一次分片尝试（区间 + 复合变换 + 是否成功）
    - Replacement: 执行器确认的重试替换关系
    - MergeResult: 合并结果（完整变换 + 实际片段序列 + 状态）
    - Collector: 收集器，负责收集尝试与替换关系并执行合并
    - compose: 按从左到右顺序组合多个变换
"""

from .collect_merge import (
    Collector,
    Interval,
    MergeResult,
    Replacement,
    ShardAttempt,
    compose,
)

__all__ = [
    "Collector",
    "Interval",
    "MergeResult",
    "Replacement",
    "ShardAttempt",
    "compose",
]
