"""Scheduler 的单元测试。

覆盖范围：
* 序列 / 可调用两种 schedule，按参数自身有效更新次数取学习率；
* 暂停不计步、计数保留、解冻后续算；
* 按参数身份调组，新组按已有计数取学习率；
* 步确认的幂等、连续性与非法输入；
* “调组后、确认前”断点恢复，以及首次解冻从第一步起算；
* 状态保存/恢复（含 JSON 落盘往返、from_state 重建）与状态校验；
* 多参数交错暂停/解冻/调组的综合场景与边界错误。
"""

from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError

from param_scheduler import Scheduler, SchedulerError, StepPlan, StepResult


def build_scheduler() -> Scheduler:
    # 序列组：越界后停在最后一个值；可调用组：按计数指数衰减。
    return Scheduler(
        {
            "backbone": [1.0, 0.5, 0.25],
            "head": lambda n: 0.9**n,
            "decay": [0.1, 0.05],
        }
    )


class ScheduleSemanticsTest(unittest.TestCase):
    def test_sequence_and_callable_follow_per_param_count(self) -> None:
        sched = build_scheduler()
        sched.add_param("w1", "backbone")
        sched.add_param("w2", "head")

        plan = sched.plan_step()
        self.assertEqual(plan.step, 1)
        self.assertEqual(plan.learning_rates, {"w1": 1.0, "w2": 1.0})
        self.assertEqual(plan.frozen, ())

        result = sched.confirm_step(1)
        self.assertEqual(result.updated, ("w1", "w2"))
        self.assertEqual(result.learning_rates, {"w1": 1.0, "w2": 1.0})
        self.assertEqual(result.counts_after, {"w1": 1, "w2": 1})
        # 确认后立即给出下一步计划，按更新后的计数取值。
        self.assertEqual(
            result.next_plan.learning_rates, {"w1": 0.5, "w2": 0.9}
        )

        result2 = sched.confirm_step(2)
        self.assertEqual(result2.learning_rates, {"w1": 0.5, "w2": 0.9})
        self.assertEqual(result2.counts_after, {"w1": 2, "w2": 2})
        self.assertEqual(
            result2.next_plan.learning_rates, {"w1": 0.25, "w2": 0.81}
        )

    def test_sequence_clamps_to_last_value_when_exhausted(self) -> None:
        sched = Scheduler({"decay": [0.1, 0.05]})
        sched.add_param("w", "decay")
        sched.confirm_step(1)
        sched.confirm_step(2)
        self.assertEqual(sched.get_count("w"), 2)
        # 计数 2、5 均超出序列长度，停在最后一个值 0.05。
        self.assertEqual(sched.current_lr("w"), 0.05)
        plan3 = sched.plan_step()
        self.assertEqual(plan3.learning_rates, {"w": 0.05})
        sched.confirm_step(3)
        sched.confirm_step(4)
        sched.confirm_step(5)
        self.assertEqual(sched.current_lr("w"), 0.05)
        self.assertEqual(sched.get_count("w"), 5)

    def test_plan_and_result_are_readonly_snapshots(self) -> None:
        sched = build_scheduler()
        sched.add_param("w1", "backbone")
        plan = sched.plan_step()
        with self.assertRaises(FrozenInstanceError):
            plan.step = 9  # type: ignore[misc]
        result = sched.confirm_step(1)
        with self.assertRaises(FrozenInstanceError):
            result.step = 9  # type: ignore[misc]
        # 后续变更不影响已缓存的快照内容。
        sched.freeze_param("w1")
        self.assertEqual(result.next_plan.learning_rates, {"w1": 0.5})
        self.assertNotIn("w1", sched.plan_step().learning_rates)


class FreezeUnfreezeTest(unittest.TestCase):
    def test_frozen_param_does_not_count_and_keeps_its_count(self) -> None:
        sched = build_scheduler()
        sched.add_param("w1", "backbone")
        sched.add_param("w2", "head")
        sched.confirm_step(1)  # 两者计数都到 1

        sched.freeze_param("w2")
        self.assertTrue(sched.is_frozen("w2"))
        plan = sched.plan_step()
        self.assertEqual(plan.learning_rates, {"w1": 0.5})
        self.assertEqual(plan.frozen, ("w2",))

        result = sched.confirm_step(2)
        self.assertEqual(result.updated, ("w1",))
        self.assertEqual(result.counts_after, {"w1": 2, "w2": 1})
        self.assertEqual(result.frozen, ("w2",))
        # 暂停参数在下一步计划里仍被标记为暂停，且学习率表中没有它。
        self.assertEqual(result.next_plan.frozen, ("w2",))
        self.assertNotIn("w2", result.next_plan.learning_rates)

    def test_unfreeze_resumes_from_retained_count(self) -> None:
        sched = build_scheduler()
        sched.add_param("w1", "backbone")
        sched.add_param("w2", "head")
        sched.confirm_step(1)
        sched.freeze_param("w2")
        sched.confirm_step(2)
        sched.confirm_step(3)  # w1 到 3，w2 仍停在 1

        sched.unfreeze_param("w2")
        self.assertFalse(sched.is_frozen("w2"))
        plan = sched.plan_step()
        # w2 保留计数 1，按 head 组 schedule(1)=0.9 取学习率；w1 走 backbone 越界值。
        self.assertEqual(plan.learning_rates, {"w1": 0.25, "w2": 0.9})
        result = sched.confirm_step(4)
        self.assertEqual(result.counts_after, {"w1": 4, "w2": 2})

    def test_first_unfreeze_of_never_updated_param_starts_at_step_one(self) -> None:
        sched = build_scheduler()
        # 注册即暂停：从未更新过。
        sched.add_param("fresh", "backbone", frozen=True)
        sched.add_param("active", "head")

        r1 = sched.confirm_step(1)
        self.assertEqual(r1.updated, ("active",))
        self.assertEqual(r1.counts_after, {"fresh": 0, "active": 1})
        self.assertEqual(r1.frozen, ("fresh",))

        # 中途训练了若干步，fresh 始终不计步。
        sched.confirm_step(2)
        self.assertEqual(sched.get_count("fresh"), 0)

        sched.unfreeze_param("fresh")
        plan = sched.plan_step()
        # 第一次解冻且从未更新：从第一步 schedule(0) 开始算。
        self.assertEqual(plan.learning_rates["fresh"], 1.0)
        result = sched.confirm_step(3)
        self.assertEqual(result.counts_after["fresh"], 1)
        self.assertEqual(result.learning_rates["fresh"], 1.0)

    def test_param_added_mid_run_with_zero_count_starts_at_step_one(self) -> None:
        sched = build_scheduler()
        sched.add_param("old", "head")
        sched.confirm_step(1)
        sched.confirm_step(2)

        sched.add_group("extra", [7.0, 6.0])
        sched.add_param("new", "extra")
        plan = sched.plan_step()
        self.assertEqual(plan.learning_rates["new"], 7.0)  # schedule(0)
        result = sched.confirm_step(3)
        self.assertEqual(result.counts_after, {"old": 3, "new": 1})

    def test_freeze_unfreeze_are_idempotent(self) -> None:
        sched = build_scheduler()
        sched.add_param("w", "backbone")
        sched.freeze_param("w")
        sched.freeze_param("w")
        self.assertTrue(sched.is_frozen("w"))
        sched.unfreeze_param("w")
        sched.unfreeze_param("w")
        self.assertFalse(sched.is_frozen("w"))


class MoveGroupTest(unittest.TestCase):
    def test_move_keeps_count_and_new_group_uses_it(self) -> None:
        sched = build_scheduler()
        sched.add_param("w", "backbone")
        sched.confirm_step(1)
        sched.confirm_step(2)
        self.assertEqual(sched.get_count("w"), 2)

        sched.move_param("w", "decay")
        self.assertEqual(sched.get_group("w"), "decay")
        self.assertEqual(sched.get_count("w"), 2)  # 计数跟随参数身份
        # 新组按已有计数 2 取值，decay 序列越界停在 0.05。
        self.assertEqual(sched.current_lr("w"), 0.05)
        plan = sched.plan_step()
        self.assertEqual(plan.learning_rates, {"w": 0.05})
        result = sched.confirm_step(3)
        self.assertEqual(result.counts_after, {"w": 3})

    def test_move_while_frozen_keeps_frozen_state(self) -> None:
        sched = build_scheduler()
        sched.add_param("w", "backbone")
        sched.confirm_step(1)
        sched.freeze_param("w")
        sched.move_param("w", "head")
        self.assertTrue(sched.is_frozen("w"))
        plan = sched.plan_step()
        self.assertEqual(plan.learning_rates, {})
        self.assertEqual(plan.frozen, ("w",))

    def test_move_to_same_group_is_noop(self) -> None:
        sched = build_scheduler()
        sched.add_param("w", "backbone")
        sched.move_param("w", "backbone")
        self.assertEqual(sched.get_group("w"), "backbone")


class StepConfirmationTest(unittest.TestCase):
    def test_repeated_confirmation_reuses_original_result(self) -> None:
        sched = build_scheduler()
        sched.add_param("w1", "backbone")
        sched.add_param("w2", "head")
        first = sched.confirm_step(1)
        again = sched.confirm_step(1)
        self.assertIs(first, again)  # 沿用原结果对象
        self.assertEqual(sched.get_count("w1"), 1)
        self.assertEqual(sched.get_count("w2"), 1)
        self.assertEqual(sched.confirmed_steps(), [1])
        self.assertTrue(sched.is_confirmed(1))
        self.assertFalse(sched.is_confirmed(2))
        self.assertEqual(sched.last_completed_step, 1)
        self.assertEqual(sched.next_step, 2)
        self.assertEqual(sched.get_result(1), first)

    def test_gap_and_replayed_steps_are_rejected(self) -> None:
        sched = build_scheduler()
        sched.add_param("w", "backbone")
        with self.assertRaises(SchedulerError):
            sched.confirm_step(2)  # 跳过第 1 步
        sched.confirm_step(1)
        with self.assertRaises(SchedulerError):
            sched.plan_step(1)  # 已确认的步不能再出计划
        with self.assertRaises(SchedulerError):
            sched.confirm_step(3)  # 不连续
        with self.assertRaises(SchedulerError):
            sched.get_result(2)  # 未确认
        for bad in (0, -1, "1", 1.0, True):
            with self.assertRaises(SchedulerError):
                sched.confirm_step(bad)  # type: ignore[arg-type]

    def test_confirm_step_with_no_params_still_advances(self) -> None:
        sched = build_scheduler()
        plan = sched.plan_step()
        self.assertEqual(plan.learning_rates, {})
        result = sched.confirm_step(1)
        self.assertEqual(result.updated, ())
        self.assertEqual(result.counts_after, {})
        self.assertEqual(sched.next_step, 2)


class CheckpointRecoveryTest(unittest.TestCase):
    def _state_with_move_before_confirm(self) -> dict:
        sched = build_scheduler()
        sched.add_param("w", "backbone")
        sched.confirm_step(1)
        sched.confirm_step(2)  # 计数 2，历史 lr 1.0 -> 0.5
        # 模拟“恰好调组、尚未确认”的断点：状态里 w 已属于新组。
        sched.move_param("w", "decay")
        return sched.state_dict()

    def test_recovery_after_move_without_confirm_uses_existing_count(self) -> None:
        state = self._state_with_move_before_confirm()
        restored = Scheduler.from_state(state, schedules={"head": lambda n: 0.9**n})
        self.assertEqual(restored.get_group("w"), "decay")
        self.assertEqual(restored.get_count("w"), 2)
        self.assertEqual(restored.next_step, 3)
        # 恢复后从它已有的计数 2 在新组取学习率（decay 越界 -> 0.05）。
        plan = restored.plan_step()
        self.assertEqual(plan.learning_rates, {"w": 0.05})
        result = restored.confirm_step(3)
        self.assertEqual(result.counts_after, {"w": 3})
        self.assertEqual(restored.get_result(1).learning_rates, {"w": 1.0})
        self.assertEqual(restored.get_result(2).learning_rates, {"w": 0.5})

    def test_recovery_with_frozen_param_pending_move(self) -> None:
        sched = build_scheduler()
        sched.add_param("w", "backbone")
        sched.confirm_step(1)
        sched.freeze_param("w")
        sched.move_param("w", "head")  # 暂停中调组，随后断点
        state = sched.state_dict()

        restored = Scheduler.from_state(state, schedules={"head": lambda n: 0.9**n})
        self.assertTrue(restored.is_frozen("w"))
        self.assertEqual(restored.get_group("w"), "head")
        plan = restored.plan_step()
        self.assertEqual(plan.frozen, ("w",))
        self.assertEqual(plan.learning_rates, {})

        restored.unfreeze_param("w")
        # 计数 1 已保留，head 组按 schedule(1) 取值。
        self.assertEqual(restored.plan_step().learning_rates, {"w": 0.9})

    def test_json_round_trip_preserves_full_state(self) -> None:
        sched = build_scheduler()
        sched.add_param("w1", "backbone")
        sched.add_param("w2", "head", frozen=True)
        sched.confirm_step(1)
        sched.confirm_step(2)
        raw = json.dumps(sched.state_dict())  # 确认可 JSON 落盘
        loaded = json.loads(raw)

        restored = Scheduler.from_state(
            loaded, schedules={"head": lambda n: 0.9**n}
        )
        self.assertEqual(restored.param_ids(), ["w1", "w2"])
        self.assertEqual(restored.get_count("w1"), 2)
        self.assertEqual(restored.get_count("w2"), 0)
        self.assertTrue(restored.is_frozen("w2"))
        self.assertEqual(restored.confirmed_steps(), [1, 2])
        cached = restored.get_result(2)
        self.assertIsInstance(cached, StepResult)
        self.assertIsInstance(cached.next_plan, StepPlan)
        self.assertEqual(cached.next_plan.step, 3)
        self.assertEqual(cached.counts_after, {"w1": 2, "w2": 0})

        # 恢复后继续训练，行为与原调度器一致。
        restored.unfreeze_param("w2")
        result = restored.confirm_step(3)
        self.assertEqual(result.learning_rates, {"w1": 0.25, "w2": 1.0})
        self.assertEqual(result.counts_after, {"w1": 3, "w2": 1})

    def test_load_state_dict_into_preconfigured_scheduler(self) -> None:
        sched = build_scheduler()
        sched.add_param("w", "backbone")
        sched.confirm_step(1)
        state = sched.state_dict()

        target = build_scheduler()  # 同结构、同序列取值
        target.load_state_dict(state)
        self.assertEqual(target.get_count("w"), 1)
        self.assertEqual(target.next_step, 2)

    def test_state_validation_rejects_bad_inputs(self) -> None:
        sched = build_scheduler()
        sched.add_param("w", "backbone")
        sched.confirm_step(1)
        state = sched.state_dict()

        with self.assertRaises(SchedulerError):
            Scheduler.from_state({**state, "version": 999})
        # 序列取值不一致：load_state_dict 会与已注册序列逐一核对；
        # from_state 对序列组以保存值为准，因此用 load 路径验证。
        wrong_seq = Scheduler(
            {
                "backbone": [9.0, 8.0, 7.0],  # 与保存点不同
                "head": lambda n: 0.9**n,
                "decay": [0.1, 0.05],
            }
        )
        with self.assertRaises(SchedulerError):
            wrong_seq.load_state_dict(state)
        # 类型不一致（序列组被换成可调用组）同样拒绝。
        wrong_kind = Scheduler(
            {
                "backbone": lambda n: 1.0 / (n + 1),
                "head": lambda n: 0.9**n,
                "decay": [0.1, 0.05],
            }
        )
        with self.assertRaises(SchedulerError):
            wrong_kind.load_state_dict(state)
        # 可调用组未重新提供 schedule。
        with self.assertRaises(SchedulerError):
            Scheduler.from_state(state)
        # 组数量不匹配。
        mismatch = Scheduler({"backbone": [1.0, 0.5, 0.25]})
        with self.assertRaises(SchedulerError):
            mismatch.load_state_dict(state)
        # 非映射输入。
        with self.assertRaises(SchedulerError):
            Scheduler.from_state(["not", "a", "mapping"])  # type: ignore[arg-type]


class InterleavedScenarioTest(unittest.TestCase):
    def test_two_params_interleaved_freeze_move_and_duplicate_confirm(self) -> None:
        sched = Scheduler(
            {
                "A": [1.0, 0.5, 0.25],
                "B": [10.0, 9.0, 8.0, 7.0],
            }
        )
        sched.add_param("p1", "A")
        sched.add_param("p2", "A")

        # 步 1：两者都从 schedule(0) 起步。
        r1 = sched.confirm_step(1)
        self.assertEqual(r1.learning_rates, {"p1": 1.0, "p2": 1.0})
        self.assertEqual(r1.counts_after, {"p1": 1, "p2": 1})

        # 暂停 p2，只有 p1 前进两步。
        sched.freeze_param("p2")
        r2 = sched.confirm_step(2)
        self.assertEqual(r2.updated, ("p1",))
        self.assertEqual(r2.counts_after, {"p1": 2, "p2": 1})
        # 重复确认沿用原结果，计数不变。
        self.assertIs(sched.confirm_step(2), r2)
        self.assertEqual(sched.get_count("p2"), 1)

        r3 = sched.confirm_step(3)
        self.assertEqual(r3.learning_rates, {"p1": 0.25})
        self.assertEqual(r3.counts_after, {"p1": 3, "p2": 1})

        # p1 调组到 B（计数 3 带过去）；p2 解冻留在 A（计数 1）。
        sched.move_param("p1", "B")
        sched.unfreeze_param("p2")
        plan4 = sched.plan_step()
        self.assertEqual(plan4.learning_rates, {"p1": 7.0, "p2": 0.5})
        r4 = sched.confirm_step(4)
        self.assertEqual(r4.counts_after, {"p1": 4, "p2": 2})

        # 用检查点状态重建后继续，验证交错结果可复现。
        state = sched.state_dict()
        restored = Scheduler.from_state(state)
        self.assertEqual(restored.plan_step().learning_rates, {"p1": 7.0, "p2": 0.25})
        r5 = restored.confirm_step(5)
        self.assertEqual(r5.counts_after, {"p1": 5, "p2": 3})
        # p2 在 A 组计数 3 已越界，停在 0.25；p1 在 B 组计数 4 越界停在 7.0。
        self.assertEqual(r5.learning_rates, {"p1": 7.0, "p2": 0.25})

    def test_crash_between_move_and_confirm_inside_long_run(self) -> None:
        sched = Scheduler({"A": [1.0, 0.5], "B": [2.0, 1.0, 0.5]})
        sched.add_param("p1", "A")
        sched.add_param("p2", "A")
        for step in range(1, 4):  # 确认 1..3，两者计数都到 3（A 越界停在 0.5）
            sched.confirm_step(step)
        sched.freeze_param("p2")
        sched.move_param("p1", "B")  # 断点：p1 已在 B，计数 3；步 4 未确认

        restored = Scheduler.from_state(sched.state_dict())
        self.assertEqual(restored.get_group("p1"), "B")
        self.assertEqual(restored.get_count("p1"), 3)
        self.assertTrue(restored.is_frozen("p2"))
        plan = restored.plan_step()
        self.assertEqual(plan.learning_rates, {"p1": 0.5})  # B 越界 -> 末值
        self.assertEqual(plan.frozen, ("p2",))
        r4 = restored.confirm_step(4)
        self.assertEqual(r4.counts_after, {"p1": 4, "p2": 3})
        # 再确认一次必须幂等。
        self.assertIs(restored.confirm_step(4), r4)
        self.assertEqual(restored.get_count("p1"), 4)


class ValidationTest(unittest.TestCase):
    def test_construction_and_registration_errors(self) -> None:
        with self.assertRaises(SchedulerError):
            Scheduler({})
        with self.assertRaises(SchedulerError):
            Scheduler({"g": []})  # 空序列
        with self.assertRaises(SchedulerError):
            Scheduler({"g": "not-a-schedule"})  # type: ignore[dict-item]
        sched = Scheduler({"g": [1.0]})
        with self.assertRaises(SchedulerError):
            sched.add_group("g", [2.0])  # 重复组
        with self.assertRaises(SchedulerError):
            sched.add_param("w", "missing")
        sched.add_param("w", "g")
        with self.assertRaises(SchedulerError):
            sched.add_param("w", "g")  # 重复参数
        with self.assertRaises(SchedulerError):
            sched.add_param("x", "g", update_count=-1)
        with self.assertRaises(SchedulerError):
            sched.add_param(["unhashable"], "g")  # type: ignore[dict-item]
        with self.assertRaises(SchedulerError):
            sched.move_param("w", "missing")
        with self.assertRaises(SchedulerError):
            sched.freeze_param("missing")
        with self.assertRaises(SchedulerError):
            sched.get_count("missing")

    def test_non_string_hashable_id_is_supported_in_memory(self) -> None:
        sched = Scheduler({"g": [1.0, 0.5]})
        sched.add_param(42, "g")
        sched.add_param(("module", 7), "g")
        result = sched.confirm_step(1)
        self.assertEqual(set(result.updated), {42, ("module", 7)})
        self.assertEqual(sched.get_count(42), 1)


if __name__ == "__main__":
    unittest.main()
