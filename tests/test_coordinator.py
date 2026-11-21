"""本地合成数据测试：不联网、不读写外部文件、不依赖第三方库。

运行方式（本次交付未执行，见 README 的“测试状态”一节，在仓库根目录）::

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import math
import unittest

from stepcoord import (
    CoordinatorError,
    FutureStepError,
    GradientReport,
    IncompleteStepError,
    LearningRateScheduleError,
    MicroBatch,
    RankMismatchError,
    StepCoordinator,
    StepStatus,
    SubmitResult,
    UnknownMicroBatchError,
)


def make_coordinator(parameters=(0.0, 0.0), lr_schedule=(1.0,)) -> StepCoordinator:
    """两 rank、各一个微批的合成场景：rank 0 -> a，rank 1 -> b。"""
    return StepCoordinator(
        parameters=parameters,
        roster=(MicroBatch("a", 0), MicroBatch("b", 1)),
        lr_schedule=lr_schedule,
    )


class CoordinatorTestCase(unittest.TestCase):
    """带向量比较辅助方法的基类，避免依赖浮点的精确二进制值。"""

    def assertVectorAlmostEqual(self, actual, expected, places: int = 12) -> None:
        actual_values = tuple(actual)
        expected_values = tuple(expected)
        self.assertEqual(len(actual_values), len(expected_values))
        for index, (got, want) in enumerate(zip(actual_values, expected_values)):
            self.assertAlmostEqual(got, want, places=places, msg=f"component {index}")


class TokenWeightingTest(CoordinatorTestCase):
    def test_one_and_nine_tokens_recover_weights_from_parameters(self) -> None:
        coordinator = make_coordinator(parameters=(0.0, 0.0), lr_schedule=(2.0,))

        self.assertIs(
            coordinator.submit(
                GradientReport(
                    step=0, mb_id="a", rank=0, valid_tokens=1, gradient=(1.0, 0.0)
                )
            ),
            SubmitResult.ACCEPTED,
        )
        self.assertIs(
            coordinator.submit(
                GradientReport(
                    step=0, mb_id="b", rank=1, valid_tokens=9, gradient=(0.0, 1.0)
                )
            ),
            SubmitResult.ACCEPTED,
        )

        result = coordinator.settle()

        self.assertIs(result.status, StepStatus.APPLIED)
        self.assertEqual(result.step, 0)
        self.assertEqual(result.total_valid_tokens, 10)
        self.assertEqual(result.lr_position, 0)
        self.assertEqual(result.learning_rate, 2.0)

        # theta -= lr * (0.1 * g_a + 0.9 * g_b)
        self.assertVectorAlmostEqual(result.parameters, (-0.2, -1.8))

        # 从更新后的参数直接反解权重：|Δθ_i| / (lr · |g_i|)。
        # g_a 只落在第 0 维、g_b 只落在第 1 维，两维互不干扰；
        # 本例 lr = 2.0、各维梯度幅值均为 1。
        weight_of_a = abs(result.parameters[0] - 0.0) / (2.0 * 1.0)
        weight_of_b = abs(result.parameters[1] - 0.0) / (2.0 * 1.0)
        self.assertAlmostEqual(weight_of_a, 0.1)
        self.assertAlmostEqual(weight_of_b, 0.9)
        self.assertAlmostEqual(weight_of_a + weight_of_b, 1.0)

    def test_contributions_report_weights_and_ranks(self) -> None:
        coordinator = make_coordinator()
        coordinator.submit(
            GradientReport(step=0, mb_id="a", rank=0, valid_tokens=1, gradient=(1.0, 0.0))
        )
        coordinator.submit(
            GradientReport(step=0, mb_id="b", rank=1, valid_tokens=9, gradient=(0.0, 1.0))
        )

        result = coordinator.settle()

        self.assertEqual(
            [(c.mb_id, c.rank, c.valid_tokens) for c in result.contributions],
            [("a", 0, 1), ("b", 1, 9)],
        )
        self.assertAlmostEqual(result.contributions[0].weight, 0.1)
        self.assertAlmostEqual(result.contributions[1].weight, 0.9)

    def test_normalized_gradient_is_token_weighted_sum(self) -> None:
        coordinator = make_coordinator(parameters=(0.0, 0.0), lr_schedule=(1.0,))
        coordinator.submit(
            GradientReport(step=0, mb_id="a", rank=0, valid_tokens=1, gradient=(1.0, 1.0))
        )
        coordinator.submit(
            GradientReport(step=0, mb_id="b", rank=1, valid_tokens=9, gradient=(1.0, 3.0))
        )

        result = coordinator.settle()

        # 0.1 * (1, 1) + 0.9 * (1, 3) = (1.0, 2.8)
        self.assertVectorAlmostEqual(result.parameters, (-1.0, -2.8))
        self.assertAlmostEqual(sum(c.weight for c in result.contributions), 1.0)

    def test_weighting_uses_tokens_not_rank_count(self) -> None:
        coordinator = StepCoordinator(
            parameters=(1.0, 2.0),
            roster=(
                MicroBatch("a", 0),
                MicroBatch("b", 0),
                MicroBatch("c", 1),
            ),
            lr_schedule=(1.0,),
        )
        for mb_id, tokens, gradient in (
            ("a", 3, (1.0, 1.0)),
            ("b", 1, (1.0, 1.0)),
            ("c", 0, None),
        ):
            coordinator.submit(
                GradientReport(
                    step=0,
                    mb_id=mb_id,
                    rank=0 if mb_id != "c" else 1,
                    valid_tokens=tokens,
                    gradient=gradient,
                )
            )

        result = coordinator.settle()

        self.assertIs(result.status, StepStatus.APPLIED)
        self.assertEqual(result.total_valid_tokens, 4)
        self.assertVectorAlmostEqual(result.parameters, (0.0, 1.0))
        self.assertEqual(
            [contribution.weight for contribution in result.contributions],
            [0.75, 0.25, 0.0],
        )


class SkipAndLrPositionTest(CoordinatorTestCase):
    def test_zero_token_step_is_skipped_and_lr_position_stays(self) -> None:
        coordinator = make_coordinator(lr_schedule=(0.5, 0.25))
        coordinator.submit(
            GradientReport(step=0, mb_id="a", rank=0, valid_tokens=0, gradient=None)
        )
        coordinator.submit(
            GradientReport(
                step=0, mb_id="b", rank=1, valid_tokens=0, gradient=(5.0, 5.0)
            )
        )

        result = coordinator.settle()

        self.assertIs(result.status, StepStatus.SKIPPED_NO_TOKENS)
        self.assertEqual(result.total_valid_tokens, 0)
        self.assertEqual(result.parameters, (0.0, 0.0))
        self.assertIsNone(result.learning_rate)
        self.assertEqual(result.lr_position, 0)
        self.assertEqual(coordinator.lr_position, 0)
        self.assertEqual(
            [contribution.weight for contribution in result.contributions],
            [None, None],
        )

        # 学习率位置没有前进：下一个成功步仍读取 lr_schedule[0]。
        coordinator.submit(
            GradientReport(step=1, mb_id="a", rank=0, valid_tokens=1, gradient=(1.0, 0.0))
        )
        coordinator.submit(
            GradientReport(step=1, mb_id="b", rank=1, valid_tokens=1, gradient=(0.0, 1.0))
        )
        applied = coordinator.settle()

        self.assertIs(applied.status, StepStatus.APPLIED)
        self.assertEqual(applied.lr_position, 0)
        self.assertEqual(applied.learning_rate, 0.5)
        self.assertVectorAlmostEqual(applied.parameters, (-0.25, -0.25))

    def test_overflow_skips_step_once_roster_is_complete(self) -> None:
        coordinator = StepCoordinator(
            parameters=(1.0,),
            roster=(
                MicroBatch("a", 0),
                MicroBatch("b", 0),
                MicroBatch("c", 1),
            ),
            lr_schedule=(1.0,),
        )
        coordinator.submit(
            GradientReport(step=0, mb_id="a", rank=0, valid_tokens=4, overflow=True)
        )
        coordinator.submit(
            GradientReport(step=0, mb_id="b", rank=0, valid_tokens=4, gradient=(1.0,))
        )
        self.assertEqual(coordinator.pending_micro_batches, ("c",))

        # 依据预先给定的名单结算：仍应收齐 c 后再统一判定 overflow。
        with self.assertRaises(IncompleteStepError) as raised:
            coordinator.settle()
        self.assertEqual(raised.exception.missing, ("c",))
        self.assertEqual(coordinator.current_step, 0)
        self.assertEqual(coordinator.parameters, (1.0,))

        coordinator.submit(
            GradientReport(step=0, mb_id="c", rank=1, valid_tokens=7, gradient=(1.0,))
        )
        result = coordinator.settle()

        self.assertIs(result.status, StepStatus.SKIPPED_OVERFLOW)
        self.assertEqual(result.total_valid_tokens, 15)
        self.assertEqual(result.parameters, (1.0,))
        self.assertIsNone(result.learning_rate)
        self.assertEqual(coordinator.lr_position, 0)
        self.assertEqual(coordinator.current_step, 1)

    def test_overflow_on_zero_token_micro_batch_still_skips(self) -> None:
        coordinator = make_coordinator(lr_schedule=(1.0,))
        coordinator.submit(
            GradientReport(step=0, mb_id="a", rank=0, valid_tokens=0, overflow=True)
        )
        coordinator.submit(
            GradientReport(step=0, mb_id="b", rank=1, valid_tokens=8, gradient=(1.0, 1.0))
        )

        result = coordinator.settle()

        self.assertIs(result.status, StepStatus.SKIPPED_OVERFLOW)
        self.assertEqual(result.parameters, (0.0, 0.0))
        self.assertEqual(coordinator.lr_position, 0)

    def test_incomplete_step_raises_and_changes_nothing(self) -> None:
        coordinator = make_coordinator(lr_schedule=(1.0,))
        coordinator.submit(
            GradientReport(step=0, mb_id="a", rank=0, valid_tokens=1, gradient=(1.0, 0.0))
        )

        with self.assertRaises(IncompleteStepError) as raised:
            coordinator.settle()

        self.assertEqual(raised.exception.step, 0)
        self.assertEqual(raised.exception.missing, ("b",))
        self.assertEqual(coordinator.parameters, (0.0, 0.0))
        self.assertEqual(coordinator.current_step, 0)
        self.assertEqual(coordinator.pending_micro_batches, ("b",))

        coordinator.submit(
            GradientReport(step=0, mb_id="b", rank=1, valid_tokens=1, gradient=(0.0, 1.0))
        )
        self.assertIs(coordinator.settle().status, StepStatus.APPLIED)

    def test_lr_schedule_exhaustion_keeps_step_pending(self) -> None:
        coordinator = make_coordinator(lr_schedule=(0.5,))
        for mb_id, rank, gradient in (("a", 0, (1.0, 0.0)), ("b", 1, (0.0, 1.0))):
            coordinator.submit(
                GradientReport(
                    step=0, mb_id=mb_id, rank=rank, valid_tokens=1, gradient=gradient
                )
            )
        coordinator.settle()

        for mb_id, rank, gradient in (("a", 0, (1.0, 0.0)), ("b", 1, (0.0, 1.0))):
            coordinator.submit(
                GradientReport(
                    step=1, mb_id=mb_id, rank=rank, valid_tokens=1, gradient=gradient
                )
            )

        with self.assertRaises(LearningRateScheduleError):
            coordinator.settle()

        self.assertEqual(coordinator.current_step, 1)
        self.assertEqual(coordinator.lr_position, 1)
        self.assertVectorAlmostEqual(coordinator.parameters, (-0.25, -0.25))
        # 该步仍未结算：上报没有被清空，补齐学习率后可直接重试。
        self.assertEqual(coordinator.pending_micro_batches, ())
        self.assertEqual(len(coordinator.history), 1)

    def test_callable_schedule_receives_position(self) -> None:
        positions: list[int] = []

        def schedule(position: int) -> float:
            positions.append(position)
            return 0.5 if position == 0 else 111.0

        coordinator = StepCoordinator(
            parameters=(0.0,),
            roster=(MicroBatch("a", 0),),
            lr_schedule=schedule,
        )
        for step in range(2):
            coordinator.submit(
                GradientReport(
                    step=step, mb_id="a", rank=0, valid_tokens=1, gradient=(1.0,)
                )
            )
            result = coordinator.settle()
            self.assertIs(result.status, StepStatus.APPLIED)

        self.assertEqual(positions, [0, 1])
        # 单微批权重为 1：先按位置 0 的 0.5 更新，再按位置 1 的 111.0 更新。
        self.assertVectorAlmostEqual(coordinator.parameters, (-111.5,))

    def test_schedule_callback_may_read_coordinator_state(self) -> None:
        # 回调在结算过程中被调用；读取公开属性不应死锁。
        seen: list[tuple[int, int]] = []
        holder: dict[str, StepCoordinator] = {}

        def schedule(position: int) -> float:
            seen.append((position, holder["coord"].current_step))
            return 1.0

        coordinator = StepCoordinator(
            parameters=(0.0,),
            roster=(MicroBatch("a", 0),),
            lr_schedule=schedule,
        )
        holder["coord"] = coordinator
        coordinator.submit(
            GradientReport(step=0, mb_id="a", rank=0, valid_tokens=1, gradient=(1.0,))
        )
        self.assertIs(coordinator.settle().status, StepStatus.APPLIED)
        self.assertEqual(seen, [(0, 0)])
        self.assertEqual(coordinator.current_step, 1)


class ReportIdentityTest(CoordinatorTestCase):
    def test_identical_retransmission_counts_once(self) -> None:
        coordinator = make_coordinator(lr_schedule=(1.0,))
        report = GradientReport(
            step=0, mb_id="a", rank=0, valid_tokens=1, gradient=(1.0, 0.0)
        )

        self.assertIs(coordinator.submit(report), SubmitResult.ACCEPTED)
        self.assertIs(coordinator.submit(report), SubmitResult.DUPLICATE)
        self.assertIs(
            coordinator.submit(
                GradientReport(
                    step=0, mb_id="b", rank=1, valid_tokens=9, gradient=(0.0, 1.0)
                )
            ),
            SubmitResult.ACCEPTED,
        )

        result = coordinator.settle()

        # 若重复计数，token 总数会是 11、权重会是 1/11 与 9/11。
        self.assertEqual(result.total_valid_tokens, 10)
        self.assertVectorAlmostEqual(result.parameters, (-0.1, -0.9))

    def test_zero_token_payload_ignores_gradient_for_identity(self) -> None:
        coordinator = make_coordinator()
        first = GradientReport(
            step=0, mb_id="a", rank=0, valid_tokens=0, gradient=(1.0, 2.0)
        )
        second = GradientReport(
            step=0, mb_id="a", rank=0, valid_tokens=0, gradient=(9.0, 9.0)
        )

        self.assertIs(coordinator.submit(first), SubmitResult.ACCEPTED)
        self.assertIs(coordinator.submit(second), SubmitResult.DUPLICATE)

    def test_conflicting_retransmission_fails_the_step(self) -> None:
        coordinator = make_coordinator(lr_schedule=(1.0, 0.5))
        coordinator.submit(
            GradientReport(step=0, mb_id="a", rank=0, valid_tokens=1, gradient=(1.0, 0.0))
        )
        self.assertIs(
            coordinator.submit(
                GradientReport(
                    step=0, mb_id="a", rank=0, valid_tokens=2, gradient=(1.0, 0.0)
                )
            ),
            SubmitResult.CONFLICT,
        )
        self.assertIs(
            coordinator.submit(
                GradientReport(
                    step=0, mb_id="b", rank=1, valid_tokens=9, gradient=(0.0, 1.0)
                )
            ),
            SubmitResult.STEP_FAILED,
        )

        result = coordinator.settle()

        self.assertIs(result.status, StepStatus.FAILED_CONFLICT)
        self.assertEqual(result.parameters, (0.0, 0.0))
        self.assertIsNone(result.learning_rate)
        self.assertEqual(result.lr_position, 0)
        self.assertEqual(coordinator.lr_position, 0)

        # 失败步骤同样推进步号，学习率位置留待下一个成功步。
        self.assertEqual(coordinator.current_step, 1)
        self.assertIs(
            coordinator.submit(
                GradientReport(
                    step=1, mb_id="a", rank=0, valid_tokens=1, gradient=(2.0, 0.0)
                )
            ),
            SubmitResult.ACCEPTED,
        )
        self.assertIs(
            coordinator.submit(
                GradientReport(
                    step=1, mb_id="b", rank=1, valid_tokens=1, gradient=(0.0, 2.0)
                )
            ),
            SubmitResult.ACCEPTED,
        )
        second = coordinator.settle()
        self.assertIs(second.status, StepStatus.APPLIED)
        self.assertEqual(second.lr_position, 0)
        self.assertVectorAlmostEqual(second.parameters, (-1.0, -1.0))

    def test_conflict_fails_step_even_with_incomplete_roster(self) -> None:
        coordinator = make_coordinator(lr_schedule=(1.0,))
        coordinator.submit(
            GradientReport(step=0, mb_id="a", rank=0, valid_tokens=1, gradient=(1.0, 0.0))
        )
        self.assertIs(
            coordinator.submit(
                GradientReport(
                    step=0, mb_id="a", rank=0, valid_tokens=5, gradient=(1.0, 0.0)
                )
            ),
            SubmitResult.CONFLICT,
        )

        # b 从未上报，但该步已被冲突判定失败，无需补齐名单。
        result = coordinator.settle()

        self.assertIs(result.status, StepStatus.FAILED_CONFLICT)
        self.assertIsNotNone(result.reason)
        self.assertEqual([c.mb_id for c in result.contributions], ["a"])
        self.assertEqual(result.parameters, (0.0, 0.0))
        self.assertEqual(coordinator.current_step, 1)

    def test_overflow_and_gradient_conflict_is_conflict(self) -> None:
        coordinator = make_coordinator()
        coordinator.submit(
            GradientReport(step=0, mb_id="a", rank=0, valid_tokens=4, overflow=True)
        )
        self.assertIs(
            coordinator.submit(
                GradientReport(
                    step=0, mb_id="a", rank=0, valid_tokens=4, gradient=(1.0, 1.0)
                )
            ),
            SubmitResult.CONFLICT,
        )
        self.assertIs(coordinator.settle().status, StepStatus.FAILED_CONFLICT)

    def test_stale_receipt_after_settlement_changes_nothing(self) -> None:
        coordinator = make_coordinator(lr_schedule=(1.0, 0.5))
        for mb_id, rank, gradient in (("a", 0, (1.0, 0.0)), ("b", 1, (0.0, 1.0))):
            coordinator.submit(
                GradientReport(
                    step=0, mb_id=mb_id, rank=rank, valid_tokens=1, gradient=gradient
                )
            )
        first = coordinator.settle()
        snapshot = coordinator.parameters

        # 旧步的回执（包括冲突载荷）一律不再影响状态。
        self.assertIs(
            coordinator.submit(
                GradientReport(
                    step=0, mb_id="a", rank=0, valid_tokens=99, gradient=(9.0, 9.0)
                )
            ),
            SubmitResult.STALE,
        )
        self.assertIs(
            coordinator.submit(
                GradientReport(
                    step=0, mb_id="b", rank=1, valid_tokens=1, overflow=True
                )
            ),
            SubmitResult.STALE,
        )

        self.assertEqual(coordinator.parameters, snapshot)
        self.assertEqual(coordinator.parameters, first.parameters)
        self.assertEqual(coordinator.lr_position, 1)
        self.assertEqual(coordinator.current_step, 1)
        self.assertEqual(len(coordinator.history), 1)

    def test_stale_receipt_after_skipped_step_changes_nothing(self) -> None:
        coordinator = make_coordinator(lr_schedule=(1.0,))
        coordinator.submit(
            GradientReport(step=0, mb_id="a", rank=0, valid_tokens=0, overflow=True)
        )
        coordinator.submit(
            GradientReport(step=0, mb_id="b", rank=1, valid_tokens=0, gradient=None)
        )
        self.assertIs(coordinator.settle().status, StepStatus.SKIPPED_OVERFLOW)

        # 跳过的步结束后同样不再接受该步回执。
        self.assertIs(
            coordinator.submit(
                GradientReport(
                    step=0, mb_id="a", rank=0, valid_tokens=4, gradient=(1.0, 1.0)
                )
            ),
            SubmitResult.STALE,
        )
        self.assertEqual(coordinator.current_step, 1)
        self.assertEqual(coordinator.lr_position, 0)
        self.assertEqual(len(coordinator.history), 1)

    def test_future_step_is_rejected_without_state_change(self) -> None:
        coordinator = make_coordinator()
        with self.assertRaises(FutureStepError) as raised:
            coordinator.submit(
                GradientReport(
                    step=1, mb_id="a", rank=0, valid_tokens=1, gradient=(1.0, 0.0)
                )
            )
        self.assertEqual(raised.exception.current_step, 0)
        self.assertEqual(raised.exception.report_step, 1)
        self.assertEqual(coordinator.current_step, 0)
        self.assertEqual(coordinator.pending_micro_batches, ("a", "b"))


class ValidationTest(CoordinatorTestCase):
    def test_unknown_micro_batch_is_rejected(self) -> None:
        coordinator = make_coordinator()
        with self.assertRaises(UnknownMicroBatchError):
            coordinator.submit(
                GradientReport(
                    step=0, mb_id="zzz", rank=0, valid_tokens=1, gradient=(1.0, 0.0)
                )
            )
        self.assertEqual(coordinator.pending_micro_batches, ("a", "b"))

    def test_rank_mismatch_is_rejected(self) -> None:
        coordinator = make_coordinator()
        with self.assertRaises(RankMismatchError):
            coordinator.submit(
                GradientReport(
                    step=0, mb_id="a", rank=1, valid_tokens=1, gradient=(1.0, 0.0)
                )
            )
        self.assertEqual(coordinator.pending_micro_batches, ("a", "b"))

    def test_gradient_dimension_mismatch_is_rejected(self) -> None:
        coordinator = make_coordinator()
        with self.assertRaises(ValueError):
            coordinator.submit(
                GradientReport(
                    step=0, mb_id="a", rank=0, valid_tokens=1, gradient=(1.0,)
                )
            )

    def test_missing_gradient_for_positive_tokens_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            GradientReport(step=0, mb_id="a", rank=0, valid_tokens=3, gradient=None)

    def test_non_finite_gradient_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            GradientReport(
                step=0, mb_id="a", rank=0, valid_tokens=1, gradient=(math.inf,)
            )

    def test_overflow_report_may_carry_non_finite_gradient(self) -> None:
        # 溢出时梯度可能已经是 inf/nan；该步整体跳过，不参与计算。
        coordinator = make_coordinator(lr_schedule=(1.0,))
        coordinator.submit(
            GradientReport(
                step=0, mb_id="a", rank=0, valid_tokens=4, overflow=True,
                gradient=(math.inf, math.nan),
            )
        )
        coordinator.submit(
            GradientReport(
                step=0, mb_id="b", rank=1, valid_tokens=4, gradient=(1.0, 1.0)
            )
        )

        result = coordinator.settle()

        # a 溢出 → 整步跳过，参数不变，即使 b 本身是正常贡献。
        self.assertIs(result.status, StepStatus.SKIPPED_OVERFLOW)
        self.assertEqual(result.total_valid_tokens, 8)
        self.assertEqual(result.parameters, (0.0, 0.0))

    def test_dropped_overflow_gradient_does_not_affect_identity(self) -> None:
        coordinator = make_coordinator()
        coordinator.submit(
            GradientReport(
                step=0, mb_id="a", rank=0, valid_tokens=2, overflow=True,
                gradient=(math.inf, math.nan),
            )
        )
        # 同为 overflow 的微批，即使梯度载荷不同也算同一次上报。
        self.assertIs(
            coordinator.submit(
                GradientReport(step=0, mb_id="a", rank=0, valid_tokens=2, overflow=True)
            ),
            SubmitResult.DUPLICATE,
        )

    def test_duplicate_roster_id_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            StepCoordinator(
                parameters=(0.0,),
                roster=(MicroBatch("a", 0), MicroBatch("a", 1)),
                lr_schedule=(1.0,),
            )

    def test_parameters_are_copied(self) -> None:
        source = [0.0, 0.0]
        coordinator = StepCoordinator(
            parameters=source,
            roster=(MicroBatch("a", 0),),
            lr_schedule=(1.0,),
        )
        source[0] = 100.0

        self.assertEqual(coordinator.parameters, (0.0, 0.0))


class InitialStateTest(CoordinatorTestCase):
    def test_new_coordinator_state(self) -> None:
        coordinator = make_coordinator(parameters=(0.5, -0.5), lr_schedule=(0.1,))

        self.assertEqual(coordinator.parameters, (0.5, -0.5))
        self.assertEqual(coordinator.current_step, 0)
        self.assertEqual(coordinator.lr_position, 0)
        self.assertEqual(coordinator.pending_micro_batches, ("a", "b"))
        self.assertEqual(coordinator.history, ())
        self.assertIn("step=0", repr(coordinator))

    def test_settle_without_any_report_is_incomplete(self) -> None:
        coordinator = make_coordinator()
        with self.assertRaises(IncompleteStepError) as raised:
            coordinator.settle()
        self.assertEqual(raised.exception.missing, ("a", "b"))
        self.assertEqual(coordinator.current_step, 0)

    def test_non_finite_update_keeps_step_pending(self) -> None:
        # 有限输入也可能让 lr * grad 溢出为 inf；此时不得提交该步。
        coordinator = StepCoordinator(
            parameters=(1e308,),
            roster=(MicroBatch("a", 0),),
            lr_schedule=(1e308,),
        )
        coordinator.submit(
            GradientReport(
                step=0, mb_id="a", rank=0, valid_tokens=1, gradient=(1e308,)
            )
        )

        with self.assertRaises(CoordinatorError):
            coordinator.settle()

        self.assertEqual(coordinator.parameters, (1e308,))
        self.assertEqual(coordinator.current_step, 0)
        self.assertEqual(coordinator.lr_position, 0)
        self.assertEqual(coordinator.pending_micro_batches, ())


class HistoryTest(CoordinatorTestCase):
    def test_history_records_every_settled_step(self) -> None:
        coordinator = make_coordinator(lr_schedule=(1.0, 1.0))
        for step in range(2):
            coordinator.submit(
                GradientReport(
                    step=step, mb_id="a", rank=0, valid_tokens=0, gradient=None
                )
            )
            coordinator.submit(
                GradientReport(
                    step=step, mb_id="b", rank=1, valid_tokens=0, gradient=None
                )
            )
            coordinator.settle()

        history = coordinator.history
        self.assertEqual([entry.step for entry in history], [0, 1])
        self.assertTrue(all(entry.status is StepStatus.SKIPPED_NO_TOKENS for entry in history))
        self.assertEqual(coordinator.current_step, 2)
        self.assertEqual(coordinator.lr_position, 0)

    def test_history_snapshot_is_immutable(self) -> None:
        coordinator = make_coordinator()
        for mb_id, rank, gradient in (("a", 0, (1.0, 0.0)), ("b", 1, (0.0, 1.0))):
            coordinator.submit(
                GradientReport(
                    step=0, mb_id=mb_id, rank=rank, valid_tokens=1, gradient=gradient
                )
            )
        coordinator.settle()
        snapshot = coordinator.history
        with self.assertRaises(AttributeError):
            snapshot[0].status = StepStatus.FAILED_CONFLICT  # type: ignore[misc]


class ErrorHierarchyTest(unittest.TestCase):
    def test_specific_errors_are_coordinator_errors(self) -> None:
        for error_type in (
            UnknownMicroBatchError,
            RankMismatchError,
            FutureStepError,
            IncompleteStepError,
            LearningRateScheduleError,
        ):
            self.assertTrue(issubclass(error_type, CoordinatorError))


if __name__ == "__main__":
    unittest.main()
