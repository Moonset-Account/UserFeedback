"""预检领域模型：步骤节点、流水线配置与结果对象。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .actions import ActionSpec


@dataclass(frozen=True)
class ConditionTrigger:
    """一次故障切换的触发条件，以及它在路径上的位置。

    ``node_id`` 为发生故障切换的上游节点，``condition`` 为配置中
    ``on`` 字典里的条件名（例如 ``cache_down``）。
    """

    node_id: str
    condition: str

    def render(self) -> str:
        return f"{self.node_id}[{self.condition}]"


@dataclass
class StepNode:
    """流水线中的一个步骤。

    主路径通过 ``next`` 串联；故障切换通过 ``failovers`` 表达，每个条件
    指向一棵独立的子树。``region`` 为最终生效地域——子配置未显式声明时
    在解析阶段按父配置回填。
    """

    node_id: str
    step: str
    action: str
    region: str
    data_types: list[str]
    next: "StepNode | None"
    failovers: dict[str, "StepNode"] = field(default_factory=dict)
    region_inherited: bool = False
    parent_id: str | None = None

    def applies_to(self, data_type: str) -> bool:
        """该步骤是否处理指定数据类型（未声明则视为处理全部数据）。"""
        return not self.data_types or data_type in self.data_types

    def iter_children(self):
        """枚举 (是否故障切换, 条件名或None, 子节点)。"""
        if self.next is not None:
            yield False, None, self.next
        for condition, child in self.failovers.items():
            yield True, condition, child


@dataclass
class PipelineConfig:
    """一份完整的发布链路配置。"""

    name: str
    home_region: str
    approvals: dict[str, list[str]]
    root: StepNode
    actions: dict[str, ActionSpec]

    def action_spec(self, action: str) -> ActionSpec:
        return self.actions[action]

    def approved_regions(self, data_type: str) -> list[str]:
        return list(self.approvals.get(data_type, []))

    def iter_nodes(self):
        """按深度优先遍历全部节点（含故障切换子树）。"""
        stack: list[StepNode] = [self.root]
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node.node_id in seen:
                continue
            seen.add(node.node_id)
            yield node
            children = [child for _, _, child in node.iter_children()]
            stack.extend(reversed(children))


# ---------------------------------------------------------------------------
# 结果模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """单条预检发现。

    severity: ``error`` 表示不满足法务核准要求，``warning`` 表示需要
    人工知悉（例如经核准的异地故障切换落地）。
    """

    code: str
    severity: str
    data_type: str
    node_id: str
    region: str
    message: str
    path: list[str]
    trigger_chain: list[ConditionTrigger]


@dataclass(frozen=True)
class CrossRegionLanding:
    """主路径在本区、故障切换分支在其他区域留下落地数据的记录。

    题面要求返回“从数据入口到那个副本的路径和触发条件”，因此这里
    同时给出完整节点路径与条件链。
    """

    data_type: str
    node_id: str
    step: str
    action: str
    region: str
    path: list[str]
    trigger_chain: list[ConditionTrigger]
    approved: bool

    @property
    def conditions(self) -> list[str]:
        return [t.render() for t in self.trigger_chain]


@dataclass(frozen=True)
class TransitRecord:
    """链路中只转发、不落地的节点记录（另行记录，不参与核准判定）。"""

    data_type: str
    node_id: str
    step: str
    action: str
    region: str
    on_failover: bool
    path: list[str]
    trigger_chain: list[ConditionTrigger]


@dataclass
class DataReport:
    """单一数据类型的地域核对结果。"""

    data_type: str
    approved_regions: list[str]
    transitable_regions: list[str]
    persistable_regions: list[str]
    transit_only_regions: list[str]
    main_route_regions: list[str]
    cross_region_landings: list[CrossRegionLanding]
    transit_records: list[TransitRecord]
    findings: list[Finding]

    @property
    def has_error(self) -> bool:
        return any(f.severity == "error" for f in self.findings)


@dataclass
class PreflightResult:
    """整份配置的预检结果。"""

    name: str
    home_region: str
    data_types: list[str]
    reports: dict[str, DataReport]
    findings: list[Finding]

    @property
    def passed(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    @property
    def all_cross_region_landings(self) -> list[CrossRegionLanding]:
        return [
            landing
            for report in self.reports.values()
            for landing in report.cross_region_landings
        ]
