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
from planner.quota import WriteRequest, schedule_writes
from planner.search import plan


def cp(t: int, duration: int = 2, amount: int = 1, recovery: int = 1, lost: int = 1):
    return CheckpointOption(t, duration, amount, recovery, lost)


class QuotaInterleavingTest(unittest.TestCase):
    def test_staggered_checkpoints_pack_without_idle_gaps(self):
        # j1 [0,6) amount 3；j2 t=4 到达 amount 2，需等 t=6；j3 t=6 到达。
        result = schedule_writes(
            [
                WriteRequest("j1", 0, 6, 3),
                WriteRequest("j2", 4, 3, 2),
                WriteRequest("j3", 6, 2, 2),
            ],
            quota=4,
        )
        by_id = {w.job_id: w for w in result}
        self.assertEqual((by_id["j2"].write_start, by_id["j2"].write_finish), (6, 9))
        # t=6 时 j2 占 2，j3(amount 2) 恰好同时放入
        self.assertEqual((by_id["j3"].write_start, by_id["j3"].write_finish), (6, 8))

    def test_three_way_serialization_feasible_before_deadline(self):
        nodes = tuple(
            Node(f"n{i}", 8, "D-A") for i in range(1, 4)
        )
        jobs = tuple(
            ExistingJob(
                f"low-{i}",
                1,
                (JobPlacement(f"n{i}", 8),),
                (cp(10, duration=2, amount=3, lost=2),),
            )
            for i in range(1, 4)
        )
        new_job = NewJob(
            "new",
            10,
            MultiNodeDomainRequirement("D-A", 3, 8),
            latest_start_time=20,
        )
        # 额度 4：三个 amount=3 的写盘串行，完成时刻 12/14/16
        result = plan(nodes, jobs, new_job, disk_write_quota=4)
        self.assertTrue(result.feasible)
        finishes = sorted(a.write_finish for a in result.preemption_actions)
        self.assertEqual(finishes, [12, 14, 16])
        self.assertEqual(result.start_time, 16)
        self.assertEqual(result.total_lost_work, 6)
        # 写盘启动顺序按作业 ID 确定
        starts = {a.job_id: a.write_start for a in result.preemption_actions}
        self.assertEqual(starts["low-1"], 10)
        self.assertEqual(starts["low-2"], 12)
        self.assertEqual(starts["low-3"], 14)

    def test_partial_overlap_then_parallel_resume(self):
        # j1 amount 3 写 [10,18)；j2 amount 1 在 10 并行；j3 amount 3 在 12
        # 到达，需等 j1 在 18 释放后启动。
        result = schedule_writes(
            [
                WriteRequest("j1", 10, 8, 3),
                WriteRequest("j2", 10, 4, 1),
                WriteRequest("j3", 12, 2, 3),
            ],
            quota=4,
        )
        by_id = {w.job_id: w for w in result}
        self.assertEqual(by_id["j2"].write_start, 10)
        self.assertEqual(by_id["j3"].write_start, 18)
        self.assertEqual(by_id["j3"].write_finish, 20)


class ReleaseBoundaryTest(unittest.TestCase):
    def test_new_job_starts_exactly_at_release_time(self):
        nodes = (Node("n1", 8, "D-A"),)
        jobs = (
            ExistingJob(
                "low",
                1,
                (JobPlacement("n1", 8),),
                (cp(7, duration=3, amount=4, lost=1),),
            ),
        )
        # 截止时刻恰好等于释放时刻 10：半开区间下应判定可行
        result = plan(
            nodes,
            jobs,
            NewJob("new", 10, SameMachineRequirement(8), 10),
            disk_write_quota=4,
        )
        self.assertTrue(result.feasible)
        self.assertEqual(result.start_time, 10)

    def test_current_time_offsets_all_semantics(self):
        nodes = (Node("n1", 8, "D-A"),)
        jobs = (
            ExistingJob(
                "low",
                1,
                (JobPlacement("n1", 8),),
                (cp(15, duration=5, amount=2, lost=1),),
            ),
        )
        result = plan(
            nodes,
            jobs,
            NewJob("new", 10, SameMachineRequirement(8), 30),
            disk_write_quota=4,
            current_time=12,
        )
        self.assertTrue(result.feasible)
        action = result.preemption_actions[0]
        self.assertEqual(action.write_start, 15)
        self.assertEqual(action.release_time, 20)
        self.assertEqual(result.start_time, 20)


class TieBreakTest(unittest.TestCase):
    def test_equal_lost_work_tie_broken_by_finish_then_job_id(self):
        # 两个同代价方案：暂停 j-a（释放晚）或 j-b（释放早，节点更优）
        nodes = (
            Node("n1", 8, "D-A"),
            Node("n2", 8, "D-A"),
        )
        jobs = (
            ExistingJob(
                "j-a",
                1,
                (JobPlacement("n1", 8),),
                (cp(10, duration=4, amount=1, lost=5),),
            ),
            ExistingJob(
                "j-b",
                1,
                (JobPlacement("n2", 8),),
                (cp(10, duration=2, amount=1, lost=5),),
            ),
        )
        new_job = NewJob("new", 10, SameMachineRequirement(8), 30)
        result = plan(nodes, jobs, new_job, disk_write_quota=4)
        self.assertTrue(result.feasible)
        # 同丢失工作量下选最晚暂停完成时刻更小的 j-b
        self.assertEqual(
            [a.job_id for a in result.preemption_actions], ["j-b"]
        )
        self.assertEqual(result.max_preemption_finish, 12)

    def test_identical_finish_tie_broken_by_job_id_tuple(self):
        # 两台满占 8 卡机，暂停任意一个都能腾出 8 卡；代价与完成时刻相同，
        # 第三级排序按暂停作业 ID 元组，应选字典序更小的 j-a。
        nodes = (
            Node("n1", 8, "D-A"),
            Node("n2", 8, "D-A"),
        )
        jobs = (
            ExistingJob(
                "j-z",
                1,
                (JobPlacement("n1", 8),),
                (cp(10, duration=2, amount=1, lost=5),),
            ),
            ExistingJob(
                "j-a",
                1,
                (JobPlacement("n2", 8),),
                (cp(10, duration=2, amount=1, lost=5),),
            ),
        )
        new_job = NewJob("new", 10, SameMachineRequirement(8), 30)
        result = plan(nodes, jobs, new_job, disk_write_quota=4)
        self.assertTrue(result.feasible)
        self.assertEqual(
            [a.job_id for a in result.preemption_actions], ["j-a"]
        )


if __name__ == "__main__":
    unittest.main()
