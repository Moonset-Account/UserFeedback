import unittest

from planner.diagnose import (
    DISK_QUOTA,
    LATE_START,
    STRUCTURAL,
)
from planner.examples import fragmentation_counterexample
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


def cp(t: int, duration: int = 2, amount: int = 1, recovery: int = 1, lost: int = 1):
    return CheckpointOption(t, duration, amount, recovery, lost)


class FragmentationCounterexampleTest(unittest.TestCase):
    def test_total_gpus_suffice_but_domain_cannot_be_assembled(self):
        nodes, jobs, new_job, quota = fragmentation_counterexample()
        total_free = sum(
            n.num_gpus for n in nodes
        ) - sum(j.total_gpus() for j in jobs)
        needed = new_job.requirement.nodes * new_job.requirement.gpus_per_node
        # 反例前提：总空闲卡数看似够用
        self.assertGreaterEqual(total_free, needed)
        self.assertEqual(total_free, 48)
        self.assertEqual(needed, 32)

        result = plan(nodes, jobs, new_job, quota)
        self.assertFalse(result.feasible)
        self.assertEqual(result.failure.constraint, STRUCTURAL)
        self.assertEqual(result.failure.observed_at, 0)
        self.assertEqual(result.failure.details["domain"], "D-A")
        self.assertEqual(result.failure.details["nodes_needed"], 4)
        self.assertEqual(result.failure.details["nodes_satisfying"], 0)
        self.assertEqual(
            result.failure.details["total_free_gpus_if_all_paused"], 48
        )

    def test_same_machine_fragmentation_counterexample(self):
        # 两台 8 卡机各被锁 4 卡：总空闲 8 卡，但任何单机都凑不齐 8 卡。
        nodes = (Node("n1", 8, "D-A"), Node("n2", 8, "D-A"))
        jobs = (
            ExistingJob(
                "lock-1", 99, (JobPlacement("n1", 4),), (cp(100),)
            ),
            ExistingJob(
                "lock-2", 99, (JobPlacement("n2", 4),), (cp(100),)
            ),
        )
        result = plan(
            nodes,
            jobs,
            NewJob("new", 10, SameMachineRequirement(8), 50),
            disk_write_quota=4,
        )
        self.assertFalse(result.feasible)
        self.assertEqual(result.failure.constraint, STRUCTURAL)
        self.assertEqual(
            result.failure.details["max_free_gpus_on_single_node"], 4
        )


class TimingFailureTest(unittest.TestCase):
    def test_late_start_when_checkpoint_finishes_after_deadline(self):
        nodes = (Node("n1", 8, "D-A"),)
        jobs = (
            ExistingJob(
                "low",
                1,
                (JobPlacement("n1", 8),),
                # 检查点 t=20，写 5 分钟 -> 25 才释放，截止 18 无法落位
                (cp(20, duration=5, amount=1, lost=3),),
            ),
        )
        result = plan(
            nodes,
            jobs,
            NewJob("new", 10, SameMachineRequirement(8), 18),
            disk_write_quota=8,
        )
        self.assertFalse(result.feasible)
        self.assertEqual(result.failure.constraint, LATE_START)
        self.assertEqual(result.failure.observed_at, 18)
        self.assertEqual(
            result.failure.details["earliest_achievable_start"], 25
        )
        gating = result.failure.details["gating_jobs"]
        self.assertEqual([g["job_id"] for g in gating], ["low"])

    def test_checkpoints_in_the_past_are_ignored(self):
        nodes = (Node("n1", 8, "D-A"),)
        jobs = (
            ExistingJob(
                "low",
                1,
                (JobPlacement("n1", 8),),
                (cp(-10, duration=1), cp(-2, duration=1)),
            ),
        )
        # 两个检查点都早于 current_time，作业事实上无法在窗口内暂停
        result = plan(
            nodes,
            jobs,
            NewJob("new", 10, SameMachineRequirement(8), 30),
            disk_write_quota=4,
            current_time=0,
        )
        self.assertFalse(result.feasible)
        self.assertEqual(result.failure.constraint, STRUCTURAL)
        self.assertIn("low", result.failure.details["protected_job_ids"])


class QuotaFailureTest(unittest.TestCase):
    def test_serialization_pushes_start_past_deadline(self):
        nodes = (
            Node("n1", 8, "D-A"),
            Node("n2", 8, "D-A"),
        )
        jobs = (
            ExistingJob(
                "low-1",
                1,
                (JobPlacement("n1", 8),),
                (cp(10, duration=4, amount=3, lost=5),),
            ),
            ExistingJob(
                "low-2",
                1,
                (JobPlacement("n2", 8),),
                (cp(10, duration=4, amount=3, lost=5),),
            ),
        )
        new_job = NewJob(
            "new",
            10,
            MultiNodeDomainRequirement("D-A", 2, 8),
            latest_start_time=15,
        )
        # 额度 4：两个 amount=3 的写盘只能串行 -> 完成 14 和 18，18 > 15
        result = plan(nodes, jobs, new_job, disk_write_quota=4)
        self.assertFalse(result.feasible)
        self.assertEqual(result.failure.constraint, DISK_QUOTA)
        self.assertEqual(
            result.failure.details["earliest_start_with_quota"], 18
        )
        self.assertEqual(
            result.failure.details["earliest_start_without_quota_contention"],
            14,
        )
        delayed = result.failure.details["delayed_writes"]
        self.assertEqual(len(delayed), 1)
        self.assertEqual(delayed[0]["write_start"], 14)

        # 额度放宽到 6 后两个写盘并行，14 即可落位，变得可行
        relaxed = plan(nodes, jobs, new_job, disk_write_quota=6)
        self.assertTrue(relaxed.feasible)
        self.assertEqual(relaxed.start_time, 14)
        self.assertEqual(relaxed.total_lost_work, 10)

    def test_oversized_write_can_never_be_scheduled(self):
        nodes = (Node("n1", 8, "D-A"),)
        jobs = (
            ExistingJob(
                "low",
                1,
                (JobPlacement("n1", 8),),
                (cp(5, duration=2, amount=10, lost=1),),
            ),
        )
        result = plan(
            nodes,
            jobs,
            NewJob("new", 10, SameMachineRequirement(8), 30),
            disk_write_quota=4,
        )
        self.assertFalse(result.feasible)
        self.assertEqual(result.failure.constraint, DISK_QUOTA)
        oversized = result.failure.details["oversized_writes"]
        self.assertEqual(oversized, [{"job_id": "low", "write_amount": 10}])


if __name__ == "__main__":
    unittest.main()
