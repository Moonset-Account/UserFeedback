"""输入数据模型与状态常量。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# 部署级 / 单修复级结论状态
STATUS_AFFECTED = "affected"
STATUS_FIXED = "fixed"
STATUS_REAFFECTED = "reaffected"
STATUS_UNKNOWN = "unknown"

STATUS_LABELS = {
    STATUS_AFFECTED: "受影响",
    STATUS_FIXED: "已修复",
    STATUS_REAFFECTED: "重新受影响",
    STATUS_UNKNOWN: "未知",
}

# 修复标识在单个提交节点上的传播状态
STATE_ACTIVE = "active"      # 修复生效
STATE_INACTIVE = "inactive"  # 修复被撤回 / 未生效
STATE_UNKNOWN = "unknown"    # 存在无法解决的矛盾
STATE_ABSENT = "absent"      # 该维护线上从未出现过此修复标识


@dataclass
class Commit:
    id: str
    parents: list[str]
    fix_ids: list[str]
    # 元素为修复标识字符串；裸撤回（未指定标识）记为 None
    revert_refs: list[Optional[str]]


@dataclass
class MaintenanceLine:
    name: str
    roots: list[str]
    recognized_fix_ids: list[str]


@dataclass
class Build:
    build_id: str
    line: str
    source_commit: str


@dataclass
class MergeResolution:
    """合并节点上的显式解决记录。"""
    commit: str
    resolved: str  # "fixed" 或 "affected"
    fix_id: Optional[str] = None
    line: Optional[str] = None


@dataclass
class LedgerInput:
    commits: dict[str, Commit]
    lines: dict[str, MaintenanceLine]
    builds: list[Build]
    resolutions: list[MergeResolution]


@dataclass
class NodeState:
    """某个修复标识在某个提交节点上的状态。"""
    state: str
    reasons: list[dict] = field(default_factory=list)

    @classmethod
    def active(cls) -> "NodeState":
        return cls(STATE_ACTIVE)

    @classmethod
    def inactive(cls) -> "NodeState":
        return cls(STATE_INACTIVE)

    @classmethod
    def unknown(cls, reasons: list[dict]) -> "NodeState":
        return cls(STATE_UNKNOWN, reasons=list(reasons))


class InputError(ValueError):
    """输入结构或引用关系不合法。"""
