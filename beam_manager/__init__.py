"""束管理器（beam search + 分页 KV 搬移调度）。

公开模块：

* :mod:`beam_manager.planner` —— 物理页、搬页操作与纯函数调度规划器
* :mod:`beam_manager.manager` —— 候选、束对象与逐步执行的束管理器
"""

from .planner import (
    AppendPage,
    CapacityError,
    ChildSpec,
    CopyPage,
    DropPage,
    MovePage,
    PageOp,
    PhysicalMemory,
    build_step_plan,
)
from .manager import Beam, BeamManager, Candidate, StepPlan

__all__ = [
    "AppendPage",
    "Beam",
    "BeamManager",
    "Candidate",
    "CapacityError",
    "ChildSpec",
    "CopyPage",
    "DropPage",
    "MovePage",
    "PageOp",
    "PhysicalMemory",
    "StepPlan",
    "build_step_plan",
]
