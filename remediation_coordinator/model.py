"""协调模块的领域模型。

只包含可序列化的纯数据结构与前置条件谓词，不包含任何调度逻辑。
所有类型都可以通过 ``to_dict`` / ``from_dict`` 做 JSON 级别的往返，
用于进程重启后的状态恢复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, FrozenSet, Mapping, Sequence, Tuple


# ---------------------------------------------------------------------------
# 观测世界：资源版本 + 诊断证据
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceState:
    """资源的当前版本。版本单调递增，任何一次变化都会产生新版本。"""

    version: int


@dataclass(frozen=True)
class Evidence:
    """一次诊断留下的证据。

    ``active`` 表示告警是否仍然成立；``observed_versions`` 记录诊断时
    观测到的各资源版本，动作据此钉住自己依赖的事实。
    """

    evidence_id: str
    active: bool
    observed_versions: Mapping[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 前置条件：动作开始前必须重新核对的事实
# ---------------------------------------------------------------------------


class Precondition:
    """前置条件谓词基类。

    子类需要实现 :meth:`evaluate`（事实是否仍成立）与 :meth:`references`
    （该谓词依赖哪些资源/证据，用于判断一次外部变化是否与本动作相关）。
    """

    def evaluate(self, world: "WorldState") -> bool:  # pragma: no cover - 接口
        raise NotImplementedError

    def references(self) -> FrozenSet[Tuple[str, str]]:  # pragma: no cover
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:  # pragma: no cover - 接口
        raise NotImplementedError

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Precondition":
        kind = data["type"]
        cls = _PRECONDITION_TYPES.get(kind)
        if cls is None:
            raise ValueError(f"未知的前置条件类型: {kind}")
        return cls._from_dict(data)


_PRECONDITION_TYPES: dict[str, type[Precondition]] = {}


def _register(cls: type[Precondition]) -> type[Precondition]:
    _PRECONDITION_TYPES[cls.__name__] = cls
    return cls


@_register
class ResourceVersion(Precondition):
    """钉住某个资源的版本；版本一旦被外部推进，动作即过期。"""

    def __init__(self, resource_id: str, expected_version: int):
        self.resource_id = resource_id
        self.expected_version = expected_version

    def evaluate(self, world: "WorldState") -> bool:
        state = world.resources.get(self.resource_id)
        return state is not None and state.version == self.expected_version

    def references(self) -> FrozenSet[Tuple[str, str]]:
        return frozenset({("resource", self.resource_id)})

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "resource_id": self.resource_id,
            "expected_version": self.expected_version,
        }

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "ResourceVersion":
        return cls(data["resource_id"], data["expected_version"])

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, ResourceVersion)
            and other.resource_id == self.resource_id
            and other.expected_version == self.expected_version
        )

    def __repr__(self) -> str:
        return f"ResourceVersion({self.resource_id!r}, {self.expected_version})"


@_register
class EvidenceActive(Precondition):
    """要求诊断证据仍然处于活动（告警未解除）状态。"""

    def __init__(self, evidence_id: str):
        self.evidence_id = evidence_id

    def evaluate(self, world: "WorldState") -> bool:
        evidence = world.evidence.get(self.evidence_id)
        return evidence is not None and evidence.active

    def references(self) -> FrozenSet[Tuple[str, str]]:
        return frozenset({("evidence", self.evidence_id)})

    def to_dict(self) -> dict[str, Any]:
        return {"type": type(self).__name__, "evidence_id": self.evidence_id}

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "EvidenceActive":
        return cls(data["evidence_id"])

    def __eq__(self, other: object) -> bool:
        return isinstance(other, EvidenceActive) and other.evidence_id == self.evidence_id

    def __repr__(self) -> str:
        return f"EvidenceActive({self.evidence_id!r})"


@_register
class EvidenceCleared(Precondition):
    """要求诊断证据已经解除（用于以"告警消失"为前提的动作）。"""

    def __init__(self, evidence_id: str):
        self.evidence_id = evidence_id

    def evaluate(self, world: "WorldState") -> bool:
        evidence = world.evidence.get(self.evidence_id)
        return evidence is not None and not evidence.active

    def references(self) -> FrozenSet[Tuple[str, str]]:
        return frozenset({("evidence", self.evidence_id)})

    def to_dict(self) -> dict[str, Any]:
        return {"type": type(self).__name__, "evidence_id": self.evidence_id}

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "EvidenceCleared":
        return cls(data["evidence_id"])

    def __eq__(self, other: object) -> bool:
        return isinstance(other, EvidenceCleared) and other.evidence_id == self.evidence_id

    def __repr__(self) -> str:
        return f"EvidenceCleared({self.evidence_id!r})"


@_register
class AllOf(Precondition):
    """合取前置条件，全部子条件成立时才成立。"""

    def __init__(self, children: Sequence[Precondition]):
        self.children: Tuple[Precondition, ...] = tuple(children)

    def evaluate(self, world: "WorldState") -> bool:
        return all(child.evaluate(world) for child in self.children)

    def references(self) -> FrozenSet[Tuple[str, str]]:
        refs: set[Tuple[str, str]] = set()
        for child in self.children:
            refs.update(child.references())
        return frozenset(refs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "children": [child.to_dict() for child in self.children],
        }

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "AllOf":
        return cls([Precondition.from_dict(item) for item in data["children"]])

    def __eq__(self, other: object) -> bool:
        return isinstance(other, AllOf) and other.children == self.children

    def __repr__(self) -> str:
        return f"AllOf({self.children!r})"


# ---------------------------------------------------------------------------
# 动作规格与运行时状态
# ---------------------------------------------------------------------------


class AccessMode(str, Enum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class ActionSpec:
    """规划期推导出来的动作规格（协调模块的输入之一）。

    ``phases`` 是执行器会依次经历的阶段名；``irrevocable_phase`` 指向进入后
    即不可撤销的那个阶段，为 ``None`` 时表示在生效（effective）之前都可取消。
    """

    action_id: str
    chain_id: str
    step: int
    reads: Tuple[str, ...]
    writes: Tuple[str, ...]
    preconditions: Tuple[Precondition, ...]
    phases: Tuple[str, ...]
    irrevocable_phase: str | None = None

    def __post_init__(self) -> None:
        if not self.action_id:
            raise ValueError("action_id 不能为空")
        if not self.chain_id:
            raise ValueError("chain_id 不能为空")
        if self.step < 0:
            raise ValueError("step 不能为负数")
        if not self.phases:
            raise ValueError(f"动作 {self.action_id} 至少需要一个执行阶段")
        if (
            self.irrevocable_phase is not None
            and self.irrevocable_phase not in self.phases
        ):
            raise ValueError(
                f"动作 {self.action_id} 的不可撤销阶段 {self.irrevocable_phase!r}"
                " 不在阶段列表中"
            )
        if len(set(self.phases)) != len(self.phases):
            raise ValueError(f"动作 {self.action_id} 的阶段名不能重复")

    def footprint(self) -> FrozenSet[str]:
        """该动作触及的全部资源（读并集写）。"""
        return frozenset(set(self.reads) | set(self.writes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "chain_id": self.chain_id,
            "step": self.step,
            "reads": list(self.reads),
            "writes": list(self.writes),
            "preconditions": [p.to_dict() for p in self.preconditions],
            "phases": list(self.phases),
            "irrevocable_phase": self.irrevocable_phase,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionSpec":
        return cls(
            action_id=data["action_id"],
            chain_id=data["chain_id"],
            step=data["step"],
            reads=tuple(data["reads"]),
            writes=tuple(data["writes"]),
            preconditions=tuple(
                Precondition.from_dict(item) for item in data["preconditions"]
            ),
            phases=tuple(data["phases"]),
            irrevocable_phase=data.get("irrevocable_phase"),
        )


class ActionState(str, Enum):
    """动作运行时状态机。"""

    PLANNED = "planned"                # 已推导，尚未占位
    SCHEDULED = "scheduled"            # 已占位，等待派发（待开始）
    DISPATCHED = "dispatched"          # 已派发、仍可取消
    IRREVOCABLE = "irrevocable"        # 已越过不可撤销点，只能等结果
    EFFECTIVE = "effective"            # 已生效，等待确认
    CANCELLING = "cancelling"          # 已请求取消，等待执行器回执
    CONFIRMED = "confirmed"            # 终态：已确认
    CANCELLED = "cancelled"            # 终态：已取消
    REJECTED = "rejected"              # 终态：前置条件过期/链中止
    FAILED = "failed"                  # 终态：执行失败


TERMINAL_STATES = frozenset(
    {
        ActionState.CONFIRMED,
        ActionState.CANCELLED,
        ActionState.REJECTED,
        ActionState.FAILED,
    }
)

# 占位态：持有资源槽位、参与全链路冲突计算
OCCUPYING_STATES = frozenset(
    {
        ActionState.SCHEDULED,
        ActionState.DISPATCHED,
        ActionState.IRREVOCABLE,
        ActionState.EFFECTIVE,
        ActionState.CANCELLING,
    }
)


@dataclass
class Action:
    """运行时动作，包裹规格并持有状态、代次与阶段游标。"""

    spec: ActionSpec
    state: ActionState
    generation: int = 0
    phase_index: int = -1
    reject_reason: str | None = None

    @property
    def action_id(self) -> str:
        return self.spec.action_id

    @property
    def chain_id(self) -> str:
        return self.spec.chain_id

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.to_dict(),
            "state": self.state.value,
            "generation": self.generation,
            "phase_index": self.phase_index,
            "reject_reason": self.reject_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Action":
        return cls(
            spec=ActionSpec.from_dict(data["spec"]),
            state=ActionState(data["state"]),
            generation=data["generation"],
            phase_index=data["phase_index"],
            reject_reason=data.get("reject_reason"),
        )


# ---------------------------------------------------------------------------
# 处置链与回执
# ---------------------------------------------------------------------------


class ChainStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ABORTED = "aborted"


@dataclass
class ChainRecord:
    chain_id: str
    order: int
    status: ChainStatus = ChainStatus.ACTIVE
    abort_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "order": self.order,
            "status": self.status.value,
            "abort_reason": self.abort_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChainRecord":
        return cls(
            chain_id=data["chain_id"],
            order=data["order"],
            status=ChainStatus(data["status"]),
            abort_reason=data.get("abort_reason"),
        )


class EventType(str, Enum):
    """处置记录（回执账本）中的事件类型。"""

    PLANNED = "planned"
    SCHEDULED = "scheduled"
    DISPATCHED = "dispatched"
    PHASE_ADVANCED = "phase_advanced"
    EFFECTIVE = "effective"
    CONFIRMED = "confirmed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_REJECTED = "cancel_rejected"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"
    BLOCKED = "blocked"
    STALE_RESULT = "stale_result"
    CHAIN_COMPLETED = "chain_completed"
    CHAIN_ABORTED = "chain_aborted"
    WORLD_CHANGED = "world_changed"
    RECOVERED = "recovered"


@dataclass(frozen=True)
class Receipt:
    """一条不可变的处置回执。"""

    seq: int
    timestamp: int
    event: str
    chain_id: str | None
    action_id: str | None
    generation: int | None
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "event": self.event,
            "chain_id": self.chain_id,
            "action_id": self.action_id,
            "generation": self.generation,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Receipt":
        return cls(
            seq=data["seq"],
            timestamp=data["timestamp"],
            event=data["event"],
            chain_id=data.get("chain_id"),
            action_id=data.get("action_id"),
            generation=data.get("generation"),
            details=dict(data.get("details", {})),
        )


# ---------------------------------------------------------------------------
# 世界状态容器
# ---------------------------------------------------------------------------


class WorldState:
    """资源版本与诊断证据的可变容器，版本只允许严格递增。"""

    def __init__(
        self,
        resources: Mapping[str, int] | None = None,
        evidence: Mapping[str, Evidence] | None = None,
    ) -> None:
        self.resources: dict[str, ResourceState] = {}
        for resource_id, version in (resources or {}).items():
            if version < 0:
                raise ValueError(f"资源 {resource_id} 初始版本不能为负")
            self.resources[resource_id] = ResourceState(version=version)
        self.evidence: dict[str, Evidence] = dict(evidence or {})

    def resource_version(self, resource_id: str) -> int | None:
        state = self.resources.get(resource_id)
        return None if state is None else state.version

    def update_resource(self, resource_id: str, new_version: int) -> None:
        current = self.resource_version(resource_id)
        if current is not None and new_version <= current:
            raise ValueError(
                f"资源 {resource_id} 版本必须严格递增: 当前 {current}，"
                f"收到 {new_version}"
            )
        self.resources[resource_id] = ResourceState(version=new_version)

    def update_evidence(
        self,
        evidence_id: str,
        active: bool,
        observed_versions: Mapping[str, int] | None = None,
    ) -> None:
        self.evidence[evidence_id] = Evidence(
            evidence_id=evidence_id,
            active=active,
            observed_versions=dict(observed_versions or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "resources": {rid: state.version for rid, state in self.resources.items()},
            "evidence": {
                eid: {
                    "evidence_id": ev.evidence_id,
                    "active": ev.active,
                    "observed_versions": dict(ev.observed_versions),
                }
                for eid, ev in self.evidence.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorldState":
        evidence = {
            eid: Evidence(
                evidence_id=item["evidence_id"],
                active=item["active"],
                observed_versions=dict(item.get("observed_versions", {})),
            )
            for eid, item in data.get("evidence", {}).items()
        }
        return cls(resources=dict(data.get("resources", {})), evidence=evidence)
