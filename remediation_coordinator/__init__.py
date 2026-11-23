"""修复动作协调模块（只推导动作与结算回执，不执行真实修复）。"""

from __future__ import annotations

from .engine import RecoveryBuckets, RemediationCoordinator
from .executor import DispatchOrder, Executor
from .model import (
    AccessMode,
    Action,
    ActionSpec,
    ActionState,
    AllOf,
    ChainStatus,
    Evidence,
    EvidenceActive,
    EvidenceCleared,
    EventType,
    Precondition,
    Receipt,
    ResourceState,
    ResourceVersion,
    WorldState,
)
from .persistence import load_snapshot, save_snapshot
from .scripted_executor import ScriptedExecutor

__all__ = [
    "AccessMode",
    "Action",
    "ActionSpec",
    "ActionState",
    "AllOf",
    "ChainStatus",
    "DispatchOrder",
    "Evidence",
    "EvidenceActive",
    "EvidenceCleared",
    "EventType",
    "Executor",
    "Precondition",
    "Receipt",
    "RecoveryBuckets",
    "RemediationCoordinator",
    "ResourceState",
    "ResourceVersion",
    "ScriptedExecutor",
    "WorldState",
    "load_snapshot",
    "save_snapshot",
]
