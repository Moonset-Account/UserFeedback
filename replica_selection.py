"""读路由的副本选择（replica selection）。

写入成功后，客户端拿到 (timeline, position) 形式的写入凭据集合。
后续读取必须路由到"包含所有这些提交"的在线副本。

核心规则：
- 子时间线只继承祖先时间线"直到相应分叉点"的历史。
  例如子线在父线 50 处分叉，则子线即使已重放到 200，
  也只能承诺父线 position <= 50 的提交，不能承诺父线 51 处的提交。
- 候选副本必须覆盖全部写入凭据。
- 在候选中优先选择延迟最低者；延迟相同按副本编号升序。
- 若没有任何单个副本能覆盖全部凭据（例如凭据分属互不包含的分支），
  返回等待（wait），并列明每个在线副本各自缺少哪些凭据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteCredential:
    """一次已确认写入的凭据：某条时间线上的一个提交位置。"""

    timeline_id: str
    position: int


@dataclass(frozen=True)
class TimelineFork:
    """时间线注册信息：父线与分叉位置。

    fork_position 是父线上的位置；本子线只继承父线上
    position <= fork_position 的历史。根时间线的
    parent_timeline_id 为 None，fork_position 约定为 0。
    """

    timeline_id: str
    parent_timeline_id: Optional[str]
    fork_position: int = 0


@dataclass(frozen=True)
class ReplicaInfo:
    """一个在线副本的观测状态。"""

    replica_id: int
    timeline_id: str
    replayed_position: int
    latency_ms: float


class SelectionStatus(Enum):
    OK = "ok"
    WAIT = "wait"


@dataclass(frozen=True)
class SelectionResult:
    """选择结果。

    status == OK   ：replica_id 给出应路由到的副本。
    status == WAIT ：当前没有副本能覆盖全部凭据；
                     missing 按副本编号列明各自缺少的凭据。
    """

    status: SelectionStatus
    replica_id: Optional[int] = None
    missing: Dict[int, Tuple[WriteCredential, ...]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 覆盖判定
# ---------------------------------------------------------------------------


class TimelineRegistry:
    """时间线父子关系与分叉位置的注册表。"""

    def __init__(self, forks: List[TimelineFork]):
        self._forks: Dict[str, TimelineFork] = {f.timeline_id: f for f in forks}

    def get(self, timeline_id: str) -> TimelineFork:
        try:
            return self._forks[timeline_id]
        except KeyError:
            raise KeyError(f"未知时间线: {timeline_id!r}") from None

    def visible_position_on(self, ancestor_id: str, descendant_id: str) -> Optional[int]:
        """descendant 时间线从 ancestor 继承到的最大位置。

        - ancestor == descendant 时返回 None 的语义无法表达"无限"，
          因此本方法仅用于 ancestor 是严格祖先的情形；
          相同时间线由调用方单独处理。
        - 若 ancestor 不在 descendant 的祖先链上，返回 None。
        - 否则返回分叉链上最靠近 ancestor 的那一段的 fork_position，
          即 descendant 可见的 ancestor 历史上界。
        """
        current = self.get(descendant_id)
        while current.parent_timeline_id is not None:
            if current.parent_timeline_id == ancestor_id:
                return current.fork_position
            current = self.get(current.parent_timeline_id)
        return None


def missing_credentials(
    registry: TimelineRegistry,
    replica: ReplicaInfo,
    credentials: List[WriteCredential],
) -> Tuple[WriteCredential, ...]:
    """计算某副本缺少的凭据集合（保持输入顺序）。"""

    missing: List[WriteCredential] = []
    for cred in credentials:
        if not _covers(registry, replica, cred):
            missing.append(cred)
    return tuple(missing)


def _covers(
    registry: TimelineRegistry,
    replica: ReplicaInfo,
    cred: WriteCredential,
) -> bool:
    """副本是否包含凭据所指的提交。"""

    if cred.position < 0:
        return False

    if cred.timeline_id == replica.timeline_id:
        # 同一时间线：只需重放进度到达该位置。
        return cred.position <= replica.replayed_position

    # 凭据所在时间线必须是副本时间线的祖先，且提交位置
    # 不超出继承上界（分叉点），同时副本已重放到该位置。
    inherited_bound = registry.visible_position_on(
        cred.timeline_id, replica.timeline_id
    )
    if inherited_bound is None:
        return False
    return (
        cred.position <= inherited_bound
        and cred.position <= replica.replayed_position
    )


# ---------------------------------------------------------------------------
# 选择
# ---------------------------------------------------------------------------


def select_replica(
    registry: TimelineRegistry,
    replicas: List[ReplicaInfo],
    credentials: List[WriteCredential],
) -> SelectionResult:
    """为一次携带写入凭据的读取选择路由副本。

    1. 逐一计算每个在线副本缺少的凭据；
    2. 不缺少任何凭据的副本为候选；
    3. 候选中按 (latency_ms, replica_id) 升序取第一个；
    4. 无候选时返回 WAIT，并按副本编号升序列明各自缺少的凭据。
    """

    missing_by_replica: Dict[int, Tuple[WriteCredential, ...]] = {}
    candidates: List[ReplicaInfo] = []

    for replica in replicas:
        missing = missing_credentials(registry, replica, credentials)
        missing_by_replica[replica.replica_id] = missing
        if not missing:
            candidates.append(replica)

    if candidates:
        best = min(candidates, key=lambda r: (r.latency_ms, r.replica_id))
        return SelectionResult(status=SelectionStatus.OK, replica_id=best.replica_id)

    ordered_missing = {
        rid: missing_by_replica[rid] for rid in sorted(missing_by_replica)
    }
    return SelectionResult(status=SelectionStatus.WAIT, missing=ordered_missing)
