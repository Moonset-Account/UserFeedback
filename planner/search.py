"""抢占规划的分支限界搜索。

小实例规模（最多 8 节点、10 个既有作业）下，穷举每个可暂停作业的
“跳过 / 选择某个检查点”组合，并使用三类剪枝：

1. 写盘额度结构性剪枝：某个写盘 ``amount`` 超过总额度，或前缀写盘的最晚
   完成时刻已超过新作业截止时间，则该子树不可能可行；
2. 落位形状剪枝：即使暂停当前已选与所有剩余可暂停作业也凑不出需求形状；
3. 丢失工作量界剪枝：当前已选丢失工作量已严格超过已知最优解。

优化目标为字典序：
``(可行且按时落位, 总丢失工作量, 最晚暂停完成时刻, 暂停作业ID元组)``。
"""

from __future__ import annotations

from dataclasses import dataclass

from planner.diagnose import (
    DISK_QUOTA,
    LATE_START,
    FailureConstraint,
    structural_failure,
)
from planner.model import (
    CheckpointOption,
    ExistingJob,
    JobPlacement,
    MultiNodeDomainRequirement,
    NewJob,
    Node,
    SameMachineRequirement,
    validate_inputs,
)
from planner.placement import (
    PlacementResult,
    earliest_placement,
    node_free_gpus,
)
from planner.quota import ScheduledWrite, WriteRequest, schedule_writes


@dataclass(frozen=True)
class PreemptionAction:
    """一个既有作业的完整暂停安排。"""

    job_id: str
    checkpoint_time: int
    write_amount: int
    write_start: int
    write_finish: int
    release_time: int  # = write_finish，半开区间完成即释放
    released_resources: tuple[JobPlacement, ...]
    lost_work: int
    recovery_time: int

    @property
    def write_was_delayed(self) -> bool:
        return self.write_start > self.checkpoint_time


@dataclass(frozen=True)
class PlanResult:
    """规划结果；``feasible`` 为真时动作与启动时间可直接执行。"""

    feasible: bool
    new_job_id: str
    start_time: int | None
    node_allocations: tuple[tuple[str, int], ...] | None
    preemption_actions: tuple[PreemptionAction, ...]
    total_lost_work: int
    max_preemption_finish: int | None
    total_recovery_gpu_time: int
    disk_write_quota: int
    current_time: int
    latest_start_time: int
    failure: FailureConstraint | None

    def earliest_failing_constraint(self) -> FailureConstraint | None:
        """不可行时返回最早失败的约束；可行时返回 ``None``。"""
        return self.failure


@dataclass
class _Snapshot:
    """一次完整组合评估的中间快照，供选优与诊断区分复用。"""

    selections: tuple[tuple[ExistingJob, CheckpointOption], ...]
    scheduled: tuple[ScheduledWrite, ...]
    placement: PlacementResult
    finish_by_job: dict[str, int]
    lost: int
    max_finish: int

    def key(self) -> tuple[int, int, tuple[str, ...]]:
        return (
            self.lost,
            self.max_finish,
            tuple(sorted(job.job_id for job, _ in self.selections)),
        )


def _capacity_shape_feasible(
    nodes: tuple[Node, ...],
    existing_jobs: tuple[ExistingJob, ...],
    new_job: NewJob,
    removable_ids: frozenset[str],
) -> bool:
    """只看容量形状：移除 removable 作业后能否满足需求（忽略时间）。"""
    free = node_free_gpus(nodes, existing_jobs, {jid: 0 for jid in removable_ids})
    req = new_job.requirement
    if isinstance(req, SameMachineRequirement):
        return any(free[n.node_id] >= req.gpus for n in nodes)
    if isinstance(req, MultiNodeDomainRequirement):
        fitting = sum(
            1
            for n in nodes
            if n.domain == req.domain and free[n.node_id] >= req.gpus_per_node
        )
        return fitting >= req.nodes
    raise TypeError("未知的落位需求类型")  # pragma: no cover


def plan(
    nodes: tuple[Node, ...],
    existing_jobs: tuple[ExistingJob, ...],
    new_job: NewJob,
    disk_write_quota: int,
    current_time: int = 0,
) -> PlanResult:
    """计算抢占规划。

    Returns:
        :class:`PlanResult`；可行时含每个暂停作业的检查点、写盘开始/完成、
        资源释放时刻与新作业启动时刻；不可行时含最早失败的约束诊断。
    """
    validate_inputs(nodes, existing_jobs, new_job, disk_write_quota, current_time)
    deadline = new_job.latest_start_time

    # 可暂停作业：严格低优，且至少有一个不早于当前时刻的检查点。
    # 检查点的写盘完成时刻是否赶得上截止时间不影响“可暂停”身份——赶不上
    # 的选项只参与 LATE_START 诊断，不会产生可行解。
    eligible: list[ExistingJob] = []
    options_by_job: dict[str, tuple[CheckpointOption, ...]] = {}
    protected: list[ExistingJob] = []
    for job in existing_jobs:
        usable = tuple(
            sorted(
                (
                    cp
                    for cp in job.checkpoints
                    if cp.checkpoint_time >= current_time
                ),
                key=lambda cp: (cp.checkpoint_time, cp.lost_work, cp.write_amount),
            )
        )
        if job.priority < new_job.priority and usable:
            eligible.append(job)
            options_by_job[job.job_id] = usable
        else:
            protected.append(job)

    eligible_ids = frozenset(job.job_id for job in eligible)
    protected_ids = frozenset(job.job_id for job in protected)

    # 结构性失败优先（总卡数够但凑不出互联域/同机形状的反例）。
    if not _capacity_shape_feasible(nodes, existing_jobs, new_job, eligible_ids):
        failure = structural_failure(
            nodes, existing_jobs, new_job, eligible_ids, protected_ids
        )
        return _infeasible_result(
            new_job, disk_write_quota, current_time, deadline, failure
        )

    best_feasible: _Snapshot | None = None
    # 真实额度下最早就绪快照；无额度争用（写盘均在检查点立即开始）快照。
    best_quota: _Snapshot | None = None
    best_unbounded: _Snapshot | None = None
    oversized: list[tuple[str, int]] = []

    def evaluate(selections: list[tuple[ExistingJob, CheckpointOption]]) -> None:
        nonlocal best_feasible, best_quota, best_unbounded, oversized
        requests = [
            WriteRequest(
                job_id=job.job_id,
                earliest_start=cp.checkpoint_time,
                duration=cp.write_duration,
                amount=cp.write_amount,
            )
            for job, cp in selections
        ]
        scheduled = schedule_writes(requests, disk_write_quota)
        if scheduled is None:
            for req in requests:
                if req.amount > disk_write_quota:
                    oversized.append((req.job_id, req.amount))
            return

        finish_by_job = {w.job_id: w.write_finish for w in scheduled}
        placement = earliest_placement(nodes, existing_jobs, new_job, finish_by_job)
        if placement is None:
            return
        lost = sum(cp.lost_work for _, cp in selections)
        max_finish = max((w.write_finish for w in scheduled), default=current_time)
        snap = _Snapshot(
            selections=tuple(selections),
            scheduled=scheduled,
            placement=placement,
            finish_by_job=dict(finish_by_job),
            lost=lost,
            max_finish=max_finish,
        )
        if best_quota is None or (
            placement.ready_time,
            snap.max_finish,
            snap.key()[2],
        ) < (
            best_quota.placement.ready_time,
            best_quota.max_finish,
            best_quota.key()[2],
        ):
            best_quota = snap
        if placement.ready_time <= deadline and (
            best_feasible is None or snap.key() < best_feasible.key()
        ):
            best_feasible = snap

        # 无额度争用参照：每个写盘在自己的检查点立即开始。
        unbounded_finish = {
            job.job_id: cp.earliest_finish for job, cp in selections
        }
        placement_ub = earliest_placement(
            nodes, existing_jobs, new_job, unbounded_finish
        )
        if placement_ub is not None:
            snap_ub = _Snapshot(
                selections=tuple(selections),
                scheduled=tuple(
                    ScheduledWrite(
                        job_id=job.job_id,
                        checkpoint_time=cp.checkpoint_time,
                        write_amount=cp.write_amount,
                        write_start=cp.checkpoint_time,
                        write_finish=cp.earliest_finish,
                    )
                    for job, cp in selections
                ),
                placement=placement_ub,
                finish_by_job=dict(unbounded_finish),
                lost=lost,
                max_finish=max(
                    (cp.earliest_finish for _, cp in selections),
                    default=current_time,
                ),
            )
            if best_unbounded is None or (
                placement_ub.ready_time,
                snap_ub.max_finish,
                snap_ub.key()[2],
            ) < (
                best_unbounded.placement.ready_time,
                best_unbounded.max_finish,
                best_unbounded.key()[2],
            ):
                best_unbounded = snap_ub

    def dfs(
        idx: int,
        selections: list[tuple[ExistingJob, CheckpointOption]],
        lost_so_far: int,
    ) -> None:
        nonlocal best_feasible
        if best_feasible is not None and lost_so_far > best_feasible.lost:
            return  # 丢失工作量只增不减，严格更差的子树剪掉
        if idx == len(eligible):
            evaluate(selections)
            return

        job = eligible[idx]
        remaining_ids = frozenset(j.job_id for j in eligible[idx:])
        selected_ids = frozenset(j.job_id for j, _ in selections)

        # 分支一：不暂停该作业。
        dfs(idx + 1, selections, lost_so_far)

        # 分支二：选择某个可用检查点暂停。
        for cp in options_by_job[job.job_id]:
            candidate = selections + [(job, cp)]
            requests = [
                WriteRequest(
                    job_id=j.job_id,
                    earliest_start=option.checkpoint_time,
                    duration=option.write_duration,
                    amount=option.write_amount,
                )
                for j, option in candidate
            ]
            if schedule_writes(requests, disk_write_quota) is None:
                for req in requests:
                    if req.amount > disk_write_quota:
                        oversized.append((req.job_id, req.amount))
                continue  # 超额写盘不会因加入更多作业而缓解
            # 注意：不能因前缀写盘完成时刻晚于截止时间就剪枝——该分支虽无
            # 可行解，却是 LATE_START / 盘额度串行化诊断所需要的信息。
            if not _capacity_shape_feasible(
                nodes, existing_jobs, new_job, selected_ids | remaining_ids
            ):
                continue
            dfs(idx + 1, candidate, lost_so_far + cp.lost_work)

    dfs(0, [], 0)

    if best_feasible is not None:
        return _feasible_result(
            best_feasible,
            new_job,
            existing_jobs,
            disk_write_quota,
            current_time,
            deadline,
        )

    if best_unbounded is None:
        # 结构上可行，但所有必需暂停的写盘都超过单次额度上限。
        offending = sorted(set(oversized))
        failure = FailureConstraint(
            constraint=DISK_QUOTA,
            observed_at=current_time,
            message=(
                f"释放所需 GPU 的暂停写盘额度需求均超过集群临时写盘额度 "
                f"{disk_write_quota}，任何暂停组合都无法执行。"
            ),
            details={
                "disk_write_quota": disk_write_quota,
                "oversized_writes": [
                    {"job_id": jid, "write_amount": amount}
                    for jid, amount in offending
                ],
            },
        )
        return _infeasible_result(
            new_job, disk_write_quota, current_time, deadline, failure
        )

    failure = _build_failure(
        new_job=new_job,
        existing_jobs=existing_jobs,
        deadline=deadline,
        current_time=current_time,
        disk_write_quota=disk_write_quota,
        best_quota=best_quota,
        best_unbounded=best_unbounded,
        oversized=oversized,
    )
    return _infeasible_result(
        new_job, disk_write_quota, current_time, deadline, failure
    )


def _feasible_result(
    snap: _Snapshot,
    new_job: NewJob,
    existing_jobs: tuple[ExistingJob, ...],
    disk_write_quota: int,
    current_time: int,
    deadline: int,
) -> PlanResult:
    jobs_by_id = {job.job_id: job for job in existing_jobs}
    cp_by_job = {job.job_id: cp for job, cp in snap.selections}
    actions: list[PreemptionAction] = []
    for write in snap.scheduled:
        job = jobs_by_id[write.job_id]
        cp = cp_by_job[write.job_id]
        actions.append(
            PreemptionAction(
                job_id=write.job_id,
                checkpoint_time=write.checkpoint_time,
                write_amount=write.write_amount,
                write_start=write.write_start,
                write_finish=write.write_finish,
                release_time=write.write_finish,
                released_resources=job.gpu_usage,
                lost_work=cp.lost_work,
                recovery_time=cp.recovery_time,
            )
        )
    actions.sort(key=lambda a: (a.release_time, a.job_id))
    start_time = max(snap.placement.ready_time, current_time)
    recovery_gpu_time = sum(
        cp_by_job[write.job_id].recovery_time
        * jobs_by_id[write.job_id].total_gpus()
        for write in snap.scheduled
    )
    return PlanResult(
        feasible=True,
        new_job_id=new_job.job_id,
        start_time=start_time,
        node_allocations=snap.placement.node_allocations,
        preemption_actions=tuple(actions),
        total_lost_work=snap.lost,
        max_preemption_finish=snap.max_finish,
        total_recovery_gpu_time=recovery_gpu_time,
        disk_write_quota=disk_write_quota,
        current_time=current_time,
        latest_start_time=deadline,
        failure=None,
    )


def _build_failure(
    *,
    new_job: NewJob,
    existing_jobs: tuple[ExistingJob, ...],
    deadline: int,
    current_time: int,
    disk_write_quota: int,
    best_quota: _Snapshot | None,
    best_unbounded: _Snapshot,
    oversized: list[tuple[str, int]],
) -> FailureConstraint:
    earliest_ready = best_unbounded.placement.ready_time
    quota_ready = best_quota.placement.ready_time if best_quota else None

    quota_is_cause = (
        best_quota is None
        or (quota_ready > deadline and earliest_ready <= deadline)
    )
    if not quota_is_cause:
        # 时间型失败：即使额度无限，最早就绪仍晚于截止时间。
        allocated_nodes = {nid for nid, _ in best_unbounded.placement.node_allocations}
        gating: list[dict[str, object]] = []
        for job, cp in best_unbounded.selections:
            finish = cp.earliest_finish
            touches = any(p.node_id in allocated_nodes for p in job.gpu_usage)
            if touches and finish == earliest_ready:
                gating.append(
                    {
                        "job_id": job.job_id,
                        "checkpoint_time": cp.checkpoint_time,
                        "write_finish": finish,
                        "lost_work": cp.lost_work,
                    }
                )
        return FailureConstraint(
            constraint=LATE_START,
            observed_at=deadline,
            message=(
                f"新作业 {new_job.job_id} 最晚启动时刻为 {deadline}，但即使暂停"
                f"所有必要作业且写盘额度无限，最早也要到 {earliest_ready} 才能"
                f"落位；卡住在门控作业的检查点/写盘完成时刻上。"
            ),
            details={
                "latest_start_time": deadline,
                "earliest_achievable_start": earliest_ready,
                "preempted_job_ids": best_unbounded.key()[2],
                "gating_jobs": gating,
            },
        )

    # 额度型失败：无争用时可按时落位，真实额度下被串行化拖过截止时间。
    if best_quota is None:
        offending = sorted(set(oversized))
        return FailureConstraint(
            constraint=DISK_QUOTA,
            observed_at=current_time,
            message=(
                f"必要暂停作业的单次写盘额度需求超过集群临时写盘额度 "
                f"{disk_write_quota}，任何暂停组合都无法执行。"
            ),
            details={
                "disk_write_quota": disk_write_quota,
                "oversized_writes": [
                    {"job_id": jid, "write_amount": amount}
                    for jid, amount in offending
                ],
                "earliest_achievable_start_without_quota_contention": earliest_ready,
            },
        )

    delayed = [w for w in best_quota.scheduled if w.delayed]
    first_delay = min((w.write_start for w in delayed), default=current_time)
    return FailureConstraint(
        constraint=DISK_QUOTA,
        observed_at=first_delay,
        message=(
            f"临时写盘额度 {disk_write_quota} 迫使暂停写盘串行排队，新作业 "
            f"{new_job.job_id} 最早就绪时刻被推迟到 {quota_ready}，晚于最晚启动 "
            f"时刻 {deadline}；若无额度争用最早可在 {earliest_ready} 就绪。"
        ),
        details={
            "disk_write_quota": disk_write_quota,
            "latest_start_time": deadline,
            "earliest_start_with_quota": quota_ready,
            "earliest_start_without_quota_contention": earliest_ready,
            "first_contention_time": first_delay,
            "delayed_writes": [
                {
                    "job_id": w.job_id,
                    "checkpoint_time": w.checkpoint_time,
                    "write_start": w.write_start,
                    "write_finish": w.write_finish,
                    "write_amount": w.write_amount,
                }
                for w in sorted(delayed, key=lambda w: (w.write_start, w.job_id))
            ],
        },
    )


def _infeasible_result(
    new_job: NewJob,
    disk_write_quota: int,
    current_time: int,
    deadline: int,
    failure: FailureConstraint,
) -> PlanResult:
    return PlanResult(
        feasible=False,
        new_job_id=new_job.job_id,
        start_time=None,
        node_allocations=None,
        preemption_actions=(),
        total_lost_work=0,
        max_preemption_finish=None,
        total_recovery_gpu_time=0,
        disk_write_quota=disk_write_quota,
        current_time=current_time,
        latest_start_time=deadline,
        failure=failure,
    )
