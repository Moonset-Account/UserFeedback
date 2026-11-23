"""验收语义补充：目标优先级与跨节点作业原子释放。"""

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


def cp(t: int, duration: int = 2, amount: int = 1, recovery: int = 1, lost: int = 1):
    return CheckpointOption(t, duration, amount, recovery, lost)


class ObjectivePriorityTest(unittest.TestCase):
    def test_feasibility_dominates_lost_work(self):
        # 暂停 cheap 能落位但释放时刻晚于截止；暂停 costly 可按时落位。
        # 规划器必须选 costly：先保证按时落位，再谈丢失工作量。
        nodes = (Node("n1", 8, "D-A"), Node("n2", 8, "D-A"))
        jobs = (
            ExistingJob(
                "cheap",
                1,
                (JobPlacement("n1", 8),),
                (cp(20, duration=2, amount=1, lost=1),),  # 22 才释放
            ),
            ExistingJob(
                "costly",
                1,
                (JobPlacement("n2", 8),),
                (cp(5, duration=2, amount=1, lost=100),),  # 7 释放
            ),
        )
        new_job = NewJob("new", 10, SameMachineRequirement(8), 15)
        result = plan(nodes, jobs, new_job, disk_write_quota=4)
        self.assertTrue(result.feasible)
        self.assertEqual(
            [a.job_id for a in result.preemption_actions], ["costly"]
        )
        self.assertEqual(result.total_lost_work, 100)
        self.assertLessEqual(result.start_time, 15)

    def test_lost_work_dominates_finish_time(self):
        # 两个方案都按时：early 完成早但丢失大，late 完成晚但丢失小。
        nodes = (Node("n1", 8, "D-A"), Node("n2", 8, "D-A"))
        jobs = (
            ExistingJob(
                "early",
                1,
                (JobPlacement("n1", 8),),
                (cp(3, duration=1, amount=1, lost=50),),
            ),
            ExistingJob(
                "late",
                1,
                (JobPlacement("n2", 8),),
                (cp(10, duration=1, amount=1, lost=2),),
            ),
        )
        new_job = NewJob("new", 10, SameMachineRequirement(8), 30)
        result = plan(nodes, jobs, new_job, disk_write_quota=4)
        self.assertTrue(result.feasible)
        self.assertEqual(
            [a.job_id for a in result.preemption_actions], ["late"]
        )
        self.assertEqual(result.total_lost_work, 2)


class MultiNodeJobAtomicReleaseTest(unittest.TestCase):
    def test_cross_node_job_releases_all_gpus_at_write_finish(self):
        # 一个作业横跨 n1/n2 各 4 卡；新作业要求 D-A 两台各 8 卡。
        nodes = (
            Node("n1", 8, "D-A"),
            Node("n2", 8, "D-A"),
        )
        span = ExistingJob(
            "span",
            1,
            (JobPlacement("n1", 4), JobPlacement("n2", 4)),
            (cp(6, duration=3, amount=2, recovery=4, lost=9),),
        )
        filler = ExistingJob(
            "filler",
            1,
            (JobPlacement("n1", 4), JobPlacement("n2", 4)),
            (cp(6, duration=3, amount=2, recovery=1, lost=9),),
        )
        new_job = NewJob(
            "new",
            10,
            MultiNodeDomainRequirement("D-A", 2, 8),
            latest_start_time=20,
        )
        result = plan(nodes, (span, filler), new_job, disk_write_quota=4)
        self.assertTrue(result.feasible)
        self.assertEqual(len(result.preemption_actions), 2)
        # 每个动作释放的资源必须完整列出该作业在所有节点上的占用
        span_action = next(
            a for a in result.preemption_actions if a.job_id == "span"
        )
        self.assertEqual(
            span_action.released_resources,
            (JobPlacement("n1", 4), JobPlacement("n2", 4)),
        )
        # 同一时刻（写盘完成）整体释放，启动时刻不早于任一释放
        self.assertEqual(result.start_time, result.max_preemption_finish)
        for action in result.preemption_actions:
            self.assertEqual(action.release_time, action.write_finish)
        # 恢复代价按作业总卡数（8 卡）× 恢复时长
        self.assertIn(span_action.recovery_time, (4,))
        self.assertEqual(span_action.recovery_time * 8, 32)


if __name__ == "__main__":
    unittest.main()
