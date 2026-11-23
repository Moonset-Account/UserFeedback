"""新作业的落位计算。

给定每个节点的空闲卡数（由暂停完成时刻决定），计算两类需求的最早就绪
时刻，并提供与时间无关的结构性容量可行性检查（用于反例诊断：总卡数够
但凑不齐一个互联域）。
"""

from __future__ import annotations

from dataclasses import dataclass

from planner.model import (
    MultiNodeDomainRequirement,
    NewJob,
    Node,
    SameMachineRequirement,
)


@dataclass(frozen=True)
class PlacementResult:
    """一次落位的计算结果。

    ``ready_time`` 为新作业最早可启动时刻；``node_allocations`` 为
    ``节点ID -> 该节点上分配给新作业的卡数``。
    """

    ready_time: int
    node_allocations: tuple[tuple[str, int], ...]


def node_free_gpus(
    nodes: tuple[Node, ...],
    existing_jobs: tuple,
    finish_time_by_job: dict[str, int],
) -> dict[str, int]:
    """计算每个节点在其相关暂停全部完成后的空闲卡数。

    ``finish_time_by_job`` 给出被暂停作业的写盘完成时刻；未出现在字典中的
    既有作业保持运行、继续占卡。由于暂停完成后该作业在所有节点上的卡
    同时释放，节点空闲量是静态值（不随时间变化，只取决于暂停集合）。
    """
    free = {node.node_id: node.num_gpus for node in nodes}
    for job in existing_jobs:
        if job.job_id in finish_time_by_job:
            continue  # 已整作业暂停，卡全部释放
        for placement in job.gpu_usage:
            # 同一作业在同一节点可能有多条占用记录，需要逐节点累加
            free[placement.node_id] -= placement.gpus
    return free


def _node_ready_time(
    node_id: str,
    existing_jobs: tuple,
    finish_time_by_job: dict[str, int],
) -> int:
    """某节点上被暂停占用全部释放的最晚完成时刻（无暂停则为 0）。"""
    times = [
        finish_time_by_job[job.job_id]
        for job in existing_jobs
        if job.job_id in finish_time_by_job
        and any(p.node_id == node_id for p in job.gpu_usage)
    ]
    return max(times, default=0)


def earliest_placement(
    nodes: tuple[Node, ...],
    existing_jobs: tuple,
    new_job: NewJob,
    finish_time_by_job: dict[str, int],
) -> PlacementResult | None:
    """计算给定暂停集合下新作业的最早落位；结构不可行返回 ``None``。"""
    free = node_free_gpus(nodes, existing_jobs, finish_time_by_job)
    req = new_job.requirement

    if isinstance(req, SameMachineRequirement):
        candidates = [
            node
            for node in nodes
            if free[node.node_id] >= req.gpus
        ]
        if not candidates:
            return None
        # 就绪时刻最早优先，并列时按节点 ID 保证确定性
        chosen = min(
            candidates,
            key=lambda n: (
                _node_ready_time(n.node_id, existing_jobs, finish_time_by_job),
                n.node_id,
            ),
        )
        return PlacementResult(
            ready_time=_node_ready_time(
                chosen.node_id, existing_jobs, finish_time_by_job
            ),
            node_allocations=((chosen.node_id, req.gpus),),
        )

    if isinstance(req, MultiNodeDomainRequirement):
        domain_nodes = [node for node in nodes if node.domain == req.domain]
        candidates = [
            node
            for node in domain_nodes
            if free[node.node_id] >= req.gpus_per_node
        ]
        if len(candidates) < req.nodes:
            return None
        chosen = sorted(
            candidates,
            key=lambda n: (
                _node_ready_time(n.node_id, existing_jobs, finish_time_by_job),
                n.node_id,
            ),
        )[: req.nodes]
        ready = max(
            _node_ready_time(n.node_id, existing_jobs, finish_time_by_job)
            for n in chosen
        )
        return PlacementResult(
            ready_time=ready,
            node_allocations=tuple(
                sorted((n.node_id, req.gpus_per_node) for n in chosen)
            ),
        )

    raise TypeError(f"未知的落位需求类型: {type(req)!r}")  # pragma: no cover


def structurally_feasible(
    nodes: tuple[Node, ...],
    existing_jobs: tuple,
    new_job: NewJob,
    eligible_job_ids: frozenset[str],
) -> bool:
    """即使把所有可暂停作业都暂停，是否凑得出所需形状的卡。

    与时间、写盘额度无关，只看互联域/单机容量结构。
    """
    finish_map = {job_id: 0 for job_id in eligible_job_ids}
    return (
        earliest_placement(nodes, existing_jobs, new_job, finish_map) is not None
    )
