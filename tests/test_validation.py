"""故障恢复、边界条件与视图重绑测试。"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tensor_coord import (  # noqa: E402
    InvalidContributionError,
    InvalidViewError,
    PhysicalStorage,
    TrainingCoordinator,
    View,
)


class ViewValidationTest(unittest.TestCase):
    def test_out_of_range_offset(self):
        storage = PhysicalStorage([1.0, 2.0, 3.0, 4.0])
        view = View(storage, shape=(2, 2), strides=(2, 1), offset=3)
        with self.assertRaises(InvalidViewError):
            view.resolve()

    def test_stride_lands_outside_storage(self):
        storage = PhysicalStorage([1.0, 2.0, 3.0, 4.0])
        view = View(storage, shape=(2, 2), strides=(5, 1))
        with self.assertRaises(InvalidViewError):
            view.resolve()

    def test_aliased_view_is_rejected(self):
        storage = PhysicalStorage([1.0, 2.0, 3.0, 4.0])
        # strides=(1,1)：两个逻辑维度命中同一物理位置
        view = View(storage, shape=(2, 2), strides=(1, 1))
        with self.assertRaises(InvalidViewError):
            view.resolve()

    def test_dimension_mismatch_between_shape_and_strides(self):
        storage = PhysicalStorage([1.0, 2.0, 3.0, 4.0])
        with self.assertRaises(InvalidViewError):
            View(storage, shape=(2, 2), strides=(2,))

    def test_negative_and_boolean_dimensions(self):
        storage = PhysicalStorage([1.0, 2.0, 3.0, 4.0])
        with self.assertRaises(InvalidViewError):
            View(storage, shape=(2, -2), strides=(1, 1))
        with self.assertRaises(InvalidViewError):
            View(storage, shape=(True, 2), strides=(1, 1))
        with self.assertRaises(InvalidViewError):
            View(storage, shape=(2, 2), strides=(True, 1))
        with self.assertRaises(InvalidViewError):
            View(storage, shape=(2, 2), strides=(1, 1), offset=-1)

    def test_nan_weight_rejected(self):
        with self.assertRaises(ValueError):
            PhysicalStorage([1.0, float("nan")])


class AbortAndRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.storage = PhysicalStorage([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
        self.coord = TrainingCoordinator(self.storage, momentum=0.5)
        self.view = View(self.storage, shape=(2, 3), strides=(3, 1))

    def _snapshot(self):
        return self.storage.buffer, self.coord.momentum

    def test_invalid_mapping_aborts_whole_step(self):
        before_w, before_m = self._snapshot()
        self.assertTrue(
            self.coord.add_contribution(self.view, [[1, 1, 1], [1, 1, 1]], "ok")
        )
        bad = View(self.storage, shape=(2, 3), strides=(9, 1))
        with self.assertRaises(InvalidViewError):
            self.coord.add_contribution(bad, [[0] * 3, [0] * 3], "bad")

        # 作废后再加任何贡献都被拒绝
        self.assertFalse(
            self.coord.add_contribution(self.view, [[1, 1, 1], [1, 1, 1]], "x")
        )

        result = self.coord.commit_step(0.5)
        self.assertTrue(result.aborted)
        self.assertEqual(result.touched_positions, ())
        self.assertEqual(self._snapshot(), (before_w, before_m))

        # 新一步合法输入可以正常重跑
        self.assertEqual(self.coord.step_id, 2)
        self.assertTrue(
            self.coord.add_contribution(self.view, [[1, 2, 3], [4, 5, 6]], "ok2")
        )
        result2 = self.coord.commit_step(0.5)
        self.assertFalse(result2.aborted)
        self.assertEqual(
            result2.aggregated_gradients,
            {0: 1.0, 1: 2.0, 2: 3.0, 3: 4.0, 4: 5.0, 5: 6.0},
        )
        self.assertEqual(self.coord.weights, [-0.5, 0.0, 0.5, 1.0, 1.5, 2.0])
        self.assertEqual(self.coord.momentum, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])

    def test_wrong_element_count_aborts(self):
        before_w, before_m = self._snapshot()
        with self.assertRaises(InvalidContributionError):
            self.coord.add_contribution(self.view, [[1, 2], [3, 4]], "bad")
        result = self.coord.commit_step()
        self.assertTrue(result.aborted)
        self.assertEqual(self._snapshot(), (before_w, before_m))

    def test_nan_gradient_aborts(self):
        before_w, before_m = self._snapshot()
        with self.assertRaises(InvalidContributionError):
            self.coord.add_contribution(
                self.view, [[1.0, float("nan"), 3], [4, 5, 6]], "nan"
            )
        result = self.coord.commit_step()
        self.assertTrue(result.aborted)
        self.assertEqual(self._snapshot(), (before_w, before_m))

    def test_non_numeric_gradient_aborts(self):
        with self.assertRaises(InvalidContributionError):
            self.coord.add_contribution(
                self.view, [[1, "x", 3], [4, 5, 6]], "str"
            )
        result = self.coord.commit_step()
        self.assertTrue(result.aborted)

    def test_unhashable_id_rejected_without_poisoning(self):
        # 不可哈希 ID 属于调用方参数错误，不应把合法的一步毒掉
        with self.assertRaises(InvalidContributionError):
            self.coord.add_contribution(
                self.view, [[1, 1, 1], [1, 1, 1]], ["not", "hashable"]
            )
        self.assertTrue(
            self.coord.add_contribution(
                self.view, [[1, 2, 3], [4, 5, 6]], "still-ok"
            )
        )
        result = self.coord.commit_step(0.1)
        self.assertFalse(result.aborted)

    def test_repeated_aborts_then_recovery(self):
        # 连续多步作废，状态始终保持，最后一步成功
        initial_w, initial_m = self._snapshot()
        for step in range(3):
            bad = View(self.storage, shape=(2, 3), strides=(100, 1))
            with self.assertRaises(InvalidViewError):
                self.coord.add_contribution(bad, [[0] * 3, [0] * 3], f"b{step}")
            aborted = self.coord.commit_step()
            self.assertTrue(aborted.aborted)
        self.assertEqual(self._snapshot(), (initial_w, initial_m))

        self.assertTrue(
            self.coord.add_contribution(
                self.view, [[2, 2, 2], [2, 2, 2]], "fine"
            )
        )
        ok = self.coord.commit_step(0.25)
        self.assertFalse(ok.aborted)
        self.assertEqual(
            self.coord.weights,
            [w - 0.5 for w in initial_w],
        )

    def test_begin_step_discards_pending_contributions(self):
        self.coord.add_contribution(self.view, [[1, 1, 1], [1, 1, 1]], "a")
        self.assertEqual(self.coord.begin_step(), 2)
        self.assertEqual(self.coord.pending_aggregation(), {})
        result = self.coord.commit_step(0.5)
        self.assertFalse(result.aborted)
        self.assertEqual(result.touched_positions, ())
        self.assertEqual(self.storage.buffer, [0.0, 1.0, 2.0, 3.0, 4.0, 5.0])

    def test_empty_step_is_valid_noop(self):
        result = self.coord.commit_step(0.5)
        self.assertFalse(result.aborted)
        self.assertEqual(result.updated_count, 0)
        self.assertEqual(self.storage.buffer, [0.0, 1.0, 2.0, 3.0, 4.0, 5.0])


class RebindTest(unittest.TestCase):
    def test_rebind_after_snapshot_does_not_replace_fixed_contribution(self):
        storage = PhysicalStorage([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
        other = PhysicalStorage([10.0, 11.0, 12.0, 13.0, 14.0, 15.0])
        view = View(storage, shape=(2, 3), strides=(3, 1))

        coord = TrainingCoordinator(storage, momentum=0.0)
        self.assertTrue(
            coord.add_contribution(view, [[1, 2, 3], [4, 5, 6]], "c1")
        )

        # 已固定的贡献映射到旧存储；重绑不能替换它
        view.rebind(other)
        self.assertIs(view.storage, other)
        # 重绑后的视图不属于该协调器：新贡献应让本步作废
        with self.assertRaises(InvalidViewError):
            coord.add_contribution(view, [[1, 1, 1], [1, 1, 1]], "c2")
        result = coord.commit_step(0.5)
        self.assertTrue(result.aborted)
        # 旧存储完全没动，另一个存储也没动
        self.assertEqual(storage.buffer, [0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertEqual(other.buffer, [10.0, 11.0, 12.0, 13.0, 14.0, 15.0])

        # 重绑回原存储后，下一步可以正常提交
        view.rebind(storage)
        self.assertTrue(
            coord.add_contribution(view, [[1, 2, 3], [4, 5, 6]], "c3")
        )
        ok = coord.commit_step(0.5)
        self.assertFalse(ok.aborted)
        self.assertEqual(
            storage.buffer,
            [-0.5, 0.0, 0.5, 1.0, 1.5, 2.0],
        )
        self.assertEqual(other.buffer, [10.0, 11.0, 12.0, 13.0, 14.0, 15.0])

    def test_rebind_requires_equal_size(self):
        storage = PhysicalStorage([0.0, 1.0, 2.0])
        other = PhysicalStorage([0.0, 1.0, 2.0, 3.0])
        view = View(storage, shape=(3,), strides=(1,))
        with self.assertRaises(InvalidViewError):
            view.rebind(other)

    def test_foreign_storage_view_aborts_step(self):
        storage = PhysicalStorage([0.0, 1.0, 2.0, 3.0])
        foreign = PhysicalStorage([9.0, 9.0, 9.0, 9.0])
        coord = TrainingCoordinator(storage)
        foreign_view = View(foreign, shape=(2, 2), strides=(2, 1))
        with self.assertRaises(InvalidViewError):
            coord.add_contribution(foreign_view, [[1, 2], [3, 4]], "f")
        result = coord.commit_step()
        self.assertTrue(result.aborted)
        self.assertEqual(storage.buffer, [0.0, 1.0, 2.0, 3.0])
        self.assertEqual(foreign.buffer, [9.0, 9.0, 9.0, 9.0])


class BoundaryAndOverlapTest(unittest.TestCase):
    def test_single_element_view(self):
        storage = PhysicalStorage([7.0])
        coord = TrainingCoordinator(storage, momentum=0.0)
        view = View(storage, shape=(1,), strides=(1,))
        self.assertTrue(coord.add_contribution(view, [3.0], "s"))
        result = coord.commit_step(2.0)
        self.assertEqual(result.touched_positions, (0,))
        self.assertEqual(storage.buffer, [1.0])  # 7 - 2*3

    def test_three_way_overlap_sums_all_contributions(self):
        storage = PhysicalStorage([0.0, 0.0, 0.0])
        coord = TrainingCoordinator(storage, momentum=0.0)
        # 三个单元素视图都指向位置 1
        v1 = View(storage, shape=(1,), strides=(1,), offset=1)
        v2 = View(storage, shape=(1,), strides=(1,), offset=1)
        v3 = View(storage, shape=(1,), strides=(1,), offset=1)
        self.assertTrue(coord.add_contribution(v1, [2.0], "1"))
        self.assertTrue(coord.add_contribution(v2, [3.0], "2"))
        self.assertTrue(coord.add_contribution(v3, [5.0], "3"))
        self.assertEqual(coord.pending_aggregation(), {1: 10.0})
        result = coord.commit_step(1.0)
        self.assertEqual(result.touched_positions, (1,))  # 物理位置只更新一次
        self.assertEqual(storage.buffer, [0.0, -10.0, 0.0])

    def test_zero_learning_rate_keeps_weights_updates_momentum(self):
        storage = PhysicalStorage([1.0, 2.0])
        coord = TrainingCoordinator(storage, momentum=0.5)
        view = View(storage, shape=(2,), strides=(1,))
        coord.add_contribution(view, [4.0, 8.0], "g")
        result = coord.commit_step(0.0)
        self.assertEqual(storage.buffer, [1.0, 2.0])
        self.assertEqual(coord.momentum, [4.0, 8.0])
        self.assertEqual(result.momentum_after, {0: 4.0, 1: 8.0})

    def test_negative_learning_rate_rejected(self):
        storage = PhysicalStorage([1.0])
        coord = TrainingCoordinator(storage)
        view = View(storage, shape=(1,), strides=(1,))
        coord.add_contribution(view, [1.0], "g")
        with self.assertRaises(ValueError):
            coord.commit_step(-0.1)

    def test_momentum_coefficient_validation(self):
        storage = PhysicalStorage([1.0])
        with self.assertRaises(ValueError):
            TrainingCoordinator(storage, momentum=1.0)
        with self.assertRaises(ValueError):
            TrainingCoordinator(storage, momentum=-0.1)

    def test_nested_gradient_flattened_row_major(self):
        storage = PhysicalStorage([0.0, 0.0, 0.0, 0.0])
        coord = TrainingCoordinator(storage, momentum=0.0)
        view = View(storage, shape=(2, 2), strides=(2, 1))
        coord.add_contribution(view, [[1, 2], [3, 4]], "n")
        self.assertEqual(
            coord.pending_aggregation(), {0: 1.0, 1: 2.0, 2: 3.0, 3: 4.0}
        )

    def test_ids_reset_across_steps(self):
        storage = PhysicalStorage([0.0, 1.0])
        coord = TrainingCoordinator(storage, momentum=0.0)
        view = View(storage, shape=(2,), strides=(1,))
        coord.add_contribution(view, [1.0, 1.0], "same-id")
        coord.commit_step(1.0)
        # 新一步里同一 ID 是全新的一份贡献
        self.assertTrue(
            coord.add_contribution(view, [10.0, 10.0], "same-id")
        )
        result = coord.commit_step(1.0)
        self.assertFalse(result.aborted)
        self.assertEqual(storage.buffer, [-11.0, -10.0])


if __name__ == "__main__":
    unittest.main()
