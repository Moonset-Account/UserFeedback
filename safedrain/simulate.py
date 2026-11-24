"""两阶段搬迁语义、快照与逐步回放。

一个动作（:class:`~safedrain.model.Action`）分两个阶段：

1. **占位 reserve**：先在目的地为 clique 中的每个 Pod 预留资源，旧副本仍在
   源端运行。目的地必须未软下线、满足标签选择器、不违反异机约束，且预留后
   双资源容量仍可容纳。此阶段正在搬的副本仍归源端区域统计。
2. **确认释放 release**：源端副本逐个确认删除，每确认一个就检查一次区域
   副本差；同机 clique 的配对约束在整个动作完成后的稳定态统一校验。

所有中间状态都以 :class:`Snapshot` 暴露，可逐步回放核对每个中间占用。
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Action, PairType, PlanningError, Pod, Topology


@dataclass(frozen=True)
class Snapshot:
    """某一时刻的占用视图。

    placement[i] 是第 i 个 Pod 当前（旧或新）副本所在节点；
    reserved 中的 (pod_id, destination) 表示该 Pod 已在目的地占位、
    但源端旧副本尚未确认删除，因此此刻它同时占用两端资源。
    """

    phase: str
    placement: tuple[str, ...]
    reserved: tuple[tuple[str, str], ...]

    def reserved_destinations(self) -> dict[str, str]:
        return dict(self.reserved)


@dataclass(frozen=True)
class ReplayEvent:
    """回放中的一个可核对事件，snapshot 是事件发生后的占用。"""

    kind: str
    ticket: str
    pod: str | None
    node: str | None
    snapshot: Snapshot


# ---------------------------------------------------------------------------
# 基础统计
# ---------------------------------------------------------------------------

def _index_maps(topology: Topology) -> tuple[dict[str, int], dict[str, Pod]]:
    return ({p.id: i for i, p in enumerate(topology.pods)}, topology.pod_map)


def physical_usage(topology: Topology, snapshot: Snapshot, node_id: str):
    """节点上的物理占用：现存副本 + 已占位但未释放的新副本。"""
    total = topology.node_map[node_id].capacity.__class__(0, 0)
    reserved = snapshot.reserved_destinations()
    for i, pod in enumerate(topology.pods):
        if snapshot.placement[i] == node_id:
            total = total + pod.request
        if reserved.get(pod.id) == node_id:
            total = total + pod.request
    return total


def zone_counts(topology: Topology, snapshot: Snapshot, workload: str) -> dict[str, int]:
    """某 workload 的区域副本数。

    正在搬迁（已占位未释放）的副本只归原区：其 placement 仍指向源节点，
    因此直接按 placement 统计即满足规则。拓扑中所有区域都会出现，
    没有副本的区域计 0，保证区域差比较不会漏掉空区。
    """
    counts: dict[str, int] = {node.zone: 0 for node in topology.nodes}
    for i, pod in enumerate(topology.pods):
        if pod.workload != workload:
            continue
        node_id = snapshot.placement[i]
        zone = topology.node_map[node_id].zone
        counts[zone] = counts.get(zone, 0) + 1
    return counts


def zone_delta_ok(topology: Topology, snapshot: Snapshot) -> bool:
    """每个受限 workload 的任意两区域副本数差都不超过上限。"""
    for workload, cap in topology.zone_delta_caps.items():
        counts = zone_counts(topology, snapshot, workload).values()
        if counts and max(counts) - min(counts) > cap:
            return False
    return True


# ---------------------------------------------------------------------------
# 稳定态校验
# ---------------------------------------------------------------------------

def check_stable(topology: Topology, placement: tuple[str, ...]) -> None:
    """校验一个稳定态（没有进行中的占位）满足全部静态与容量约束。"""
    if len(placement) != len(topology.pods):
        raise PlanningError("placement 长度与 Pod 数不一致")
    node_map = topology.node_map
    for node_id in placement:
        if node_id not in node_map:
            raise PlanningError(f"placement 引用了未知节点 {node_id}")

    # 容量 / 标签 / 软下线
    usage = {n.id: n.capacity.__class__(0, 0) for n in topology.nodes}
    for i, pod in enumerate(topology.pods):
        node_id = placement[i]
        node = node_map[node_id]
        usage[node_id] = usage[node_id] + pod.request
        if pod.selector and not pod.selector.issubset(node.labels):
            raise PlanningError(f"Pod {pod.id} 落在不满足选择器的节点 {node_id}")
        # 软下线只禁止“新接入”，由 reserve 阶段把关；稳定态这里只看不可移动。
        if pod.immovable and pod.node != node_id:
            raise PlanningError(f"不可移动 Pod {pod.id} 被搬离现址 {pod.node}")
    for node in topology.nodes:
        if not usage[node.id].fits_in(node.capacity):
            raise PlanningError(f"节点 {node.id} 资源超配")

    # 同机 / 异机约束
    pos = {pod.id: placement[i] for i, pod in enumerate(topology.pods)}
    for pair in topology.pairs:
        if pair.kind is PairType.SAME_NODE and pos[pair.a] != pos[pair.b]:
            raise PlanningError(f"同机约束被违反：{pair.a} / {pair.b}")
        if pair.kind is PairType.DIFFERENT_NODE and pos[pair.a] == pos[pair.b]:
            raise PlanningError(f"异机约束被违反：{pair.a} / {pair.b}")

    # 区域差
    snap = Snapshot("stable", placement, ())
    if not zone_delta_ok(topology, snap):
        raise PlanningError("稳定态区域副本差超过上限")


def initial_snapshot(topology: Topology) -> Snapshot:
    placement = tuple(p.node for p in topology.pods)
    snapshot = Snapshot("initial", placement, ())
    check_stable(topology, placement)
    return snapshot


# ---------------------------------------------------------------------------
# 两阶段动作模拟
# ---------------------------------------------------------------------------

def _physical_node_of(snapshot: Snapshot, pod_id: str, idx: int) -> str:
    """异机校验用：若 Pod 已在别处占位，则以占位目的地视为其物理位置。"""
    return snapshot.reserved_destinations().get(pod_id, snapshot.placement[idx])


def reserve_action(topology: Topology, snapshot: Snapshot,
                   action: Action) -> list[ReplayEvent]:
    """只执行阶段 1（占位），返回 reserved 事件；结束后快照停在 reserved。"""
    node_map = topology.node_map
    pod_index, pod_map = _index_maps(topology)
    if snapshot.phase not in {"stable", "initial", "completed"}:
        raise PlanningError("只能在稳定态上发起新的搬迁动作")
    if action.source not in node_map or action.destination not in node_map:
        raise PlanningError("动作引用了未知节点")
    dest_node = node_map[action.destination]
    if dest_node.cordoned:
        raise PlanningError(f"目的地 {dest_node.id} 已软下线，不能接入新副本")

    reserved = dict(snapshot.reserved)
    placement = list(snapshot.placement)
    events: list[ReplayEvent] = []
    ticket = action.ticket or "T?"

    for pod_id in action.clique:
        if pod_id not in pod_index:
            raise PlanningError(f"动作引用了未知 Pod {pod_id}")
        idx = pod_index[pod_id]
        pod = pod_map[pod_id]
        if pod_id in reserved:
            raise PlanningError(f"Pod {pod_id} 已有进行中的占位")
        if placement[idx] != action.source:
            raise PlanningError(f"Pod {pod_id} 不在源节点 {action.source} 上")
        if pod.immovable:
            raise PlanningError(f"不可移动 Pod {pod_id} 不能搬迁")
        if pod.selector and not pod.selector.issubset(dest_node.labels):
            raise PlanningError(f"节点 {dest_node.id} 不满足 Pod {pod_id} 的标签选择器")
        for pair in topology.pairs:
            if pair.kind is not PairType.DIFFERENT_NODE:
                continue
            other = pair.b if pair.a == pod_id else pair.a if pair.b == pod_id else None
            if other is None or other in action.clique:
                continue
            other_idx = pod_index[other]
            if _physical_node_of(snapshot, other, other_idx) == action.destination:
                raise PlanningError(
                    f"异机约束：{pod_id} 不能与 {other} 同落 {action.destination}"
                )

    # 容量：目的地现存占用 + 整个 clique 的新占位必须同时容纳
    used = physical_usage(topology, snapshot, action.destination)
    requested = topology.node_map[action.destination].capacity.__class__(0, 0)
    for pod_id in action.clique:
        requested = requested + pod_map[pod_id].request
    if not (used + requested).fits_in(dest_node.capacity):
        raise PlanningError(f"目的地 {action.destination} 无法容纳整组占位")

    for pod_id in action.clique:
        reserved[pod_id] = action.destination
        reserved_snapshot = Snapshot(
            "reserved",
            tuple(placement),
            tuple(sorted(reserved.items())),
        )
        # 占位期间副本仍归原区，区域差不应变化；仍显式复核一次。
        if not zone_delta_ok(topology, reserved_snapshot):
            raise PlanningError("占位后区域副本差超过上限")
        events.append(
            ReplayEvent("reserved", ticket, pod_id, action.destination, reserved_snapshot)
        )
    return events


def release_action(topology: Topology, reserved_snapshot: Snapshot,
                   action: Action) -> list[ReplayEvent]:
    """只执行阶段 2：从已占位快照逐个确认删除，最终回到稳定态。"""
    pod_index, _ = _index_maps(topology)
    reserved = dict(reserved_snapshot.reserved)
    placement = list(reserved_snapshot.placement)
    ticket = action.ticket or "T?"
    events: list[ReplayEvent] = []

    for pod_id in action.clique:
        if reserved.get(pod_id) != action.destination:
            raise PlanningError(
                f"释放前对账失败：{pod_id} 未在 {action.destination} 占位"
            )
    for pod_id in action.clique:
        idx = pod_index[pod_id]
        del reserved[pod_id]
        placement[idx] = action.destination
        partial = Snapshot(
            "releasing",
            tuple(placement),
            tuple(sorted(reserved.items())),
        )
        if not zone_delta_ok(topology, partial):
            raise PlanningError(f"确认删除 {pod_id} 后区域副本差超过上限")
        events.append(
            ReplayEvent("released", ticket, pod_id, action.source, partial)
        )

    final_snapshot = Snapshot("completed", tuple(placement), ())
    # 同机/异机、容量、标签、不可移动在动作边界统一复核。
    check_stable(topology, final_snapshot.placement)
    events.append(
        ReplayEvent("completed", ticket, None, action.destination, final_snapshot)
    )
    return events


def simulate_action(topology: Topology, snapshot: Snapshot, action: Action) -> list[ReplayEvent]:
    """在 snapshot 上执行完整两阶段动作，返回逐阶段事件与最终稳定快照。

    任一约束不满足都会抛出 :class:`PlanningError`，且不会产生部分结果。
    """
    events = reserve_action(topology, snapshot, action)
    reserved_snapshot = events[-1].snapshot
    events.extend(release_action(topology, reserved_snapshot, action))
    return events


# ---------------------------------------------------------------------------
# 计划回放
# ---------------------------------------------------------------------------

def replay_plan(topology: Topology, plan, *, verify_goal: bool = True) -> list[ReplayEvent]:
    """逐步回放整个计划，返回含初始态在内的全部事件。

    verify_goal=True 时，结束后退役节点上不得残留任何 Pod。
    """
    snapshot = initial_snapshot(topology)
    events = [ReplayEvent("initial", "", None, None, snapshot)]
    for step, action in enumerate(plan.actions, start=1):
        ticket = action.ticket or f"T{step}"
        if action.ticket != ticket:
            action = Action(action.source, action.destination, action.clique, ticket)
        for event in simulate_action(topology, snapshot, action):
            events.append(event)
        snapshot = events[-1].snapshot

    if verify_goal:
        for i, pod in enumerate(topology.pods):
            if snapshot.placement[i] in plan.retiring:
                raise PlanningError(
                    f"退役节点 {snapshot.placement[i]} 上仍残留 Pod {pod.id}"
                )
    return events
