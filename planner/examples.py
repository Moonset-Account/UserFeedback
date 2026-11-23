"""本地合成的小实例工厂与命令行演示。

仅使用合成数据，不访问任何真实集群或网络目标。运行：

    python3 -m planner.examples
"""

from __future__ import annotations

from planner.model import (
    CheckpointOption,
    ExistingJob,
    JobPlacement,
    MultiNodeDomainRequirement,
    NewJob,
    Node,
    SameMachineRequirement,
)
from planner.search import PlanResult, plan


def _eight_node_cluster() -> tuple[Node, ...]:
    # 8 台 8 卡节点：n1-n4 属互联域 D-A，n5-n8 属互联域 D-B。
    return (
        Node("n1", 8, "D-A"),
        Node("n2", 8, "D-A"),
        Node("n3", 8, "D-A"),
        Node("n4", 8, "D-A"),
        Node("n5", 8, "D-B"),
        Node("n6", 8, "D-B"),
        Node("n7", 8, "D-B"),
        Node("n8", 8, "D-B"),
    )


def _cp(
    t: int,
    duration: int = 2,
    amount: int = 1,
    recovery: int = 1,
    lost: int = 0,
) -> CheckpointOption:
    return CheckpointOption(
        checkpoint_time=t,
        write_duration=duration,
        write_amount=amount,
        recovery_time=recovery,
        lost_work=lost,
    )


def fragmentation_counterexample() -> (
    tuple[tuple[Node, ...], tuple[ExistingJob, ...], NewJob, int]
):
    """反例：总空闲卡 48 >= 需求 32，却凑不齐一个互联域的 4 台 8 卡机器。

    D-A 的 4 台节点各被高优作业锁死 4 卡（每台仅剩 4 卡），D-B 的 4 台
    节点全空（32 卡），但新作业明确要求 D-A 内 4 台各 8 卡。
    """
    nodes = _eight_node_cluster()
    locked_jobs = tuple(
        ExistingJob(
            job_id=f"lock-{nid}",
            priority=20,  # 高于新作业，不可暂停
            gpu_usage=(JobPlacement(nid, 4),),
            checkpoints=(_cp(1000),),
        )
        for nid in ("n1", "n2", "n3", "n4")
    )
    new_job = NewJob(
        job_id="train-new",
        priority=10,
        requirement=MultiNodeDomainRequirement("D-A", nodes=4, gpus_per_node=8),
        latest_start_time=100,
    )
    return nodes, locked_jobs, new_job, 4


def simple_preemption_scenario() -> (
    tuple[tuple[Node, ...], tuple[ExistingJob, ...], NewJob, int]
):
    """常规抢占：n1 被低优作业占满，暂停它才能在截止前落位同机 8 卡。"""
    nodes = (
        Node("n1", 8, "D-A"),
        Node("n2", 8, "D-A"),
    )
    jobs = (
        ExistingJob(
            job_id="low-1",
            priority=1,
            gpu_usage=(JobPlacement("n1", 8),),
            checkpoints=(
                _cp(5, duration=2, amount=2, recovery=3, lost=10),
                _cp(15, duration=2, amount=2, recovery=3, lost=4),
            ),
        ),
        ExistingJob(
            job_id="low-2",
            priority=2,
            gpu_usage=(JobPlacement("n2", 8),),
            checkpoints=(_cp(8, duration=2, amount=2, recovery=2, lost=3),),
        ),
    )
    new_job = NewJob(
        job_id="train-new",
        priority=10,
        requirement=SameMachineRequirement(gpus=8),
        latest_start_time=30,
    )
    return nodes, jobs, new_job, 4


def quota_interleaving_scenario() -> (
    tuple[tuple[Node, ...], tuple[ExistingJob, ...], NewJob, int]
):
    """盘额度交错：D-A 的两台机器被占满，两个写盘同时到达但额度只允许串行。"""
    nodes = (
        Node("n1", 8, "D-A"),
        Node("n2", 8, "D-A"),
        Node("n5", 8, "D-B"),
        Node("n6", 8, "D-B"),
    )
    jobs = (
        ExistingJob(
            job_id="low-1",
            priority=1,
            gpu_usage=(JobPlacement("n1", 8),),
            checkpoints=(_cp(10, duration=4, amount=3, recovery=2, lost=5),),
        ),
        ExistingJob(
            job_id="low-2",
            priority=1,
            gpu_usage=(JobPlacement("n2", 8),),
            checkpoints=(_cp(10, duration=4, amount=3, recovery=2, lost=5),),
        ),
    )
    new_job = NewJob(
        job_id="train-new",
        priority=10,
        requirement=MultiNodeDomainRequirement("D-A", nodes=2, gpus_per_node=8),
        latest_start_time=20,
    )
    return nodes, jobs, new_job, 4


def render(result: PlanResult) -> str:
    """把规划结果渲染成可读文本。"""
    lines = [f"新作业 {result.new_job_id} 的抢占规划（feasible={result.feasible}）"]
    if result.feasible:
        lines.append(f"  启动时刻: {result.start_time}（最晚 {result.latest_start_time}）")
        lines.append(f"  落位: {dict(result.node_allocations)}")
        lines.append(f"  总丢失工作量: {result.total_lost_work}")
        lines.append(
            f"  最晚暂停完成时刻: {result.max_preemption_finish}；"
            f"恢复总 GPU-时间: {result.total_recovery_gpu_time}"
        )
        for action in result.preemption_actions:
            delayed = "（排队延后）" if action.write_was_delayed else ""
            lines.append(
                f"  - 暂停 {action.job_id}: 检查点 t={action.checkpoint_time}, "
                f"写盘 [{action.write_start}, {action.write_finish}) "
                f"占额度 {action.write_amount}{delayed}, "
                f"释放 t={action.release_time}, 丢失={action.lost_work}, "
                f"恢复={action.recovery_time}"
            )
    else:
        assert result.failure is not None
        lines.append(
            f"  最早失败约束[{result.failure.constraint}] "
            f"@t={result.failure.observed_at}: {result.failure.message}"
        )
        lines.append(f"  细节: {result.failure.details}")
    return "\n".join(lines)


def demo() -> str:
    outputs = []
    for name, factory in (
        ("反例：总卡够但凑不齐互联域", fragmentation_counterexample),
        ("常规抢占", simple_preemption_scenario),
        ("盘额度交错", quota_interleaving_scenario),
    ):
        nodes, jobs, new_job, quota = factory()
        outputs.append(f"=== {name} ===")
        outputs.append(render(plan(nodes, jobs, new_job, quota)))
    text = "\n".join(outputs)
    print(text)
    return text


if __name__ == "__main__":
    demo()
