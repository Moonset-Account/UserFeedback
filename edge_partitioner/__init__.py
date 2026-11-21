"""edge_partitioner：两设备（CPU / 加速器）无环推理图分图器。

快速入口见 :func:`partition`，输入构造见 :class:`Problem`。
"""

from .model import (
    CPU,
    DEVICES,
    MAX_OPERATORS,
    NPU,
    NoFeasiblePartitionError,
    Operator,
    PartitionError,
    Problem,
    topological_order,
)
from .partitioner import (
    Copy,
    CopyStep,
    ComputeStep,
    Solution,
    partition,
    validate_solution,
)

__all__ = [
    "CPU",
    "DEVICES",
    "MAX_OPERATORS",
    "NPU",
    "NoFeasiblePartitionError",
    "Operator",
    "PartitionError",
    "Problem",
    "Copy",
    "CopyStep",
    "ComputeStep",
    "Solution",
    "partition",
    "topological_order",
    "validate_solution",
]
