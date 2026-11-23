import unittest

from planner.model import (
    CheckpointOption,
    ExistingJob,
    JobPlacement,
    NewJob,
    Node,
    SameMachineRequirement,
)
from planner.search import plan


class NormalPlacementTest(unittest.TestCase):
    def test_no_preemption_needed_when_free_capacity_exists(self):
        nodes = (Node("n1", 8, "D-A"), Node("n2", 8, "D-A"))
        jobs = (
            ExistingJob(
                "j1",
                1,
                (JobPlacement("n1", 4),),
                (CheckpointOption(5, 2, 1, 1, 10),),
            ),
        )
        result = plan(
            nodes,
            jobs,
            NewJob("new", 10, SameMachineRequirement(8), 30),
            disk_write_quota=4,
        )
        self.assertTrue(result.feasible)
        self.assertEqual(result.preemption_actions, ())
        self.assertEqual(result.total_lost_work, 0)
        self.assertEqual(result.start_time, 0)
        self.assertEqual(result.node_allocations, (("n2", 8),))

    def test_simple_preemption_timeline_is_executable(self):
        nodes = (Node("n1", 8, "D-A"),)
        jobs = (
            ExistingJob(
                "low",
                1,
                (JobPlacement("n1", 8),),
                (CheckpointOption(5, 2, 2, recovery_time=3, lost_work=10),),
            ),
        )
        result = plan(
            nodes,
            jobs,
            NewJob("new", 10, SameMachineRequirement(8), 30),
            disk_write_quota=4,
        )
        self.assertTrue(result.feasible)
        self.assertEqual(len(result.preemption_actions), 1)
        action = result.preemption_actions[0]
        self.assertEqual(action.job_id, "low")
        self.assertEqual(action.checkpoint_time, 5)
        self.assertEqual(action.write_start, 5)
        self.assertEqual(action.write_finish, 7)
        self.assertEqual(action.release_time, 7)
        self.assertEqual(action.released_resources, (JobPlacement("n1", 8),))
        # 已完成暂停的资源才参与新落位：启动时刻不早于释放时刻
        self.assertEqual(result.start_time, 7)
        self.assertLessEqual(result.start_time, result.latest_start_time)
        self.assertEqual(result.total_recovery_gpu_time, 3 * 8)


class CheckpointSelectionTest(unittest.TestCase):
    def _job_with_two_checkpoints(self) -> ExistingJob:
        return ExistingJob(
            "low",
            1,
            (JobPlacement("n1", 8),),
            (
                CheckpointOption(5, 2, 2, recovery_time=1, lost_work=10),
                CheckpointOption(15, 2, 2, recovery_time=1, lost_work=4),
            ),
        )

    def test_chooses_checkpoint_minimizing_lost_work_when_deadline_allows(self):
        nodes = (Node("n1", 8, "D-A"),)
        jobs = (self._job_with_two_checkpoints(),)
        result = plan(
            nodes,
            jobs,
            NewJob("new", 10, SameMachineRequirement(8), 30),
            disk_write_quota=4,
        )
        self.assertTrue(result.feasible)
        action = result.preemption_actions[0]
        self.assertEqual(action.checkpoint_time, 15)
        self.assertEqual(action.write_finish, 17)
        self.assertEqual(result.total_lost_work, 4)
        self.assertEqual(result.start_time, 17)

    def test_falls_back_to_earlier_checkpoint_when_deadline_tight(self):
        nodes = (Node("n1", 8, "D-A"),)
        jobs = (self._job_with_two_checkpoints(),)
        result = plan(
            nodes,
            jobs,
            NewJob("new", 10, SameMachineRequirement(8), 16),
            disk_write_quota=4,
        )
        self.assertTrue(result.feasible)
        action = result.preemption_actions[0]
        # t=15 的检查点写完要到 17，超过截止 16；只能选 t=5
        self.assertEqual(action.checkpoint_time, 5)
        self.assertEqual(action.write_finish, 7)
        self.assertEqual(result.total_lost_work, 10)


class PriorityTest(unittest.TestCase):
    def test_equal_and_higher_priority_jobs_are_never_preempted(self):
        nodes = (Node("n1", 8, "D-A"),)
        protected = ExistingJob(
            "protected",
            10,  # 与新作业同优先级
            (JobPlacement("n1", 8),),
            (CheckpointOption(5, 2, 2, 1, 1),),
        )
        result = plan(
            nodes,
            (protected,),
            NewJob("new", 10, SameMachineRequirement(8), 30),
            disk_write_quota=4,
        )
        self.assertFalse(result.feasible)
        self.assertEqual(result.preemption_actions, ())
        self.assertIn("protected", result.failure.details["protected_job_ids"])


if __name__ == "__main__":
    unittest.main()
