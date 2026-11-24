"""safedrain: 双资源节点池缩容规划与可恢复迁移库。

公开模块
--------
- model: 节点 / Pod / 拓扑 / 动作 / 计划 等领域模型
- simulate: 动作的两阶段（占位 -> 确认释放）语义、快照与回放
- planner: 退役集合可行计划、最大退役规划、成本比较
- diagnosis: 极小不可退子集与逐成员移除依据
- logs: 控制器切换下的迁移票据日志与可能现场枚举
- uncertainty: 每轮至多两个节点查询的极小化最坏查询轮数
- controller: 可恢复的迁移器（DrainController）
"""

from .model import (
    Action,
    PairType,
    Plan,
    PlanningError,
    Pod,
    PodPair,
    Resources,
    Topology,
    Node,
    PlanCost,
)
from .simulate import (
    ReplayEvent,
    Snapshot,
    check_stable,
    physical_usage,
    replay_plan,
    simulate_action,
    zone_counts,
)
from .planner import (
    ExplorationLimit,
    plan_for_retirement,
    plan_max_retirement,
)
from .diagnosis import RemovalWitness, explain_irreducible
from .logs import LogEvent, LogEventKind, MigrationTicket, possible_worlds
from .uncertainty import SafetyContext, StrategyNode, UncertaintySolution, solve_query_game
from .controller import DrainController, ControllerState

__all__ = [
    "Action",
    "PairType",
    "Plan",
    "PlanningError",
    "Pod",
    "PodPair",
    "Resources",
    "Node",
    "Topology",
    "PlanCost",
    "ReplayEvent",
    "Snapshot",
    "check_stable",
    "physical_usage",
    "replay_plan",
    "simulate_action",
    "zone_counts",
    "ExplorationLimit",
    "plan_for_retirement",
    "plan_max_retirement",
    "RemovalWitness",
    "explain_irreducible",
    "LogEvent",
    "LogEventKind",
    "MigrationTicket",
    "possible_worlds",
    "SafetyContext",
    "StrategyNode",
    "UncertaintySolution",
    "solve_query_game",
    "DrainController",
    "ControllerState",
]
