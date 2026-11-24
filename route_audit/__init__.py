"""route_audit —— 私网对接变更的路由隔离审查模块（仅防御性判断）。

公开 API 见 README。典型用法::

    from route_audit import (
        Change, ChangeAction, ChangeAuditor, NextHopMap, Prefix, Probe,
        Route, RoutingState, TransitDirection, TransitGrant,
        load_scenario_dict, load_scenario_file,
    )
"""

from __future__ import annotations

from .auditor import AuditReport, ChangeAuditor
from .exceptions import (
    DuplicateRouteError,
    InvalidPrefixError,
    RouteAuditError,
    RouteNotFoundError,
    ScenarioFormatError,
    UnknownDomainError,
)
from .loader import Scenario, load_scenario_dict, load_scenario_file
from .model import (
    Change,
    ChangeAction,
    Finding,
    Prefix,
    Probe,
    Route,
    Step,
    TraceResult,
    TraceStatus,
    TransitDirection,
    TransitGrant,
)
from .serialize import (
    change_to_dict,
    finding_to_dict,
    probe_to_dict,
    route_to_dict,
    step_to_dict,
    trace_to_dict,
)
from .state import NextHopMap, RoutingState
from .tracer import DEFAULT_MAX_HOPS, Tracer

__all__ = [
    # 核心模型
    "Prefix",
    "Route",
    "TransitGrant",
    "TransitDirection",
    "Change",
    "ChangeAction",
    "Step",
    "TraceStatus",
    "TraceResult",
    "Finding",
    "Probe",
    # 状态与审查
    "NextHopMap",
    "RoutingState",
    "Tracer",
    "DEFAULT_MAX_HOPS",
    "ChangeAuditor",
    "AuditReport",
    # 装载
    "Scenario",
    "load_scenario_dict",
    "load_scenario_file",
    # 序列化
    "route_to_dict",
    "step_to_dict",
    "trace_to_dict",
    "finding_to_dict",
    "change_to_dict",
    "probe_to_dict",
    # 异常
    "RouteAuditError",
    "InvalidPrefixError",
    "UnknownDomainError",
    "DuplicateRouteError",
    "RouteNotFoundError",
    "ScenarioFormatError",
]
