"""集群、作业与需求的数据模型。

所有时间均为整数分钟（相对时刻，``time=0`` 为规划起点）。

关键语义
--------
* 暂停（preempt）一个既有作业必须选择它列出的一个检查点：
  检查点时间是该作业**最早**可以开始写检查点的时刻；写盘可以因为临时写盘
  额度不足而排队延后，作业在自己的写盘完成之前继续占用 GPU。
* 只有整作业暂停写盘完成后，它占用的所有 GPU 才在同一时刻释放；不支持
  释放单个作业的部分卡。
* ``write_amount`` 是该次暂停在写盘期间持续占用的临时写盘额度（类似带宽
  槽位）；写盘占用使用半开区间 ``[write_start, write_finish)``，完成时刻
  立即释放额度。
* ``lost_work`` 是选择该检查点暂停后该作业被丢弃的计算量；``recovery_time``
  是之后恢复该作业需要额外占用 GPU 的时长（仅用于报告恢复代价）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Node:
    """GPU 节点。"""

    node_id: str
    num_gpus: int
    domain: str  # 高速互联域标识


@dataclass(frozen=True)
class CheckpointOption:
    """既有作业列出的一个“下一次可暂停”的检查点。"""

    checkpoint_time: int
    write_duration: int  # 写检查点占用临时盘额度的持续时间
    write_amount: int  # 写盘期间占用的临时写盘额度（带宽槽位）
    recovery_time: int  # 恢复时需要额外占用 GPU 的时长
    lost_work: int  # 在此暂停会丢失的工作量

    @property
    def earliest_finish(self) -> int:
        """额度无限时该次暂停的最早完成时刻。"""
        return self.checkpoint_time + self.write_duration


@dataclass(frozen=True)
class JobPlacement:
    """既有作业在节点上的 GPU 占用：``node_id`` 上占用 ``gpus`` 张卡。"""

    node_id: str
    gpus: int


@dataclass(frozen=True)
class ExistingJob:
    """一个正在运行的既有作业。

    ``priority`` 数值越大优先级越高；只有严格低于新作业优先级的既有作业
    才允许被暂停。
    """

    job_id: str
    priority: int
    gpu_usage: tuple[JobPlacement, ...]
    checkpoints: tuple[CheckpointOption, ...]

    def used_nodes(self) -> frozenset[str]:
        return frozenset(p.node_id for p in self.gpu_usage)

    def total_gpus(self) -> int:
        return sum(p.gpus for p in self.gpu_usage)


@dataclass(frozen=True)
class SameMachineRequirement:
    """新作业要求 ``gpus`` 张卡全部位于同一台节点。"""

    gpus: int


@dataclass(frozen=True)
class MultiNodeDomainRequirement:
    """新作业要求同一互联域内至少 ``nodes`` 台机器，每台 ``gpus_per_node`` 张卡。"""

    domain: str
    nodes: int
    gpus_per_node: int


PlacementRequirement = SameMachineRequirement | MultiNodeDomainRequirement


@dataclass(frozen=True)
class NewJob:
    """等待落位的新作业，``latest_start_time`` 是其硬截止时间。"""

    job_id: str
    priority: int
    requirement: PlacementRequirement
    latest_start_time: int


def validate_inputs(
    nodes: tuple[Node, ...],
    existing_jobs: tuple[ExistingJob, ...],
    new_job: NewJob,
    disk_write_quota: int,
    current_time: int,
) -> None:
    """对输入做结构性校验；非法输入抛 ``ValueError``。

    截止时间不可达等规划层面的不可行不在这里抛错，而由 ``plan`` 返回
    :class:`~planner.diagnose.FailureConstraint` 诊断。
    """
    if not nodes:
        raise ValueError("nodes 不能为空")
    if disk_write_quota <= 0:
        raise ValueError("disk_write_quota 必须为正整数")
    if current_time < 0:
        raise ValueError("current_time 不能为负")

    node_ids: set[str] = set()
    for node in nodes:
        if node.node_id in node_ids:
            raise ValueError(f"节点 ID 重复: {node.node_id}")
        node_ids.add(node.node_id)
        if node.num_gpus <= 0:
            raise ValueError(f"节点 {node.node_id} 的 GPU 数必须为正")

    job_ids: set[str] = set()
    for job in existing_jobs:
        if job.job_id in job_ids:
            raise ValueError(f"作业 ID 重复: {job.job_id}")
        job_ids.add(job.job_id)
        if not job.gpu_usage:
            raise ValueError(f"作业 {job.job_id} 至少需要一处 GPU 占用")
        if not job.checkpoints:
            raise ValueError(f"作业 {job.job_id} 至少需要一个检查点")
        per_node: dict[str, int] = {}
        for placement in job.gpu_usage:
            if placement.node_id not in node_ids:
                raise ValueError(
                    f"作业 {job.job_id} 引用了不存在的节点 {placement.node_id}"
                )
            if placement.gpus <= 0:
                raise ValueError(f"作业 {job.job_id} 的 GPU 占用必须为正")
            per_node[placement.node_id] = per_node.get(placement.node_id, 0) + placement.gpus
        node_capacity = {n.node_id: n.num_gpus for n in nodes}
        for nid, used in per_node.items():
            if used > node_capacity[nid]:
                raise ValueError(
                    f"作业 {job.job_id} 在节点 {nid} 上占用 {used} 卡，"
                    f"超过该节点容量 {node_capacity[nid]}"
                )
        for cp in job.checkpoints:
            if cp.write_duration < 0 or cp.write_amount <= 0 or cp.lost_work < 0:
                raise ValueError(f"作业 {job.job_id} 的检查点字段非法")
            if cp.recovery_time < 0:
                raise ValueError(f"作业 {job.job_id} 的恢复时长不能为负")

    if new_job.job_id in job_ids:
        raise ValueError(f"新作业 ID 与既有作业重复: {new_job.job_id}")
    req = new_job.requirement
    if isinstance(req, SameMachineRequirement):
        if req.gpus <= 0:
            raise ValueError("同机需求的 GPU 数必须为正")
    elif isinstance(req, MultiNodeDomainRequirement):
        if req.nodes <= 0 or req.gpus_per_node <= 0:
            raise ValueError("多机需求的机器数与每机卡数必须为正")
        if not any(n.domain == req.domain for n in nodes):
            raise ValueError(f"集群中不存在互联域 {req.domain}")
    else:  # pragma: no cover - 类型系统已限定
        raise ValueError("未知的落位需求类型")
    if new_job.latest_start_time < current_time:
        raise ValueError("latest_start_time 早于 current_time，作业在规划前已超时")
