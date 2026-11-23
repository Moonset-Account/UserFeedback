import unittest

from planner.quota import ScheduledWrite, WriteRequest, schedule_writes


def req(job_id: str, start: int, duration: int, amount: int) -> WriteRequest:
    return WriteRequest(
        job_id=job_id, earliest_start=start, duration=duration, amount=amount
    )


class QuotaScheduleTest(unittest.TestCase):
    def test_empty_requests(self):
        self.assertEqual(schedule_writes([], 4), ())

    def test_single_request_starts_at_checkpoint(self):
        result = schedule_writes([req("j1", 5, 3, 2)], 4)
        self.assertEqual(
            result,
            (ScheduledWrite("j1", 5, 2, write_start=5, write_finish=8),),
        )

    def test_parallel_writes_fit_within_quota(self):
        result = schedule_writes(
            [req("j1", 10, 4, 3), req("j2", 10, 4, 1)], 4
        )
        by_id = {w.job_id: w for w in result}
        self.assertEqual(by_id["j1"].write_start, 10)
        self.assertEqual(by_id["j2"].write_start, 10)
        self.assertEqual(by_id["j1"].write_finish, 14)
        self.assertEqual(by_id["j2"].write_finish, 14)

    def test_same_checkpoint_serialized_when_quota_contended(self):
        # 两个 amount=3 的写盘同时到达，额度 4：第二个必须等第一个完成。
        result = schedule_writes(
            [req("j1", 10, 4, 3), req("j2", 10, 4, 3)], 4
        )
        by_id = {w.job_id: w for w in result}
        self.assertEqual(
            (by_id["j1"].write_start, by_id["j1"].write_finish), (10, 14)
        )
        self.assertEqual(
            (by_id["j2"].write_start, by_id["j2"].write_finish), (14, 18)
        )
        self.assertTrue(by_id["j2"].delayed)
        self.assertFalse(by_id["j1"].delayed)

    def test_half_open_interval_completion_frees_quota_immediately(self):
        # j1 占满额度在 [10, 14)；j3 的检查点恰为 14，半开区间下额度在
        # t=14 立即释放，j3 应在 14 整点启动（闭区间语义下会错成 15）。
        result = schedule_writes(
            [
                req("j1", 10, 4, 4),
                req("j3", 14, 2, 4),
            ],
            4,
        )
        by_id = {w.job_id: w for w in result}
        self.assertEqual(by_id["j1"].write_finish, 14)
        self.assertEqual(by_id["j3"].write_start, 14)
        self.assertEqual(by_id["j3"].write_finish, 16)
        self.assertFalse(by_id["j3"].delayed)

    def test_late_arrival_interleaving(self):
        result = schedule_writes(
            [
                req("j1", 0, 10, 3),
                req("j2", 4, 6, 2),  # t=4 到达时只剩 1 额度，需等到 t=10
            ],
            4,
        )
        by_id = {w.job_id: w for w in result}
        self.assertEqual(by_id["j2"].write_start, 10)
        self.assertEqual(by_id["j2"].write_finish, 16)

    def test_zero_duration_write(self):
        result = schedule_writes(
            [req("j1", 5, 0, 4), req("j2", 5, 2, 4)], 4
        )
        by_id = {w.job_id: w for w in result}
        self.assertEqual(by_id["j1"].write_start, 5)
        self.assertEqual(by_id["j1"].write_finish, 5)
        self.assertEqual(by_id["j2"].write_start, 5)
        self.assertEqual(by_id["j2"].write_finish, 7)

    def test_oversized_write_returns_none(self):
        self.assertIsNone(schedule_writes([req("j1", 0, 2, 5)], 4))

    def test_tie_breaking_by_job_id_is_deterministic(self):
        first = schedule_writes(
            [req("z", 10, 4, 3), req("a", 10, 4, 3)], 4
        )
        second = schedule_writes(
            [req("a", 10, 4, 3), req("z", 10, 4, 3)], 4
        )
        self.assertEqual(first, second)
        self.assertEqual(first[0].job_id, "a")


if __name__ == "__main__":
    unittest.main()
