"""核心数据模型：成本向量、问题、节点/整图报告与门禁结论。

上界约定
--------
``Cost.bytes`` / ``Cost.records`` 为非负整数时，表示可证明的有限上界；
为 ``None`` 时，表示在现有证据下无法给出有限上界（缺少证据或可能无界），
导入端应把对应分支视为可拒绝分支。

两个资源维度相互独立：可能一个维度可证明为 0 或有限，而另一个维度为 None。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence


# ---------------------------------------------------------------------------
# 原因码（机器可判定）
# ---------------------------------------------------------------------------

class B:
    """Blocking / warning 原因码常量。"""

    # 引用了证据键，但调用方没有提供该证据
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    # 证据存在，但取值不是非负整数上界
    INVALID_EVIDENCE = "INVALID_EVIDENCE"
    # 分段展开次数无法证明有界
    UNBOUNDED_COUNT = "UNBOUNDED_COUNT"
    # 子包递归深度 / 扇出无法证明有界
    UNBOUNDED_RECURSION = "UNBOUNDED_RECURSION"
    # 命名子包调用形成没有深度证据保护的静态环
    RECURSION_CYCLE = "RECURSION_CYCLE"
    # call/recurse 引用的 definition 不存在
    UNKNOWN_TARGET = "UNKNOWN_TARGET"
    # 匿名引用：无法证明共享，已保守按新分配计数（warning）
    ANONYMOUS_REFERENCE = "ANONYMOUS_REFERENCE"
    # 具名引用找不到支配性的已解包分配，不能证明共享（warning）
    UNPROVEN_SHARING = "UNPROVEN_SHARING"
    # 无法证明共享且没有给出保守新分配大小，内存上界缺失（blocking）
    MISSING_OBJECT_SIZE = "MISSING_OBJECT_SIZE"
    # reuse_if 的对象在当前位置不能静态证明已解包（warning）
    REUSE_WITHOUT_EVIDENCE = "REUSE_WITHOUT_EVIDENCE"
    # 该区域被导入端显式拒绝（info）
    BRANCH_REJECTED = "BRANCH_REJECTED"

    # 门禁级代码
    BYTES_BUDGET_EXCEEDED = "BYTES_BUDGET_EXCEEDED"
    RECORDS_BUDGET_EXCEEDED = "RECORDS_BUDGET_EXCEEDED"
    UNPROVABLE_BOUND = "UNPROVABLE_BOUND"


SEVERITY_BLOCKING = "blocking"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"


# ---------------------------------------------------------------------------
# 成本向量与格运算
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Cost:
    """二维资源上界。``None`` 表示该维度没有有限可证明上界。"""

    bytes: Optional[int] = 0
    records: Optional[int] = 0

    def __post_init__(self) -> None:
        for name in ("bytes", "records"):
            v = getattr(self, name)
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                raise ValueError(f"Cost.{name} 必须是非负整数或 None，得到 {v!r}")

    @property
    def finite(self) -> bool:
        return self.bytes is not None and self.records is not None

    def as_dict(self) -> dict[str, Optional[int]]:
        return {"bytes": self.bytes, "records": self.records}


COST_ZERO = Cost(0, 0)
COST_UNKNOWN = Cost(None, None)


def bound_add(a: Optional[int], b: Optional[int]) -> Optional[int]:
    """加法格运算：任一操作数无界则结果无界。"""
    if a is None or b is None:
        return None
    return a + b


def bound_max(a: Optional[int], b: Optional[int]) -> Optional[int]:
    """互斥分支取 max：任一分支无有限上界，则 max 无有限上界。"""
    if a is None or b is None:
        return None
    return max(a, b)


def bound_scale(x: Optional[int], k: Optional[int]) -> Optional[int]:
    """乘法格运算 ``k * x``。

    关键边界：

    * ``k == 0``（可证明零次迭代）时结果为 0，即使 ``x is None``；
    * ``k is None``（次数无证据）时，只有 ``x == 0`` 才能证明为 0，
      否则为 None；
    * 其余为普通非负整数乘法。
    """
    if k == 0:
        return 0
    if x == 0:
        return 0
    if k is None or x is None:
        return None
    return k * x


def cost_add(a: Cost, b: Cost) -> Cost:
    return Cost(bound_add(a.bytes, b.bytes), bound_add(a.records, b.records))


def cost_max(a: Cost, b: Cost) -> Cost:
    return Cost(bound_max(a.bytes, b.bytes), bound_max(a.records, b.records))


def cost_scale(c: Cost, k: Optional[int]) -> Cost:
    return Cost(bound_scale(c.bytes, k), bound_scale(c.records, k))


# ---------------------------------------------------------------------------
# 问题与报告
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    message: str
    # 操作位置：优先使用图节点显式给出的 pos，否则为合成的 JSON 路径
    pos: str
    # 产生问题的节点 id（可能为 None）
    node_id: Optional[str] = None
    # 可用于 analyze(reject=...) 的拒绝目标（节点 id 或合成路径）
    region_id: Optional[str] = None
    evidence_key: Optional[str] = None
    # False 表示问题仍保留在报告中，但在当前配置下不可达/已被拒绝静音，
    # 不再阻断门禁。
    active: bool = True
    mute_reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "pos": self.pos,
            "node_id": self.node_id,
            "region_id": self.region_id,
            "evidence_key": self.evidence_key,
            "active": self.active,
            "mute_reason": self.mute_reason,
        }


@dataclass
class NodeReport:
    """单个操作区域（节点或分支臂包装）的分析结果。"""

    region_id: str
    op: str
    pos: str
    cost: Cost
    issues: list[Issue] = field(default_factory=list)
    children: list["NodeReport"] = field(default_factory=list)
    # 该区域在“至少执行一次”的结构意义上必定注册的共享键
    generates: frozenset[str] = field(default_factory=frozenset)
    rejected: bool = False
    unreachable: bool = False
    note: Optional[str] = None

    def walk(self) -> Sequence["NodeReport"]:
        yield self
        for ch in self.children:
            yield from ch.walk()

    def as_dict(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "op": self.op,
            "pos": self.pos,
            "cost": self.cost.as_dict(),
            "issues": [i.as_dict() for i in self.issues],
            "children": [c.as_dict() for c in self.children],
            "generates": sorted(self.generates),
            "rejected": self.rejected,
            "unreachable": self.unreachable,
            "note": self.note,
        }


@dataclass(frozen=True)
class BranchVerdict:
    """一个可被导入端单独拒绝的分支/操作区域。"""

    branch_id: str
    pos: str
    node_id: Optional[str]
    reason_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    cost: Cost

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "pos": self.pos,
            "node_id": self.node_id,
            "reason_codes": list(self.reason_codes),
            "reasons": list(self.reasons),
            "cost": self.cost.as_dict(),
        }


@dataclass
class AnalysisReport:
    graph: Any
    evidence: Mapping[str, Any]
    reject: frozenset[str]
    root: NodeReport
    known_region_ids: frozenset[str]

    # -- 汇总 -------------------------------------------------------------
    @property
    def total(self) -> Cost:
        return self.root.cost

    @property
    def issues(self) -> list[Issue]:
        out: list[Issue] = []
        for nr in self.root.walk():
            out.extend(nr.issues)
        return out

    @property
    def active_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.active]

    @property
    def blocking_issues(self) -> list[Issue]:
        return [i for i in self.active_issues if i.severity == SEVERITY_BLOCKING]

    @property
    def warnings(self) -> list[Issue]:
        return [
            i for i in self.active_issues if i.severity == SEVERITY_WARNING
        ]

    @property
    def rejectable_branches(self) -> list[BranchVerdict]:
        """按 region 聚合所有活动 blocking 问题，给出可拒绝分支清单。"""
        grouped: dict[str, list[Issue]] = {}
        for iss in self.blocking_issues:
            rid = iss.region_id or iss.node_id or iss.pos
            grouped.setdefault(rid, []).append(iss)
        verdicts: list[BranchVerdict] = []
        for rid, iss_list in grouped.items():
            first = iss_list[0]
            cost = self._region_cost(rid) or COST_UNKNOWN
            verdicts.append(
                BranchVerdict(
                    branch_id=rid,
                    pos=first.pos,
                    node_id=first.node_id,
                    reason_codes=tuple(dict.fromkeys(i.code for i in iss_list)),
                    reasons=tuple(i.message for i in iss_list),
                    cost=cost,
                )
            )
        return verdicts

    def _region_cost(self, region_id: str) -> Optional[Cost]:
        for nr in self.root.walk():
            if nr.region_id == region_id:
                return nr.cost
        return None

    def find_region(self, region_id: str) -> Optional[NodeReport]:
        for nr in self.root.walk():
            if nr.region_id == region_id:
                return nr
        return None

    def reject_branches(self, *branch_ids: str) -> "AnalysisReport":
        """在同一图与证据上，追加拒绝若干分支后重新分析。"""
        from .analyzer import analyze

        merged = set(self.reject) | set(branch_ids)
        return analyze(self.graph, dict(self.evidence), reject=merged)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total.as_dict(),
            "rejected_branches": sorted(self.reject),
            "rejectable_branches": [v.as_dict() for v in self.rejectable_branches],
            "issues": [i.as_dict() for i in self.issues],
            "root": self.root.as_dict(),
            "known_region_ids": sorted(self.known_region_ids),
        }


@dataclass(frozen=True)
class BudgetViolation:
    dimension: str
    code: str
    message: str
    bound: Optional[int]
    limit: Optional[int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "code": self.code,
            "message": self.message,
            "bound": self.bound,
            "limit": self.limit,
        }


@dataclass
class GateDecision:
    allowed: bool
    total: Cost
    max_bytes: Optional[int]
    max_records: Optional[int]
    violations: list[BudgetViolation]
    blocking_issues: list[Issue]
    warnings: list[Issue]
    rejectable_branches: list[BranchVerdict]
    report: AnalysisReport = field(repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "total": self.total.as_dict(),
            "max_bytes": self.max_bytes,
            "max_records": self.max_records,
            "violations": [v.as_dict() for v in self.violations],
            "blocking_issues": [i.as_dict() for i in self.blocking_issues],
            "warnings": [i.as_dict() for i in self.warnings],
            "rejectable_branches": [v.as_dict() for v in self.rejectable_branches],
        }
