"""独立暴力枚举交叉验证。

测试里用与生产实现不同的写法（逐分钟时间扫描 + itertools 全组合枚举）
重新求解一批合成实例，核对规划器给出的可行性与字典序最优目标值。
"""

import itertools
import random
import time
import unittest

from planner.model import (
    CheckpointOption,
    ExistingJob,
    JobPlacement,
    MultiNodeDomainRequirement,
    NewJob,
    Node,
    SameMachineRequirement,
)
from planner.search import plan


def independent_quota(requests, quota):
    """逐分钟扫描的独立额度调度，返回 {job_id: finish} 或 None。

    与生产实现刻意采用不同的控制结构：外层按自然时间逐分钟推进，内层在
    每个时刻反复尝试放入所有已到达的请求。
    """
    if any(r.amount > quota for r in requests):
        return None
    pending = sorted(requests, key=lambda r: (r.earliest_start, r.job_id))
    active = []  # (job_id, finish, amount)
    finishes = {}
    t = min((r.earliest_start for r in pending), default=0)
    # 上界：最晚检查点时刻 + 全部写盘严格串行的总时长，必定足够
    horizon = max((r.earliest_start for r in pending), default=t) + sum(
        r.duration for r in pending
    ) + 1
    while pending or active:
        active = [a for a in active if a[1] > t]
        used = sum(a[2] for a in active)
        progress = True
        while progress:
            progress = False
            for req in list(pending):
                if req.earliest_start <= t and used + req.amount <= quota:
                    active.append((req.job_id, t + req.duration, req.amount))
                    finishes[req.job_id] = t + req.duration
                    used += req.amount
                    pending.remove(req)
                    progress = True
        t += 1
        if t > horizon:  # pragma: no cover
            return None
    return finishes


def independent_placement(nodes, jobs, finish_by_job, req):
    # 注意：必须按节点聚合占用，同一节点可能被多个（或一个作业的多条）记录占用
    free = {n.node_id: n.num_gpus for n in nodes}
    for job in jobs:
        if job.job_id in finish_by_job:
            continue
        for p in job.gpu_usage:
            free[p.node_id] -= p.gpus
    ready_of = {}
    for node in nodes:
        times = [
            finish_by_job[job.job_id]
            for job in jobs
            if job.job_id in finish_by_job
            and any(p.node_id == node.node_id for p in job.gpu_usage)
        ]
        ready_of[node.node_id] = max(times, default=0)

    if isinstance(req, SameMachineRequirement):
        fit = [n for n in nodes if free[n.node_id] >= req.gpus]
        if not fit:
            return None
        chosen = min(fit, key=lambda n: (ready_of[n.node_id], n.node_id))
        return ready_of[chosen.node_id]
    if isinstance(req, MultiNodeDomainRequirement):
        fit = [
            n
            for n in nodes
            if n.domain == req.domain and free[n.node_id] >= req.gpus_per_node
        ]
        if len(fit) < req.nodes:
            return None
        chosen = sorted(fit, key=lambda n: (ready_of[n.node_id], n.node_id))[
            : req.nodes
        ]
        return max(ready_of[n.node_id] for n in chosen)
    raise TypeError  # pragma: no cover


def brute_force(nodes, jobs, new_job, quota, current_time):
    """全组合枚举，返回 (feasible, best_key_or_None)。"""
    deadline = new_job.latest_start_time
    choices = []
    for job in jobs:
        usable = [
            cp
            for cp in job.checkpoints
            if cp.checkpoint_time >= current_time
            and cp.earliest_finish <= deadline
        ]
        if job.priority >= new_job.priority or not usable:
            choices.append([None])
        else:
            choices.append([None, *usable])

    best = None
    any_feasible = False
    for combo in itertools.product(*choices):
        selected = [
            (job, cp)
            for job, cp in zip(jobs, combo)
            if cp is not None
        ]
        requests = [
            type("R", (), {
                "job_id": job.job_id,
                "earliest_start": cp.checkpoint_time,
                "duration": cp.write_duration,
                "amount": cp.write_amount,
            })()
            for job, cp in selected
        ]
        finishes = independent_quota(requests, quota)
        if finishes is None:
            continue
        ready = independent_placement(
            nodes, jobs, finishes, new_job.requirement
        )
        if ready is None:
            continue
        if ready <= deadline:
            any_feasible = True
            key = (
                sum(cp.lost_work for _, cp in selected),
                max(finishes.values(), default=current_time),
                tuple(sorted(job.job_id for job, _ in selected)),
            )
            if best is None or key < best:
                best = key
    return any_feasible, best


def make_random_instance(rng, num_nodes=6, num_jobs=6):
    domains = ["D-A", "D-B"]
    nodes = tuple(
        Node(f"n{i}", 8, domains[i % 2]) for i in range(1, num_nodes + 1)
    )
    jobs = []
    for j in range(num_jobs):
        node = rng.choice(nodes)
        gpus = rng.choice([2, 4, 8])
        if gpus > node.num_gpus:
            gpus = 8
        cps = tuple(
            CheckpointOption(
                checkpoint_time=rng.choice([3, 6, 10, 14]),
                write_duration=rng.choice([1, 2, 3]),
                write_amount=rng.choice([1, 2, 3]),
                recovery_time=rng.choice([1, 2]),
                lost_work=rng.choice([1, 3, 5, 8]),
            )
            for _ in range(rng.choice([1, 2]))
        )
        jobs.append(
            ExistingJob(
                f"j{j}",
                priority=rng.choice([1, 2, 20]),
                gpu_usage=(JobPlacement(node.node_id, gpus),),
                checkpoints=cps,
            )
        )
    req = rng.choice(
        [
            SameMachineRequirement(rng.choice([4, 8])),
            MultiNodeDomainRequirement(
                "D-A", rng.choice([1, 2]), rng.choice([4, 8])
            ),
        ]
    )
    new_job = NewJob(
        "new", priority=10, requirement=req, latest_start_time=rng.choice([12, 20, 30])
    )
    return nodes, tuple(jobs), new_job, rng.choice([2, 4, 6])


class ExhaustiveCrossValidationTest(unittest.TestCase):
    def test_cross_validate_40_random_instances(self):
        rng = random.Random(20261123)
        for _ in range(40):
            nodes, jobs, new_job, quota = make_random_instance(rng)
            result = plan(nodes, jobs, new_job, quota)
            feasible, best_key = brute_force(
                nodes, jobs, new_job, quota, result.current_time
            )
            self.assertEqual(result.feasible, feasible)
            if feasible:
                produced_key = (
                    result.total_lost_work,
                    result.max_preemption_finish,
                    tuple(sorted(a.job_id for a in result.preemption_actions)),
                )
                self.assertEqual(produced_key, best_key)
                self.assertLessEqual(result.start_time, result.latest_start_time)
                # 执行性：每个释放时刻都不早于对应写盘完成时刻
                for action in result.preemption_actions:
                    self.assertEqual(action.release_time, action.write_finish)

    def test_ten_job_scale_instance_runs_fast_and_matches_brute_force(self):
        rng = random.Random(42)
        nodes, jobs, new_job, quota = make_random_instance(
            rng, num_nodes=8, num_jobs=10
        )
        start = time.perf_counter()
        result = plan(nodes, jobs, new_job, quota)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 10.0)

        feasible, best_key = brute_force(
            nodes, jobs, new_job, quota, result.current_time
        )
        self.assertEqual(result.feasible, feasible)
        if feasible:
            produced_key = (
                result.total_lost_work,
                result.max_preemption_finish,
                tuple(sorted(a.job_id for a in result.preemption_actions)),
            )
            self.assertEqual(produced_key, best_key)

    def test_planner_is_deterministic(self):
        rng = random.Random(7)
        nodes, jobs, new_job, quota = make_random_instance(rng)
        first = plan(nodes, jobs, new_job, quota)
        second = plan(nodes, jobs, new_job, quota)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
