"""clock_calibration 的单元测试。

从工作区根目录运行：
    python3 -m unittest discover -s tests -v
"""

import unittest

from clock_calibration import (
    CalibrationResult,
    HostBounds,
    Observation,
    calibrate,
    verify_conflict,
)


class TestBasicBounds(unittest.TestCase):
    """正常场景：单条观测与基准自身。"""

    def test_single_observation_bounds(self):
        # c = 105 - 100 = 5；o_B - o_A ∈ [1-5, 10-5] = [-4, 5]
        obs = [Observation("A", "B", send_ts=100, recv_ts=105,
                           min_delay=1, max_delay=10)]
        res = calibrate(obs, reference="A")
        self.assertEqual(res.bounds["A"], HostBounds(0, 0))
        self.assertEqual(res.bounds["B"], HostBounds(-4, 5))
        self.assertEqual(res.unbounded, frozenset())
        self.assertEqual(res.conflicted, frozenset())
        self.assertEqual(res.conflicts, ())

    def test_exact_delay_pins_offset(self):
        # 耗时区间退化为点：偏移被唯一确定
        obs = [Observation("A", "B", send_ts=0, recv_ts=7,
                           min_delay=7, max_delay=7)]
        res = calibrate(obs, reference="A")
        self.assertEqual(res.bounds["B"], HostBounds(0, 0))

    def test_receiver_is_reference(self):
        # 观测方向指向基准：o_A - o_B ∈ [2, 4]，o_A = 0 → o_B ∈ [-4, -2]
        obs = [Observation("B", "A", send_ts=10, recv_ts=10,
                           min_delay=2, max_delay=4)]
        res = calibrate(obs, reference="A")
        self.assertEqual(res.bounds["B"], HostBounds(-4, -2))

    def test_repeated_observations_intersect(self):
        # 同一对主机的多条观测取交
        obs = [
            Observation("A", "B", 0, 0, 0, 100),
            Observation("A", "B", 0, 0, 3, 7),
        ]
        res = calibrate(obs, reference="A")
        self.assertEqual(res.bounds["B"], HostBounds(3, 7))


class TestTransitiveTightening(unittest.TestCase):
    """正常场景：经第三台主机收紧的界反映在最短路结果里。"""

    def setUp(self):
        # A→B: c=5, o_B ∈ [-4, 5]
        # B→C: c=3, o_C - o_B ∈ [-1, 1]
        # A→C 直接观测很宽: c=100, o_C ∈ [-100, 900]
        self.obs = [
            Observation("A", "B", send_ts=100, recv_ts=105,
                        min_delay=1, max_delay=10),
            Observation("B", "C", send_ts=200, recv_ts=203,
                        min_delay=2, max_delay=4),
            Observation("A", "C", send_ts=1000, recv_ts=1100,
                        min_delay=0, max_delay=1000),
        ]
        self.res = calibrate(self.obs, reference="A")

    def test_chain_tightens_beyond_wide_direct_observation(self):
        # 经 B 中转：o_C ∈ [-4-1, 5+1] = [-5, 6]，远紧于直接观测的 [-100, 900]
        self.assertEqual(self.res.bounds["C"], HostBounds(-5, 6))

    def test_intermediate_host_bounds(self):
        self.assertEqual(self.res.bounds["B"], HostBounds(-4, 5))

    def test_no_false_conflicts(self):
        self.assertEqual(self.res.conflicts, ())
        self.assertEqual(self.res.conflicted, frozenset())


class TestEdgeCases(unittest.TestCase):
    """边界场景：空观测、孤立主机、自环、非法输入。"""

    def test_empty_observations(self):
        res = calibrate([], reference="A")
        self.assertEqual(res.bounds, {"A": HostBounds(0, 0)})
        self.assertEqual(res.unbounded, frozenset())
        self.assertEqual(res.conflicts, ())

    def test_explicit_host_without_observations_is_unbounded(self):
        res = calibrate([], reference="A", hosts=["Z"])
        self.assertEqual(res.bounds, {"A": HostBounds(0, 0)})
        self.assertEqual(res.unbounded, frozenset({"Z"}))

    def test_consistent_self_loop_keeps_bounds(self):
        # 自环：o_B - o_B = 0 须落在 [min_delay-c, max_delay-c] = [-1, 3]，自洽
        obs = [
            Observation("A", "B", 0, 0, 0, 10),
            Observation("B", "B", 10, 12, 1, 5),
        ]
        res = calibrate(obs, reference="A")
        self.assertEqual(res.bounds["B"], HostBounds(0, 10))
        self.assertEqual(res.conflicts, ())

    def test_contradictory_self_loop_reported(self):
        # 自环要求 0 ∈ [1, 5]，单独一条即矛盾
        obs = [Observation("X", "X", 0, 0, 1, 5)]
        res = calibrate(obs, reference="A")
        self.assertEqual(res.bounds, {"A": HostBounds(0, 0)})
        self.assertEqual(res.conflicted, frozenset({"X"}))
        self.assertEqual(res.conflicts, ((0,),))
        self.assertTrue(verify_conflict(obs, res.conflicts[0]))

    def test_invalid_delay_range_raises(self):
        with self.assertRaises(ValueError):
            Observation("A", "B", 0, 0, min_delay=5, max_delay=3)

    def test_verify_conflict_rejects_out_of_range_index(self):
        obs = [Observation("A", "B", 0, 0, 0, 1)]
        with self.assertRaises(IndexError):
            verify_conflict(obs, [99])

    def test_verify_conflict_empty_subset_is_false(self):
        obs = [Observation("A", "B", 0, 0, 0, 1)]
        self.assertFalse(verify_conflict(obs, []))


class TestInterleavedComponents(unittest.TestCase):
    """交错场景：基准分量、自洽无界分量、矛盾分量混合，观测编号对应原始下标。"""

    def setUp(self):
        self.obs = [
            # 0: 基准分量 A→B，c=5，o_B ∈ [-4, 5]
            Observation("A", "B", 100, 105, 1, 10),
            # 1: 分量 {P,Q}：o_Q - o_P ∈ [5, 10]
            Observation("P", "Q", 0, 0, 5, 10),
            # 2: 基准分量 B→C，c=3，o_C - o_B ∈ [-1, 1]
            Observation("B", "C", 200, 203, 2, 4),
            # 3: 分量 {P,Q}：o_P - o_Q ∈ [5, 10]，与 1 矛盾
            Observation("Q", "P", 0, 0, 5, 10),
            # 4: 分量 {M,N}：自洽但与基准不连通
            Observation("M", "N", 0, 0, 0, 100),
        ]
        self.res = calibrate(self.obs, reference="A")

    def test_reference_component_bounds(self):
        self.assertEqual(self.res.bounds["A"], HostBounds(0, 0))
        self.assertEqual(self.res.bounds["B"], HostBounds(-4, 5))
        self.assertEqual(self.res.bounds["C"], HostBounds(-5, 6))

    def test_consistent_disconnected_component_is_unbounded(self):
        self.assertEqual(self.res.unbounded, frozenset({"M", "N"}))
        self.assertNotIn("M", self.res.bounds)
        self.assertNotIn("N", self.res.bounds)

    def test_contradictory_component_witness(self):
        self.assertEqual(self.res.conflicted, frozenset({"P", "Q"}))
        self.assertNotIn("P", self.res.bounds)
        self.assertNotIn("P", self.res.unbounded)
        # 证物必须指向原始下标 1 和 3（顺序交错也不应错位）
        self.assertEqual(self.res.conflicts, ((1, 3),))

    def test_witness_is_verifiable(self):
        for conflict in self.res.conflicts:
            self.assertTrue(verify_conflict(self.obs, conflict))

    def test_consistent_subset_not_a_conflict(self):
        self.assertFalse(verify_conflict(self.obs, [0, 2]))
        self.assertFalse(verify_conflict(self.obs, [1]))


class TestConflictInReferenceComponent(unittest.TestCase):
    """边界场景：基准所在分量自身矛盾。"""

    def test_reference_component_conflict(self):
        obs = [
            Observation("A", "B", 0, 0, 5, 10),   # o_B ∈ [5, 10]
            Observation("B", "A", 0, 0, 5, 10),   # o_B ≤ -5，矛盾
        ]
        res = calibrate(obs, reference="A")
        self.assertEqual(res.bounds, {})
        self.assertEqual(res.conflicted, frozenset({"A", "B"}))
        self.assertEqual(len(res.conflicts), 1)
        self.assertEqual(res.conflicts[0], (0, 1))
        self.assertTrue(verify_conflict(obs, res.conflicts[0]))


class TestResultStructure(unittest.TestCase):
    """结果对象的公开属性。"""

    def test_result_attributes(self):
        obs = [Observation("A", "B", 0, 0, 0, 5)]
        res = calibrate(obs, reference="A")
        self.assertIsInstance(res, CalibrationResult)
        self.assertEqual(res.reference, "A")
        self.assertIsInstance(res.bounds["B"], HostBounds)
        self.assertIsInstance(res.unbounded, frozenset)
        self.assertIsInstance(res.conflicted, frozenset)
        self.assertIsInstance(res.conflicts, tuple)


if __name__ == "__main__":
    unittest.main()
