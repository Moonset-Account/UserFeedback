"""手算核对：两份部分重叠的切片 + 一个转置视图。

物理存储（12 个位置）::

    W = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]

视图 A：shape=(2,3), strides=(3,1), offset=0
    逻辑行优先 -> 物理下标 [0,1,2,3,4,5]
视图 B：shape=(2,3), strides=(3,1), offset=4（与 A 在 4、5 重叠）
    逻辑行优先 -> 物理下标 [4,5,6,7,8,9]
视图 T：shape=(3,2), strides=(1,3), offset=0（转置视图）
    逻辑 (i,j) -> i*1 + j*3，行优先 -> [0,3,1,4,2,5]

超参数：momentum=0.5，learning_rate=0.5，动量初值全 0。
所有手算值都是 0.5/0.25 的组合，二进制浮点可精确表示。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tensor_coord import (  # noqa: E402
    InvalidContributionError,
    PhysicalStorage,
    TrainingCoordinator,
    View,
)

INITIAL_WEIGHTS = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0]
MOMENTUM_COEF = 0.5
LEARNING_RATE = 0.5

# 第一步三份贡献的梯度
GRAD_A = [[1, 2, 3], [4, 5, 6]]
GRAD_B = [[10, 20, 30], [40, 50, 60]]
GRAD_T = [[-1, -2], [-3, -4], [-5, -6]]

# 手算：第一步每个物理位置聚合后的梯度
EXPECTED_GRAD_STEP1 = {
    0: 0.0,      # A:1 + T:-1
    1: -1.0,     # A:2 + T:-3
    2: -2.0,     # A:3 + T:-5
    3: 2.0,      # A:4 + T:-2
    4: 11.0,     # A:5 + B:10 + T:-4
    5: 20.0,     # A:6 + B:20 + T:-6
    6: 30.0,     # B:30
    7: 40.0,     # B:40
    8: 50.0,     # B:50
    9: 60.0,     # B:60
    # 10、11 未参与
}

# 手算：第一步后动量（v = 0.5*0 + g）
EXPECTED_MOM_STEP1 = [
    0.0, -1.0, -2.0, 2.0, 11.0, 20.0, 30.0, 40.0, 50.0, 60.0, 0.0, 0.0,
]

# 手算：第一步后权重 W' = W - 0.5 * v
EXPECTED_W_STEP1 = [
    0.0,     # 0 - 0.5*0
    1.5,     # 1 - 0.5*(-1)
    3.0,     # 2 - 0.5*(-2)
    2.0,     # 3 - 0.5*2
    -1.5,    # 4 - 0.5*11
    -5.0,    # 5 - 0.5*20
    -9.0,    # 6 - 0.5*30
    -13.0,   # 7 - 0.5*40
    -17.0,   # 8 - 0.5*50
    -21.0,   # 9 - 0.5*60
    10.0,    # 未参与，保持
    11.0,    # 未参与，保持
]

# 第二步：仅位置 4 再得 100；A/B/T 同 ID 回放（payload 已改）必须被忽略
GRAD_A2 = [[0, 0, 0], [0, 100, 0]]

# 手算：第二步后动量 v2 = 0.5*v1 + g2
# 注意位置 0-3、5 收到 0 梯度但仍参与公式，动量按 0.5 衰减；
# 位置 6-9 未参与，保持第一步动量；10、11 从未参与，保持 0。
EXPECTED_MOM_STEP2 = [
    0.0,     # 0.5*0 + 0
    -0.5,    # 0.5*(-1) + 0
    -1.0,    # 0.5*(-2) + 0
    1.0,     # 0.5*2 + 0
    105.5,   # 0.5*11 + 100
    10.0,    # 0.5*20 + 0
    30.0,    # 未参与，保持 v1
    40.0,
    50.0,
    60.0,
    0.0,     # 从未参与，保持 0
    0.0,
]

# 手算：第二步后权重 W'' = W' - 0.5 * v2
# 位置 6-11 未参与第二步，权重保持第一步结果。
EXPECTED_W_STEP2 = [
    0.0,      # 0 - 0
    1.75,     # 1.5 - 0.5*(-0.5)
    3.5,      # 3 - 0.5*(-1)
    1.5,      # 2 - 0.5*1
    -54.25,   # -1.5 - 0.5*105.5
    -10.0,    # -5 - 0.5*10
    -9.0,     # 未参与，保持 W'
    -13.0,
    -17.0,
    -21.0,
    10.0,
    11.0,
]


class HandCalculationTest(unittest.TestCase):
    def setUp(self):
        self.storage = PhysicalStorage(INITIAL_WEIGHTS)
        self.coord = TrainingCoordinator(self.storage, momentum=MOMENTUM_COEF)
        self.view_a = View(self.storage, shape=(2, 3), strides=(3, 1), offset=0)
        self.view_b = View(self.storage, shape=(2, 3), strides=(3, 1), offset=4)
        self.view_t = View(self.storage, shape=(3, 2), strides=(1, 3), offset=0)

    def test_view_physical_mappings(self):
        self.assertEqual(self.view_a.resolve(), (0, 1, 2, 3, 4, 5))
        self.assertEqual(self.view_b.resolve(), (4, 5, 6, 7, 8, 9))
        self.assertEqual(self.view_t.resolve(), (0, 3, 1, 4, 2, 5))

    def test_step1_aggregation_momentum_and_weights(self):
        self.assertTrue(self.coord.add_contribution(self.view_a, GRAD_A, "A"))
        self.assertTrue(self.coord.add_contribution(self.view_b, GRAD_B, "B"))
        self.assertTrue(self.coord.add_contribution(self.view_t, GRAD_T, "T"))

        # 提交前就能观察到重叠位置已相加
        self.assertEqual(
            self.coord.pending_aggregation(), EXPECTED_GRAD_STEP1
        )

        result = self.coord.commit_step(LEARNING_RATE)

        self.assertFalse(result.aborted)
        self.assertEqual(result.step_id, 1)
        self.assertEqual(result.touched_positions, tuple(range(10)))
        self.assertEqual(result.updated_count, 10)
        self.assertEqual(
            result.aggregated_gradients, EXPECTED_GRAD_STEP1
        )
        self.assertEqual(
            result.accepted_contribution_ids, ("A", "B", "T")
        )

        for position, expected in EXPECTED_GRAD_STEP1.items():
            self.assertAlmostEqual(result.momentum_after[position], expected)

        for got, expected in zip(self.coord.momentum, EXPECTED_MOM_STEP1):
            self.assertAlmostEqual(got, expected)
        for got, expected in zip(self.coord.weights, EXPECTED_W_STEP1):
            self.assertAlmostEqual(got, expected)
        # 结果快照与存储实况一致
        self.assertEqual(result.weights_after, self.coord.weights)

    def test_step1_replay_dedup_then_commit(self):
        self.assertTrue(self.coord.add_contribution(self.view_a, GRAD_A, "A"))
        self.assertTrue(self.coord.add_contribution(self.view_b, GRAD_B, "B"))
        self.assertTrue(self.coord.add_contribution(self.view_t, GRAD_T, "T"))

        # 同一步内同 ID 回放（payload 已改）：全部忽略，聚合结果不变
        self.assertFalse(
            self.coord.add_contribution(self.view_a, GRAD_A2, "A")
        )
        self.assertFalse(
            self.coord.add_contribution(
                self.view_b, [[999] * 3, [999] * 3], "B"
            )
        )
        self.assertFalse(
            self.coord.add_contribution(
                self.view_t, [[999, 999]] * 3, "T"
            )
        )
        self.assertEqual(
            self.coord.pending_aggregation(), EXPECTED_GRAD_STEP1
        )

        result = self.coord.commit_step(LEARNING_RATE)
        self.assertFalse(result.aborted)
        self.assertEqual(result.duplicate_contribution_ids, ("A", "B", "T"))
        for got, expected in zip(self.coord.momentum, EXPECTED_MOM_STEP1):
            self.assertAlmostEqual(got, expected)
        for got, expected in zip(self.coord.weights, EXPECTED_W_STEP1):
            self.assertAlmostEqual(got, expected)

    def test_step2_momentum_accumulation(self):
        self.coord.add_contribution(self.view_a, GRAD_A, "A")
        self.coord.add_contribution(self.view_b, GRAD_B, "B")
        self.coord.add_contribution(self.view_t, GRAD_T, "T")
        first = self.coord.commit_step(LEARNING_RATE)
        self.assertFalse(first.aborted)
        self.assertEqual(self.coord.step_id, 2)

        # 新一步的合法贡献（新 ID）：只让位置 4 收到 100，其余位置收到 0
        self.assertTrue(
            self.coord.add_contribution(self.view_a, GRAD_A2, "A2")
        )
        self.assertEqual(
            self.coord.pending_aggregation(),
            {0: 0.0, 1: 0.0, 2: 0.0, 3: 0.0, 4: 100.0, 5: 0.0},
        )

        result = self.coord.commit_step(LEARNING_RATE)
        self.assertFalse(result.aborted)
        self.assertEqual(result.accepted_contribution_ids, ("A2",))
        # 0 梯度位置虽在聚合字典中，但不是“被更新”的物理位置；
        # 动量为 0、权重不变，与未参与等价。touched 以聚合键为准。
        self.assertEqual(result.touched_positions, (0, 1, 2, 3, 4, 5))
        self.assertEqual(
            result.aggregated_gradients,
            {0: 0.0, 1: 0.0, 2: 0.0, 3: 0.0, 4: 100.0, 5: 0.0},
        )

        for got, expected in zip(self.coord.momentum, EXPECTED_MOM_STEP2):
            self.assertAlmostEqual(got, expected)
        for got, expected in zip(self.coord.weights, EXPECTED_W_STEP2):
            self.assertAlmostEqual(got, expected)

    def test_untouched_positions_never_change(self):
        self.coord.add_contribution(self.view_a, GRAD_A, "A")
        self.coord.add_contribution(self.view_b, GRAD_B, "B")
        self.coord.add_contribution(self.view_t, GRAD_T, "T")
        self.coord.commit_step(LEARNING_RATE)
        self.coord.add_contribution(self.view_a, GRAD_A2, "A2")
        self.coord.commit_step(LEARNING_RATE)

        # 位置 10、11 从未参与：权重与动量都保持初值
        self.assertEqual(self.storage[10], 10.0)
        self.assertEqual(self.storage[11], 11.0)
        self.assertEqual(self.coord.momentum[10], 0.0)
        self.assertEqual(self.coord.momentum[11], 0.0)

    def test_gradient_shape_mismatch_does_not_partially_apply(self):
        # 错误形状的贡献：异常 + 本步作废，此前已加入的合法贡献也不落盘
        self.assertTrue(self.coord.add_contribution(self.view_a, GRAD_A, "A"))
        with self.assertRaises(InvalidContributionError):
            self.coord.add_contribution(
                self.view_b, [[1, 2]], "BAD-SHAPE"
            )
        result = self.coord.commit_step(LEARNING_RATE)
        self.assertTrue(result.aborted)
        self.assertEqual(self.storage.buffer, INITIAL_WEIGHTS)
        self.assertEqual(self.coord.momentum, [0.0] * 12)


if __name__ == "__main__":
    unittest.main()
