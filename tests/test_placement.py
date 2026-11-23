import unittest

from planner.model import (
    ExistingJob,
    JobPlacement,
    MultiNodeDomainRequirement,
    NewJob,
    Node,
    SameMachineRequirement,
    CheckpointOption,
)
from planner.placement import (
    earliest_placement,
    node_free_gpus,
    structurally_feasible,
)


def nodes() -> tuple[Node, ...]:
    return (
        Node("n1", 8, "D-A"),
        Node("n2", 8, "D-A"),
        Node("n3", 8, "D-B"),
    )


def job(
    job_id: str,
    usage: tuple[JobPlacement, ...],
    priority: int = 1,
) -> ExistingJob:
    return ExistingJob(
        job_id=job_id,
        priority=priority,
        gpu_usage=usage,
        checkpoints=(
            CheckpointOption(
                checkpoint_time=5,
                write_duration=2,
                write_amount=1,
                recovery_time=1,
                lost_work=1,
            ),
        ),
    )


class PlacementTest(unittest.TestCase):
    def test_free_gpus_accounts_for_running_and_paused_jobs(self):
        n = nodes()
        jobs = (
            job("j1", (JobPlacement("n1", 3),)),
            job("j2", (JobPlacement("n1", 2), JobPlacement("n2", 4))),
        )
        free = node_free_gpus(n, jobs, {"j1": 7})  # j1 已暂停释放
        # n1: 8 卡中仅 j2 的 2 卡仍被占用 -> 空闲 6
        self.assertEqual(free["n1"], 6)
        self.assertEqual(free["n2"], 8 - 4)
        self.assertEqual(free["n3"], 8)

    def test_same_machine_picks_earliest_ready_node(self):
        n = (
            Node("n1", 8, "D-A"),
            Node("n2", 8, "D-A"),
        )
        jobs = (
            job("j1", (JobPlacement("n1", 8),)),
            job("j2", (JobPlacement("n2", 8),)),
        )
        result = earliest_placement(
            n,
            jobs,
            NewJob("new", 10, SameMachineRequirement(8), 30),
            {"j1": 12, "j2": 9},
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.ready_time, 9)
        self.assertEqual(result.node_allocations, (("n2", 8),))

    def test_same_machine_none_when_no_node_fits(self):
        n = (Node("n1", 8, "D-A"), Node("n2", 8, "D-A"))
        # 每台 8 卡机都被占用 4 卡：任何单机都凑不齐 8 卡
        jobs = (
            job("j1", (JobPlacement("n1", 4),)),
            job("j2", (JobPlacement("n2", 4),)),
        )
        result = earliest_placement(
            n,
            jobs,
            NewJob("new", 10, SameMachineRequirement(8), 30),
            {},
        )
        self.assertIsNone(result)

    def test_multi_node_domain_requires_enough_nodes_in_one_domain(self):
        n = nodes()
        jobs = (
            job("j1", (JobPlacement("n1", 8),)),
            job("j2", (JobPlacement("n2", 8),)),
        )
        new_job = NewJob(
            "new", 10, MultiNodeDomainRequirement("D-A", 2, 8), 30
        )
        # 只暂停 j1：n1 释放但 n2 仍被 j2 占满，D-A 里仅 1 台满足
        result = earliest_placement(n, jobs, new_job, {"j1": 5})
        self.assertIsNone(result)

        result2 = earliest_placement(n, jobs, new_job, {"j1": 5, "j2": 8})
        self.assertIsNotNone(result2)
        self.assertEqual(result2.ready_time, 8)
        self.assertEqual(
            dict(result2.node_allocations), {"n1": 8, "n2": 8}
        )

    def test_structurally_feasible_only_considers_eligible_jobs(self):
        n = (Node("n1", 12, "D-A"),)
        jobs = (
            job("locked", (JobPlacement("n1", 4),), priority=99),
            job("movable", (JobPlacement("n1", 8),), priority=1),
        )
        new_job = NewJob("new", 10, SameMachineRequirement(8), 30)
        # 暂停 movable 后节点空闲 8 卡，可落位
        self.assertTrue(
            structurally_feasible(n, jobs, new_job, frozenset({"movable"}))
        )
        # movable 不可暂停时只剩 4 卡空闲，无法落位
        self.assertFalse(
            structurally_feasible(n, jobs, new_job, frozenset())
        )


if __name__ == "__main__":
    unittest.main()
