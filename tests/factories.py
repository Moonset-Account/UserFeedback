"""测试共用的拓扑构造助手，全部为本地合成数据。"""

from __future__ import annotations

from safedrain.model import (
    PairType,
    Pod,
    PodPair,
    Resources,
    Node,
    Topology,
)


def node(node_id, cpu=4, mem=4, zone="z1", labels=(), cordoned=False) -> Node:
    return Node(
        node_id,
        Resources(cpu, mem),
        zone,
        frozenset(labels),
        cordoned,
    )


def pod(pod_id, workload="app", cpu=1, mem=1, *, on=None,
        immovable=False, selector=()) -> Pod:
    return Pod(
        pod_id,
        workload,
        Resources(cpu, mem),
        node=on,
        immovable=immovable,
        selector=frozenset(selector),
    )


def pair(a, b, kind: PairType) -> PodPair:
    return PodPair(a, b, kind)


def build(nodes, pods, pairs=(), caps=None) -> Topology:
    return Topology(
        nodes=list(nodes),
        pods=list(pods),
        pairs=list(pairs),
        zone_delta_caps=dict(caps or {}),
    )
