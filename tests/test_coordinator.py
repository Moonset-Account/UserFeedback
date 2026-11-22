"""mixed_precision_coordinator 的 unittest 测试。

从工作区根目录运行：python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mixed_precision_coordinator import (  # noqa: E402
    AlreadyInitializedError,
    BadGradientError,
    CoordinatorConfig,
    CoordinatorError,
    DuplicateReportError,
    MixedPrecisionCoordinator,
    NoActiveStepError,
    StaleReportError,
    StepAlreadyActiveError,
    UnknownRankError,
)


def make_config(**overrides):
    base = dict(learning_rate=0.1, momentum_beta=0.9, initial_scale=4.0,
                growth_interval=2, scale_backoff=0.5, scale_growth=2.0,
                min_scale=1.0, max_scale=64.0)
    base.update(overrides)
    return CoordinatorConfig(**base)


class CoordinatorTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def make_coordinator(self, config=None, **kwargs):
        coord = MixedPrecisionCoordinator(config or make_config(),
                                          state_dir=self.dir, **kwargs)
        return coord

    def init_coordinator(self, params=(1.0, -2.0), config=None, **kwargs):
        coord = self.make_coordinator(config, **kwargs)
        coord.initialize(params)
        return coord


class TestNormalStep(CoordinatorTestBase):
    """正常步：收齐后恰好一次反缩放、合并与优化器推进。"""

    def test_single_step_values(self):
        coord = self.init_coordinator()
        epoch = coord.begin_step(step_id=1, ranks=[0, 1], num_microbatches=1)
        # rank0 真实梯度 [0.5, 0.25]，rank1 真实梯度 [-0.5, 0.75]，缩放 4
        self.assertEqual(coord.report_grad(epoch, 0, 0, [2.0, 1.0]),
                         "collecting")
        self.assertEqual(coord.report_grad(epoch, 1, 0, [-2.0, 3.0]),
                         "committed")
        # 合并梯度 = ([2,-2]+[1,3] 按分量求和) / (4*2) = [0.0, 0.5]
        # 动量 = 0.9*0 + g = [0, 0.5]；参数 = [1,-2] - 0.1*[0,0.5]
        self.assertEqual(coord.params, [1.0, -2.05])
        self.assertEqual(coord.momentum, [0.0, 0.5])
        self.assertEqual(coord.train_step, 1)
        self.assertEqual(coord.success_streak, 1)
        self.assertEqual(coord.scale, 4.0)  # 未达阈值，缩放不变

    def test_scale_grows_after_threshold_consecutive_successes(self):
        coord = self.init_coordinator()
        for step_id in (1, 2):
            epoch = coord.begin_step(step_id=step_id, ranks=[0],
                                     num_microbatches=2)
            coord.report_grad(epoch, 0, 0, [0.0, 0.0])
            coord.report_grad(epoch, 0, 1, [0.0, 0.0])
        # 第 2 步连续成功达到阈值 2 -> 缩放翻倍并重置计数
        self.assertEqual(coord.scale, 8.0)
        self.assertEqual(coord.success_streak, 0)
        self.assertEqual(coord.train_step, 2)
        # 动量 = 0.9*[0,0.5?]... 第一步动量 [0,0.5]? 此处梯度全 0：
        # 第一步后动量 [0,0]，第二步动量仍 [0,0]
        self.assertEqual(coord.momentum, [0.0, 0.0])
        self.assertEqual(coord.params, [1.0, -2.0])

    def test_momentum_accumulates_across_steps(self):
        coord = self.init_coordinator()
        e1 = coord.begin_step(1, ranks=[0], num_microbatches=1)
        coord.report_grad(e1, 0, 0, [4.0, -4.0])  # 真实梯度 [1, -1]
        e2 = coord.begin_step(2, ranks=[0], num_microbatches=1)
        coord.report_grad(e2, 0, 0, [4.0, -4.0])
        # m1 = [1,-1]; p1 = [1,-2]-0.1*[1,-1] = [0.9, -1.9]
        # m2 = 0.9*[1,-1]+[1,-1] = [1.9,-1.9]; p2 = p1-0.1*m2 = [0.71, -1.71]
        self.assertAlmostEqual(coord.params[0], 0.71)
        self.assertAlmostEqual(coord.params[1], -1.71)
        self.assertAlmostEqual(coord.momentum[0], 1.9)
        self.assertAlmostEqual(coord.momentum[1], -1.9)

    def test_finalize_happens_exactly_once(self):
        coord = self.init_coordinator()
        epoch = coord.begin_step(1, ranks=[0], num_microbatches=1)
        coord.report_grad(epoch, 0, 0, [4.0, 4.0])
        self.assertEqual(coord.train_step, 1)
        # 收齐后同 epoch 的额外报告不得再次触发推进
        with self.assertRaises(NoActiveStepError):
            coord.report_grad(epoch, 0, 0, [4.0, 4.0])
        self.assertEqual(coord.train_step, 1)


class TestOverflowStep(CoordinatorTestBase):
    """溢出步：整步跳过、保留参数动量、下调缩放。"""

    def test_nonfinite_flag_skips_step(self):
        coord = self.init_coordinator()
        epoch = coord.begin_step(1, ranks=[0, 1], num_microbatches=1)
        coord.report_grad(epoch, 0, 0, [2.0, 1.0])
        result = coord.report_grad(epoch, 1, 0, [0.0, 0.0], finite=False)
        self.assertEqual(result, "overflow_skipped")
        self.assertEqual(coord.params, [1.0, -2.0])   # 参数保留
        self.assertEqual(coord.momentum, [0.0, 0.0])  # 动量保留
        self.assertEqual(coord.train_step, 0)         # 步号不推进
        self.assertEqual(coord.scale, 2.0)            # 缩放下调
        self.assertEqual(coord.success_streak, 0)

    def test_nonfinite_value_detected_even_if_flag_true(self):
        coord = self.init_coordinator()
        epoch = coord.begin_step(1, ranks=[0], num_microbatches=1)
        result = coord.report_grad(epoch, 0, 0, [float("inf"), 1.0],
                                   finite=True)
        self.assertEqual(result, "overflow_skipped")
        self.assertEqual(coord.scale, 2.0)
        self.assertEqual(coord.train_step, 0)

    def test_nan_detected(self):
        coord = self.init_coordinator()
        epoch = coord.begin_step(1, ranks=[0], num_microbatches=1)
        result = coord.report_grad(epoch, 0, 0, [float("nan"), 1.0])
        self.assertEqual(result, "overflow_skipped")

    def test_overflow_resets_success_streak(self):
        coord = self.init_coordinator()
        e1 = coord.begin_step(1, ranks=[0], num_microbatches=1)
        coord.report_grad(e1, 0, 0, [0.0, 0.0])
        self.assertEqual(coord.success_streak, 1)
        e2 = coord.begin_step(2, ranks=[0], num_microbatches=1)
        coord.report_grad(e2, 0, 0, [0.0, 0.0], finite=False)
        self.assertEqual(coord.success_streak, 0)
        self.assertEqual(coord.scale, 2.0)

    def test_scale_floor(self):
        coord = self.init_coordinator(config=make_config(initial_scale=1.0))
        for step_id in (1, 2):
            epoch = coord.begin_step(step_id, ranks=[0], num_microbatches=1)
            coord.report_grad(epoch, 0, 0, [0.0, 0.0], finite=False)
        self.assertEqual(coord.scale, 1.0)  # 不低于 min_scale

    def test_scale_ceiling(self):
        coord = self.init_coordinator(config=make_config(max_scale=8.0))
        for step_id in (1, 2, 3, 4):
            epoch = coord.begin_step(step_id, ranks=[0], num_microbatches=1)
            coord.report_grad(epoch, 0, 0, [0.0, 0.0])
        self.assertEqual(coord.scale, 8.0)  # 不超过 max_scale


class TestAbortAndRetry(CoordinatorTestBase):
    """掉线撤销与重试：保留缩放、拒绝旧 epoch 迟到报告。"""

    def test_abort_preserves_scale_and_streak(self):
        coord = self.init_coordinator()
        e1 = coord.begin_step(1, ranks=[0], num_microbatches=1)
        coord.report_grad(e1, 0, 0, [0.0, 0.0])  # 成功：streak=1
        # 第 2 步收集中成员退出
        e2 = coord.begin_step(2, ranks=[0, 1], num_microbatches=1)
        coord.report_grad(e2, 0, 0, [1.0, 1.0])
        coord.abort_step(e2)
        self.assertFalse(coord.has_active_step)
        self.assertEqual(coord.scale, 4.0)        # 缩放保留，不下调
        self.assertEqual(coord.success_streak, 1)  # 连续成功计数保留
        self.assertEqual(coord.train_step, 1)
        self.assertEqual(coord.params, [1.0, -2.0])

    def test_late_report_from_aborted_epoch_rejected(self):
        coord = self.init_coordinator()
        e1 = coord.begin_step(1, ranks=[0, 1], num_microbatches=1)
        coord.abort_step(e1)
        # 撤销后旧 epoch 报告：无活动步
        with self.assertRaises(NoActiveStepError):
            coord.report_grad(e1, 1, 0, [1.0, 1.0])
        # 重试步使用新 epoch
        e2 = coord.begin_step(1, ranks=[0, 1], num_microbatches=1)
        self.assertNotEqual(e1, e2)
        # 旧 epoch 的迟到报告不能进入重试步
        with self.assertRaises(StaleReportError):
            coord.report_grad(e1, 1, 0, [9.0, 9.0])
        self.assertEqual(coord.pending_reports, 0)
        # 重试步正常完成
        coord.report_grad(e2, 0, 0, [4.0, 4.0])
        self.assertEqual(coord.report_grad(e2, 1, 0, [4.0, 4.0]),
                         "committed")
        self.assertEqual(coord.train_step, 1)
        # 合并梯度 = [8,8]/(4*2) = [1,1]
        self.assertEqual(coord.momentum, [1.0, 1.0])
        self.assertAlmostEqual(coord.params[0], 0.9)
        self.assertAlmostEqual(coord.params[1], -2.1)

    def test_comm_failure_not_confused_with_overflow(self):
        """通信中断（撤销）不改变缩放轨迹，溢出才下调。"""
        coord = self.init_coordinator()
        scales = []
        # 成功
        e = coord.begin_step(1, ranks=[0], num_microbatches=1)
        coord.report_grad(e, 0, 0, [0.0, 0.0])
        scales.append(coord.scale)  # 4
        # 撤销（通信中断）
        e = coord.begin_step(2, ranks=[0], num_microbatches=1)
        coord.abort_step(e)
        scales.append(coord.scale)  # 仍为 4，streak 仍为 1
        # 再成功 -> streak 达阈值 -> 上调
        e = coord.begin_step(2, ranks=[0], num_microbatches=1)
        coord.report_grad(e, 0, 0, [0.0, 0.0])
        scales.append(coord.scale)  # 8
        # 溢出 -> 下调
        e = coord.begin_step(3, ranks=[0], num_microbatches=1)
        coord.report_grad(e, 0, 0, [0.0, 0.0], finite=False)
        scales.append(coord.scale)  # 4
        self.assertEqual(scales, [4.0, 4.0, 8.0, 4.0])
        self.assertEqual(coord.train_step, 2)

    def test_abort_wrong_epoch_rejected(self):
        coord = self.init_coordinator()
        e1 = coord.begin_step(1, ranks=[0], num_microbatches=1)
        with self.assertRaises(StaleReportError):
            coord.abort_step(e1 + 99)
        self.assertTrue(coord.has_active_step)


class TestCrashRecovery(CoordinatorTestBase):
    """恢复点：汇总完成与优化器发布之间崩溃。"""

    class _Crash(Exception):
        pass

    def _run_step_with_grads(self, coord, step_id=1):
        epoch = coord.begin_step(step_id, ranks=[0, 1], num_microbatches=1)
        coord.report_grad(epoch, 0, 0, [2.0, 1.0])
        return coord.report_grad(epoch, 1, 0, [-2.0, 3.0]), epoch

    def test_recover_between_aggregate_and_publish(self):
        crashed = self.init_coordinator(
            crash_hook=lambda: (_ for _ in ()).throw(self._Crash()))
        with self.assertRaises(self._Crash):
            self._run_step_with_grads(crashed)
        # journal 已落盘、优化器未发布
        self.assertTrue(os.path.exists(
            os.path.join(self.dir, "journal.json")))
        self.assertEqual(crashed.train_step, 0)

        # 恢复：幂等重放，恰好推进一次
        recovered = self.make_coordinator()
        self.assertEqual(recovered.train_step, 1)
        self.assertEqual(recovered.params, [1.0, -2.05])
        self.assertEqual(recovered.momentum, [0.0, 0.5])
        self.assertEqual(recovered.scale, 4.0)
        self.assertEqual(recovered.success_streak, 1)
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, "journal.json")))

        # 与无崩溃的参照运行结果完全一致
        with tempfile.TemporaryDirectory() as ref_dir:
            ref = MixedPrecisionCoordinator(make_config(), state_dir=ref_dir)
            ref.initialize([1.0, -2.0])
            epoch = ref.begin_step(1, ranks=[0, 1], num_microbatches=1)
            ref.report_grad(epoch, 0, 0, [2.0, 1.0])
            ref.report_grad(epoch, 1, 0, [-2.0, 3.0])
            self.assertEqual(recovered.params, ref.params)
            self.assertEqual(recovered.momentum, ref.momentum)
            self.assertEqual(recovered.train_step, ref.train_step)
            self.assertEqual(recovered.scale, ref.scale)

    def test_recovery_is_idempotent(self):
        crashed = self.init_coordinator(
            crash_hook=lambda: (_ for _ in ()).throw(self._Crash()))
        with self.assertRaises(self._Crash):
            self._run_step_with_grads(crashed)
        first = self.make_coordinator()
        second = self.make_coordinator()  # 再次加载不应重复推进
        self.assertEqual(second.train_step, 1)
        self.assertEqual(second.params, first.params)
        self.assertEqual(second.momentum, first.momentum)

    def test_crash_mid_collection_leaves_no_partial_step(self):
        coord = self.init_coordinator()
        epoch = coord.begin_step(1, ranks=[0, 1], num_microbatches=1)
        coord.report_grad(epoch, 0, 0, [2.0, 1.0])
        # 模拟崩溃：直接新建协调器（收集中的报告不持久化）
        recovered = self.make_coordinator()
        self.assertFalse(recovered.has_active_step)
        self.assertEqual(recovered.train_step, 0)
        self.assertEqual(recovered.params, [1.0, -2.0])
        # 旧 epoch 报告在重启后同样被拒绝
        with self.assertRaises(NoActiveStepError):
            recovered.report_grad(epoch, 1, 0, [-2.0, 3.0])
        # 可以以新 epoch 重试整步
        e2 = recovered.begin_step(1, ranks=[0, 1], num_microbatches=1)
        self.assertGreater(e2, epoch)

    def test_overflow_step_persists_across_restart(self):
        coord = self.init_coordinator()
        epoch = coord.begin_step(1, ranks=[0], num_microbatches=1)
        coord.report_grad(epoch, 0, 0, [0.0, 0.0], finite=False)
        recovered = self.make_coordinator()
        self.assertEqual(recovered.scale, 2.0)
        self.assertEqual(recovered.train_step, 0)
        self.assertEqual(recovered.params, [1.0, -2.0])


class TestValidation(CoordinatorTestBase):
    """边界与输入校验。"""

    def test_begin_while_active_rejected(self):
        coord = self.init_coordinator()
        coord.begin_step(1, ranks=[0], num_microbatches=1)
        with self.assertRaises(StepAlreadyActiveError):
            coord.begin_step(2, ranks=[0], num_microbatches=1)

    def test_duplicate_report_rejected(self):
        coord = self.init_coordinator()
        epoch = coord.begin_step(1, ranks=[0], num_microbatches=2)
        coord.report_grad(epoch, 0, 0, [1.0, 1.0])
        with self.assertRaises(DuplicateReportError):
            coord.report_grad(epoch, 0, 0, [1.0, 1.0])

    def test_unknown_rank_rejected(self):
        coord = self.init_coordinator()
        epoch = coord.begin_step(1, ranks=[0], num_microbatches=1)
        with self.assertRaises(UnknownRankError):
            coord.report_grad(epoch, 7, 0, [1.0, 1.0])

    def test_microbatch_index_out_of_range(self):
        coord = self.init_coordinator()
        epoch = coord.begin_step(1, ranks=[0], num_microbatches=1)
        with self.assertRaises(CoordinatorError):
            coord.report_grad(epoch, 0, 1, [1.0, 1.0])

    def test_grad_dim_mismatch_rejected(self):
        coord = self.init_coordinator()
        epoch = coord.begin_step(1, ranks=[0], num_microbatches=1)
        with self.assertRaises(BadGradientError):
            coord.report_grad(epoch, 0, 0, [1.0, 1.0, 1.0])

    def test_report_without_active_step_rejected(self):
        coord = self.init_coordinator()
        with self.assertRaises(NoActiveStepError):
            coord.report_grad(1, 0, 0, [1.0, 1.0])

    def test_begin_before_initialize_rejected(self):
        coord = self.make_coordinator()
        with self.assertRaises(Exception):
            coord.begin_step(1, ranks=[0], num_microbatches=1)

    def test_double_initialize_rejected(self):
        coord = self.init_coordinator()
        with self.assertRaises(AlreadyInitializedError):
            coord.initialize([0.0, 0.0])
        # 从持久态恢复后同样拒绝重新初始化
        recovered = self.make_coordinator()
        with self.assertRaises(AlreadyInitializedError):
            recovered.initialize([0.0, 0.0])

    def test_empty_ranks_rejected(self):
        coord = self.init_coordinator()
        with self.assertRaises(CoordinatorError):
            coord.begin_step(1, ranks=[], num_microbatches=1)


if __name__ == "__main__":
    unittest.main()
