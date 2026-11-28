"""分步装载规划器（staged load planner）。

对一批互相引用的迁移记录，先选择总代价最低的外键暂缓集合，再排出
INSERT / UPDATE（补写）语句序列，并在每一条语句上模拟检查主键、非空
与外键约束。复合外键只能整组暂缓；非空列不能被临时置空。
"""

from .errors import (
    PlanningError,
    SchemaError,
    DanglingReferenceError,
    ConstraintViolationError,
)
from .model import (
    ColumnSpec,
    ForeignKeySpec,
    TableSpec,
    Row,
    Insert,
    Update,
    DeferredFK,
    CycleEdge,
    PlanResult,
)
from .planner import plan_load
from .checker import ConstraintChecker
from .solver import (
    find_unbreakable_cycle,
    minimum_feedback_edge_set,
    brute_force_minimum_feedback_edge_set,
)

__all__ = [
    "PlanningError",
    "SchemaError",
    "DanglingReferenceError",
    "ConstraintViolationError",
    "ColumnSpec",
    "ForeignKeySpec",
    "TableSpec",
    "Row",
    "Insert",
    "Update",
    "DeferredFK",
    "CycleEdge",
    "PlanResult",
    "plan_load",
    "ConstraintChecker",
    "find_unbreakable_cycle",
    "minimum_feedback_edge_set",
    "brute_force_minimum_feedback_edge_set",
]
