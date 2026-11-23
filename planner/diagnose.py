"""不可行计划的约束诊断。

当不存在满足新作业最晚启动时间的计划时，规划器报告“最早失败的约束”：
先按约束可被观察到的时刻排序，同时刻按约束类型与作业 ID 排序，输出
排在最前面的那条。
"""

from __future__ import annotations

from dataclasses import dataclass

from planner.model import (
    MultiNodeDomainRequirement,
    NewJob,
    Node,
    SameMachineRequirement,
)


# 约束类型（同时刻下的报告优先级也按此顺序）
STRUCTURAL = "structural_capacity"
LATE_START = "late_start"
DISK_QUOTA = "disk_quota_serialization"
PROTECTED_JOB = "protected_job"


@dataclass(frozen=True)
class FailureConstraint:
    """最早失败的约束。

    Attributes:
        constraint: 约束类型常量（见本模块）。
        observed_at: 该约束被判定为失败的时刻。
        message: 人类可读的诊断说明。
        details: 结构化细节（涉及的节点、作业、期望与实际值等）。
    """

    constraint: str
    observed_at: int
    message: str
    details: dict[str, object]


def structural_failure(
    nodes: tuple[Node, ...],
    existing_jobs: tuple,
    new_job: NewJob,
    eligible_job_ids: frozenset[str],
    protected_job_ids: frozenset[str],
) -> FailureConstraint:
    """构造“总卡数即使够用也凑不齐形状”的结构性失败诊断。"""
    req = new_job.requirement
    total_capacity = sum(node.num_gpus for node in nodes)
    node_capacity: dict[str, int] = {n.node_id: n.num_gpus for n in nodes}
    node_locked: dict[str, int] = {n.node_id: 0 for n in nodes}
    total_locked = 0
    for job in existing_jobs:
        if job.job_id in eligible_job_ids:
            continue  # 可暂停作业的卡在“全部暂停”的参照下视为可释放
        total_locked += job.total_gpus()
        for p in job.gpu_usage:
            node_locked[p.node_id] += p.gpus
    total_free_if_all_paused = total_capacity - total_locked

    if isinstance(req, SameMachineRequirement):
        max_single = max(
            node_capacity[nid] - node_locked[nid] for nid in node_capacity
        )
        message = (
            f"新作业 {new_job.job_id} 需要同机 {req.gpus} 张卡，但即使暂停所有"
            f"可暂停作业，单节点最大空闲仅 {max_single} 张（受不可暂停作业锁卡"
            f"或单机容量限制）。"
        )
        details: dict[str, object] = {
            "requirement": "same_machine",
            "gpus_needed": req.gpus,
            "max_free_gpus_on_single_node": max_single,
            "total_free_gpus_if_all_paused": total_free_if_all_paused,
            "total_cluster_gpus": total_capacity,
            "node_locked_gpus": dict(sorted(node_locked.items())),
            "protected_job_ids": sorted(protected_job_ids),
        }
    elif isinstance(req, MultiNodeDomainRequirement):
        domain_nodes = [n for n in nodes if n.domain == req.domain]
        per_node_free = {
            n.node_id: node_capacity[n.node_id] - node_locked[n.node_id]
            for n in domain_nodes
        }
        fitting = sum(
            1 for nid, free in per_node_free.items() if free >= req.gpus_per_node
        )
        message = (
            f"新作业 {new_job.job_id} 需要互联域 {req.domain!r} 内 {req.nodes} 台"
            f"机器、每台 {req.gpus_per_node} 卡；该域仅 {fitting} 台满足（即使"
            f"暂停所有可暂停作业）。集群总空闲 {total_free_if_all_paused} 卡"
            f"看起来够用，但卡分散在不同互联域或被不可暂停作业锁定。"
        )
        details = {
            "requirement": "multi_node_domain",
            "domain": req.domain,
            "nodes_needed": req.nodes,
            "gpus_per_node": req.gpus_per_node,
            "nodes_satisfying": fitting,
            "domain_nodes": [n.node_id for n in domain_nodes],
            "per_node_free_gpus": dict(sorted(per_node_free.items())),
            "total_free_gpus_if_all_paused": total_free_if_all_paused,
            "total_cluster_gpus": total_capacity,
            "protected_job_ids": sorted(protected_job_ids),
        }
    else:  # pragma: no cover
        raise TypeError("未知的落位需求类型")

    return FailureConstraint(
        constraint=STRUCTURAL,
        observed_at=0,
        message=message,
        details=details,
    )
