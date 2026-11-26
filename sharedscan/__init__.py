"""单扫描共享计划：计划器 + 小型关系解释器。

公开 API 见 README 与各子模块文档字符串。
"""

from .model import (
    Agg,
    AggFn,
    Catalog,
    EmptyGroupPolicy,
    EmptyInputPolicy,
    Predicate,
    Request,
    Semantics,
    make_catalog,
)
from .planner import BlockingReasons, Projection, SharedPlan, build_shared_plan
from .interpreter import RequestResult, execute_request, execute_shared_plan
from .verify import verify_plan

__all__ = [
    "Agg",
    "AggFn",
    "Catalog",
    "EmptyGroupPolicy",
    "EmptyInputPolicy",
    "Predicate",
    "Request",
    "Semantics",
    "make_catalog",
    "BlockingReasons",
    "Projection",
    "SharedPlan",
    "build_shared_plan",
    "RequestResult",
    "execute_request",
    "execute_shared_plan",
    "verify_plan",
]
