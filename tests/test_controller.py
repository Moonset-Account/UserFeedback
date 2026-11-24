"""JobController 规划逻辑测试。

覆盖题面要求的全部场景：

* 普通每日调度与重启补看窗口（含边界闭区间）；
* 秋令时回拨：同一挂钟时刻出现两次，每个实际时刻各一个候选；
* 春令时前拨：计划时刻被跳过，无候选、只产生 GapNotice 并落终态；
* 禁并发：运行中的另一实例按时刻记录跳过；
* 发出创建请求后立即重启：同标识幂等重发（RecreateDecision）；
* 最近确认点裁剪、终态不重放、允许并发时不跳过；
* 多日交错状态。
"""

import unittest
from datetime import timedelta

from job_controller import (
    CreateDecision,
    GapNotice,
    InMemoryRunStateStore,
    JobController,
    RecreateDecision,
    RunRecord,
    RunStatus,
    Schedule,
    SkipDecision,
    creation_id_for,
)

from synthetic import (
    LOCAL_01_30,
    SYNTHETIC_ZONE,
    local,
    utc,
)


def make_schedule(**overrides) -> Schedule:
    defaults = dict(
        schedule_id="job-daily",
        local_time=LOCAL_01_30,
        allow_concurrent=False,
        lookback=timedelta(days=3),
    )
    defaults.update(overrides)
    return Schedule(**defaults)


def make_controller():
    store = InMemoryRunStateStore()
    return store, JobController(store, SYNTHETIC_ZONE)


class NormalSchedulingTest(unittest.TestCase):
    def test_single_candidate_on_normal_day_creates_run(self):
        store, controller = make_controller()
        # lookback 设为 18 小时：只覆盖当日一次 01:30
        schedule = make_schedule(lookback=timedelta(hours=18))

        result = controller.plan(
            schedule,
            now_utc=utc(2026, 6, 2, 6, 0),
            last_confirmed_utc=utc(2026, 5, 30, 0, 0),
        )

        self.assertEqual(len(result.decisions), 1)
        decision = result.decisions[0]
        self.assertIsInstance(decision, CreateDecision)
        self.assertEqual(decision.scheduled_utc, utc(2026, 6, 2, 5, 30))
        self.assertEqual(
            decision.creation_id,
            creation_id_for(schedule.schedule_id, utc(2026, 6, 2, 5, 30)),
        )
        # 决定同步落盘
        record = store.get(schedule.schedule_id, utc(2026, 6, 2, 5, 30))
        self.assertEqual(record.status, RunStatus.CREATE_REQUESTED)
        self.assertEqual(record.creation_id, decision.creation_id)
        self.assertEqual(result.gap_notices, ())

    def test_window_is_closed_interval_at_both_ends(self):
        """恰好落在窗口两端的时刻都要纳入。"""
        _store, controller = make_controller()
        schedule = make_schedule(lookback=timedelta(days=1))

        now = utc(2026, 6, 2, 5, 30)  # 当日 01:30 刚到
        result = controller.plan(
            schedule,
            now_utc=now,
            last_confirmed_utc=utc(2026, 5, 30, 0, 0),
        )
        window_start = utc(2026, 6, 1, 5, 30)
        self.assertEqual(result.window_start, window_start)
        scheduled = [d.scheduled_utc for d in result.decisions]
        self.assertEqual(
            scheduled,
            [utc(2026, 6, 1, 5, 30), utc(2026, 6, 2, 5, 30)],
        )

    def test_lookback_limits_history_replay(self):
        """超出 lookback 的历史时刻不补看。"""
        _store, controller = make_controller()
        # 18 小时窗口：6/5 12:00 回推到 6/4 18:00，6/4 05:30 被排除
        schedule = make_schedule(lookback=timedelta(hours=18))

        result = controller.plan(
            schedule,
            now_utc=utc(2026, 6, 5, 12, 0),
            last_confirmed_utc=utc(2026, 1, 1, 0, 0),
        )
        scheduled = [d.scheduled_utc for d in result.decisions]
        self.assertEqual(
            scheduled,
            [utc(2026, 6, 5, 5, 30)],
        )


class RestartAndIdempotencyTest(unittest.TestCase):
    def test_restart_replays_same_creation_id(self):
        store = InMemoryRunStateStore()
        schedule = make_schedule(lookback=timedelta(hours=18))
        instant = utc(2026, 6, 2, 5, 30)

        # 第一次规划：发出创建请求
        first = JobController(store, SYNTHETIC_ZONE).plan(
            schedule,
            now_utc=utc(2026, 6, 2, 6, 0),
            last_confirmed_utc=utc(2026, 5, 30, 0, 0),
        )
        self.assertIsInstance(first.decisions[0], CreateDecision)
        original_id = first.decisions[0].creation_id

        # 模拟“发出创建请求后立即重启”：新建控制器实例、存储保留
        second = JobController(store, SYNTHETIC_ZONE).plan(
            schedule,
            now_utc=utc(2026, 6, 2, 6, 5),
            last_confirmed_utc=utc(2026, 5, 30, 0, 0),
        )
        self.assertEqual(len(second.decisions), 1)
        replayed = second.decisions[0]
        self.assertIsInstance(replayed, RecreateDecision)
        self.assertEqual(replayed.creation_id, original_id)
        self.assertEqual(replayed.scheduled_utc, instant)
        # 存储仍只有一条记录，没有二次创建
        self.assertEqual(len(store.list_for_schedule(schedule.schedule_id)), 1)

    def test_creation_id_is_stable_across_processes(self):
        """uuid5 派生对相同输入必然相同（跨进程也成立）。"""
        instant = utc(2026, 6, 2, 5, 30)
        self.assertEqual(
            creation_id_for("job", instant),
            creation_id_for("job", instant),
        )
        self.assertNotEqual(
            creation_id_for("job-a", instant),
            creation_id_for("job-b", instant),
        )
        self.assertNotEqual(
            creation_id_for("job", instant),
            creation_id_for("job", utc(2026, 6, 2, 5, 31)),
        )

    def test_running_instance_blocks_new_nonconcurrent_run(self):
        store, controller = make_controller()
        schedule = make_schedule()
        older = utc(2026, 6, 1, 5, 30)
        newer = utc(2026, 6, 2, 5, 30)
        store.put(
            RunRecord(
                schedule_id=schedule.schedule_id,
                scheduled_utc=older,
                status=RunStatus.RUNNING,
                creation_id=creation_id_for(schedule.schedule_id, older),
            )
        )

        result = controller.plan(
            schedule,
            now_utc=utc(2026, 6, 2, 6, 0),
            last_confirmed_utc=utc(2026, 5, 30, 0, 0),
        )
        by_instant = {d.scheduled_utc: d for d in result.decisions}

        # 旧时刻：仍在运行 -> skip(reason=running)
        old_decision = by_instant[older]
        self.assertIsInstance(old_decision, SkipDecision)
        self.assertEqual(old_decision.reason, "running")

        # 新时刻：被运行中的实例阻塞 -> 按时刻记录并发跳过
        new_decision = by_instant[newer]
        self.assertIsInstance(new_decision, SkipDecision)
        self.assertEqual(new_decision.reason, "concurrency")
        self.assertEqual(new_decision.blocking_scheduled_utc, older)
        stored = store.get(schedule.schedule_id, newer)
        self.assertEqual(stored.status, RunStatus.SKIPPED_CONCURRENCY)

    def test_concurrent_schedule_is_not_blocked(self):
        store, controller = make_controller()
        schedule = make_schedule(allow_concurrent=True)
        older = utc(2026, 6, 1, 5, 30)
        newer = utc(2026, 6, 2, 5, 30)
        store.put(
            RunRecord(
                schedule_id=schedule.schedule_id,
                scheduled_utc=older,
                status=RunStatus.RUNNING,
                creation_id=creation_id_for(schedule.schedule_id, older),
            )
        )

        result = controller.plan(
            schedule,
            now_utc=utc(2026, 6, 2, 6, 0),
            last_confirmed_utc=utc(2026, 5, 30, 0, 0),
        )
        new_decision = next(d for d in result.decisions if d.scheduled_utc == newer)
        self.assertIsInstance(new_decision, CreateDecision)
        self.assertEqual(
            store.get(schedule.schedule_id, newer).status,
            RunStatus.CREATE_REQUESTED,
        )

    def test_create_requested_also_holds_concurrency_slot(self):
        """请求已发但未确认（CREATE_REQUESTED）同样占用并发名额。"""
        store, controller = make_controller()
        schedule = make_schedule()
        older = utc(2026, 6, 1, 5, 30)
        newer = utc(2026, 6, 2, 5, 30)
        store.put(
            RunRecord(
                schedule_id=schedule.schedule_id,
                scheduled_utc=older,
                status=RunStatus.CREATE_REQUESTED,
                creation_id=creation_id_for(schedule.schedule_id, older),
            )
        )

        result = controller.plan(
            schedule,
            now_utc=utc(2026, 6, 2, 6, 0),
            last_confirmed_utc=utc(2026, 5, 30, 0, 0),
        )
        by_instant = {d.scheduled_utc: d for d in result.decisions}
        self.assertIsInstance(by_instant[older], RecreateDecision)
        self.assertEqual(by_instant[newer].reason, "concurrency")


class DaylightSavingTest(unittest.TestCase):
    def test_fallback_emits_one_candidate_per_actual_occurrence(self):
        store, controller = make_controller()
        # 18 小时窗口只覆盖 11-01 当天，聚焦回拨歧义本身
        schedule = make_schedule(lookback=timedelta(hours=18))

        result = controller.plan(
            schedule,
            now_utc=utc(2026, 11, 1, 12, 0),
            last_confirmed_utc=utc(2026, 10, 29, 0, 0),
        )
        scheduled = [d.scheduled_utc for d in result.decisions]
        # 当天 01:30 实际出现两次，各自成为独立候选
        self.assertEqual(
            scheduled,
            [utc(2026, 11, 1, 5, 30), utc(2026, 11, 1, 6, 30)],
        )
        self.assertTrue(
            all(isinstance(d, CreateDecision) for d in result.decisions)
        )
        # 两个歧义时刻必须拿到不同的创建标识
        ambiguous_ids = {d.creation_id for d in result.decisions}
        self.assertEqual(len(ambiguous_ids), 2)
        self.assertEqual(result.gap_notices, ())

    def test_fallback_same_wall_time_occurrences_do_not_block_each_other(self):
        """回拨的两遍是同一挂钟时刻的两次实际出现，互不占用并发名额。"""
        store, controller = make_controller()
        schedule = make_schedule()
        first = utc(2026, 11, 1, 5, 30)
        second = utc(2026, 11, 1, 6, 30)
        store.put(
            RunRecord(
                schedule_id=schedule.schedule_id,
                scheduled_utc=first,
                status=RunStatus.RUNNING,
                creation_id=creation_id_for(schedule.schedule_id, first),
            )
        )

        result = controller.plan(
            schedule,
            now_utc=utc(2026, 11, 1, 12, 0),
            last_confirmed_utc=utc(2026, 10, 31, 12, 0),
        )
        by_instant = {d.scheduled_utc: d for d in result.decisions}
        # 第一遍：仍在运行，只回显 running 跳过
        self.assertEqual(by_instant[first].reason, "running")
        # 第二遍：同一挂钟时刻，不被第一遍阻塞，正常创建
        self.assertIsInstance(by_instant[second], CreateDecision)
        self.assertIsNotNone(by_instant[second].creation_id)
        self.assertEqual(
            store.get(schedule.schedule_id, second).status,
            RunStatus.CREATE_REQUESTED,
        )

    def test_fallback_blocked_by_running_instance_from_other_wall_time(self):
        """前一天（不同挂钟时刻）仍在运行时，回拨当天两遍都按时刻跳过。"""
        store, controller = make_controller()
        schedule = make_schedule(lookback=timedelta(days=2))
        previous_day = utc(2026, 10, 31, 5, 30)
        first = utc(2026, 11, 1, 5, 30)
        second = utc(2026, 11, 1, 6, 30)
        store.put(
            RunRecord(
                schedule_id=schedule.schedule_id,
                scheduled_utc=previous_day,
                status=RunStatus.RUNNING,
                creation_id=creation_id_for(schedule.schedule_id, previous_day),
            )
        )

        result = controller.plan(
            schedule,
            now_utc=utc(2026, 11, 1, 12, 0),
            last_confirmed_utc=utc(2026, 10, 31, 0, 0),
        )
        by_instant = {d.scheduled_utc: d for d in result.decisions}
        self.assertEqual(by_instant[previous_day].reason, "running")
        for blocked in (first, second):
            decision = by_instant[blocked]
            self.assertEqual(decision.reason, "concurrency")
            self.assertEqual(
                decision.blocking_scheduled_utc, previous_day
            )
            self.assertEqual(
                store.get(schedule.schedule_id, blocked).status,
                RunStatus.SKIPPED_CONCURRENCY,
            )

    def test_spring_forward_gap_has_no_candidate(self):
        store, controller = make_controller()
        schedule = make_schedule()

        result = controller.plan(
            schedule,
            now_utc=utc(2026, 3, 9, 12, 0),
            last_confirmed_utc=utc(2026, 3, 7, 0, 0),
        )
        # 3-7、3-9 各一个候选；3-8 的 01:30 被前拨跳过
        scheduled = [d.scheduled_utc for d in result.decisions]
        self.assertEqual(
            scheduled,
            [
                utc(2026, 3, 7, 6, 30),
                utc(2026, 3, 9, 5, 30),
            ],
        )
        self.assertEqual(len(result.gap_notices), 1)
        gap = result.gap_notices[0]
        self.assertIsInstance(gap, GapNotice)
        self.assertEqual(gap.local_wall_time, local(2026, 3, 8, 1, 30))
        # 不存在的时刻没有实际实例，因此不产生任何运行记录
        gap_record = store.get(schedule.schedule_id, utc(2026, 3, 8, 6, 30))
        self.assertIsNone(gap_record)

    def test_gap_notice_is_stable_across_replanning(self):
        """重算多次：GapNotice 一致，且始终不产生实例。"""
        store, controller = make_controller()
        schedule = make_schedule()
        kwargs = dict(
            now_utc=utc(2026, 3, 9, 12, 0),
            last_confirmed_utc=utc(2026, 3, 7, 0, 0),
        )
        first = controller.plan(schedule, **kwargs)
        second = controller.plan(schedule, **kwargs)
        self.assertEqual(first.gap_notices, second.gap_notices)
        # 第二次规划不会产生重复创建
        self.assertEqual(
            len(store.list_for_schedule(schedule.schedule_id)),
            len(first.decisions),
        )


class CheckpointAndTerminalTest(unittest.TestCase):
    def test_last_confirmed_checkpoint_clips_history(self):
        _store, controller = make_controller()
        schedule = make_schedule(lookback=timedelta(days=10))

        result = controller.plan(
            schedule,
            now_utc=utc(2026, 6, 5, 12, 0),
            last_confirmed_utc=utc(2026, 6, 4, 0, 0),
        )
        scheduled = [d.scheduled_utc for d in result.decisions]
        # 窗口起点取 max(确认点, now-lookback)，6-4 之前的都不重放
        self.assertEqual(
            scheduled,
            [utc(2026, 6, 4, 5, 30), utc(2026, 6, 5, 5, 30)],
        )
        self.assertEqual(result.window_start, utc(2026, 6, 4, 0, 0))

    def test_terminal_statuses_are_never_recreated(self):
        store, controller = make_controller()
        schedule = make_schedule(lookback=timedelta(days=5))
        succeeded_at = utc(2026, 6, 1, 5, 30)
        failed_at = utc(2026, 6, 2, 5, 30)
        skipped_at = utc(2026, 6, 3, 5, 30)
        store.put(
            RunRecord(
                schedule_id=schedule.schedule_id,
                scheduled_utc=succeeded_at,
                status=RunStatus.SUCCEEDED,
            )
        )
        store.put(
            RunRecord(
                schedule_id=schedule.schedule_id,
                scheduled_utc=failed_at,
                status=RunStatus.FAILED,
            )
        )
        store.put(
            RunRecord(
                schedule_id=schedule.schedule_id,
                scheduled_utc=skipped_at,
                status=RunStatus.SKIPPED_CONCURRENCY,
            )
        )

        result = controller.plan(
            schedule,
            now_utc=utc(2026, 6, 4, 6, 0),
            last_confirmed_utc=utc(2026, 6, 1, 0, 0),
        )
        reasons = {d.scheduled_utc: d.reason for d in result.skips}
        self.assertEqual(reasons[succeeded_at], "terminal:succeeded")
        self.assertEqual(reasons[failed_at], "terminal:failed")
        self.assertEqual(
            reasons[skipped_at], "terminal:skipped_concurrency"
        )
        # 只有 6-4 是新建
        self.assertEqual(len(result.creates), 1)
        self.assertEqual(
            result.creates[0].scheduled_utc, utc(2026, 6, 4, 5, 30)
        )

    def test_interleaved_statuses_across_multiple_days(self):
        store, controller = make_controller()
        # 4 天窗口：起点 5/31 06:00，恰好覆盖 6/1 的在途阻塞实例
        schedule = make_schedule(lookback=timedelta(days=4))
        running_at = utc(2026, 6, 1, 5, 30)
        store.put(
            RunRecord(
                schedule_id=schedule.schedule_id,
                scheduled_utc=running_at,
                status=RunStatus.RUNNING,
                creation_id=creation_id_for(schedule.schedule_id, running_at),
            )
        )

        result = controller.plan(
            schedule,
            now_utc=utc(2026, 6, 4, 6, 0),
            last_confirmed_utc=utc(2026, 5, 30, 0, 0),
        )
        kinds = [
            (d.scheduled_utc, type(d).__name__, getattr(d, "reason", None))
            for d in result.decisions
        ]
        self.assertEqual(
            kinds,
            [
                (utc(2026, 6, 1, 5, 30), "SkipDecision", "running"),
                (utc(2026, 6, 2, 5, 30), "SkipDecision", "concurrency"),
                (utc(2026, 6, 3, 5, 30), "SkipDecision", "concurrency"),
                (utc(2026, 6, 4, 5, 30), "SkipDecision", "concurrency"),
            ],
        )

        # 运行端随后确认 6-1 成功：重算后后续时刻的跳过是终态，
        # 不会因为阻塞消失而追溯补建
        store.put(
            RunRecord(
                schedule_id=schedule.schedule_id,
                scheduled_utc=running_at,
                status=RunStatus.SUCCEEDED,
            )
        )
        again = controller.plan(
            schedule,
            now_utc=utc(2026, 6, 4, 6, 30),
            last_confirmed_utc=utc(2026, 5, 30, 0, 0),
        )
        self.assertEqual(len(again.creates), 0)
        self.assertEqual(len(again.recreates), 0)
        terminal_skips = {
            d.scheduled_utc: d.reason for d in again.skips
        }
        self.assertEqual(
            terminal_skips[utc(2026, 6, 2, 5, 30)],
            "terminal:skipped_concurrency",
        )


class ValidationTest(unittest.TestCase):
    def test_invalid_schedule_arguments(self):
        with self.assertRaises(ValueError):
            Schedule(schedule_id="", local_time=LOCAL_01_30)
        with self.assertRaises(ValueError):
            Schedule(
                schedule_id="x",
                local_time=LOCAL_01_30,
                lookback=timedelta(0),
            )
        with self.assertRaises(ValueError):
            Schedule(
                schedule_id="x",
                local_time=LOCAL_01_30,
                lookback=timedelta(hours=-1),
            )

    def test_plan_rejects_naive_datetimes(self):
        _store, controller = make_controller()
        schedule = make_schedule()
        with self.assertRaises(ValueError):
            controller.plan(
                schedule,
                now_utc=local(2026, 6, 2, 6, 0),
                last_confirmed_utc=utc(2026, 5, 30, 0, 0),
            )
        with self.assertRaises(ValueError):
            controller.plan(
                schedule,
                now_utc=utc(2026, 6, 2, 6, 0),
                last_confirmed_utc=local(2026, 5, 30, 0, 0),
            )

    def test_checkpoint_after_now_rejected(self):
        _store, controller = make_controller()
        with self.assertRaises(ValueError):
            controller.plan(
                make_schedule(),
                now_utc=utc(2026, 6, 2, 6, 0),
                last_confirmed_utc=utc(2026, 6, 3, 0, 0),
            )

    def test_run_record_requires_creation_id_in_flight(self):
        with self.assertRaises(ValueError):
            RunRecord(
                schedule_id="x",
                scheduled_utc=utc(2026, 6, 1, 5, 30),
                status=RunStatus.RUNNING,
            )

    def test_run_record_rejects_naive_instant(self):
        with self.assertRaises(ValueError):
            RunRecord(
                schedule_id="x",
                scheduled_utc=local(2026, 6, 1, 5, 30),
                status=RunStatus.SUCCEEDED,
            )


if __name__ == "__main__":
    unittest.main()
