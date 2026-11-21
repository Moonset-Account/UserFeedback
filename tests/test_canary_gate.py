"""灰度闸门（canary_gate）的 unittest 测试。

从工作区根目录运行：
    python3 -m unittest discover -v
"""

import unittest
from fractions import Fraction

from canary_gate import (
    CanaryGate,
    Decision,
    GateClosedError,
    Receipt,
    RegionGroups,
    UnknownRequestError,
)


def make_gate(groups, weights, threshold):
    return CanaryGate(groups, weights, threshold)


class TestEarlyRollbackSlowRegion(unittest.TestCase):
    """正常场景：小流量地区权重高，未回齐时已能确定回滚。"""

    def setUp(self):
        self.gate = make_gate(
            {
                "small": (["s-n1", "s-n2"], ["s-b1", "s-b2"]),
                "big": (["g-n1"], ["g-b1"]),
            },
            {"small": Fraction(3, 4), "big": Fraction(1, 4)},
            Fraction(1, 10),
        )

    def test_rollback_before_all_receipts(self):
        # big 地区完全没有回报，但 small 地区新版全败、基线全胜，
        # 下界已被抬高到阈值之上。
        report = self.gate.submit_batch(
            [
                Receipt("s-n1", False),
                Receipt("s-n2", False),
                Receipt("s-b1", True),
                Receipt("s-b2", True),
            ]
        )
        self.assertEqual(report.decision, Decision.ROLLBACK)
        self.assertEqual(report.batch_index, 1)
        # small 差值 = 1，big 下界 = -1 → lower = 3/4 - 1/4 = 1/2
        self.assertEqual(report.lower, Fraction(1, 2))
        self.assertEqual(report.upper, Fraction(1))
        self.assertEqual(report.triggered_bound, Fraction(1, 2))

        final = self.gate.final_report
        self.assertIsNotNone(final)
        self.assertEqual(final.batch_index, 1)
        self.assertEqual(final.triggered_bound, Fraction(1, 2))

    def test_gate_closed_after_decision(self):
        self.test_rollback_before_all_receipts()
        with self.assertRaises(GateClosedError):
            self.gate.submit_batch([Receipt("g-n1", True)])
        # 终局报告不受后续调用影响
        self.assertEqual(self.gate.final_report.decision, Decision.ROLLBACK)


class TestRampUpBoundary(unittest.TestCase):
    """边界场景：上界恰好等于阈值即放量（不超过即可）。"""

    def test_upper_equal_threshold_ramps_up(self):
        gate = make_gate(
            {"r": (["n1", "n2"], ["b1"])},
            {"r": Fraction(1)},
            Fraction(-1, 2),
        )
        # n2 未回报：upper = (0+1)/2 - 1/1 = -1/2 == 阈值
        report = gate.submit_batch([("n1", True), ("b1", False)])
        self.assertEqual(report.decision, Decision.RAMP_UP)
        self.assertEqual(report.triggered_bound, Fraction(-1, 2))
        self.assertEqual(report.upper, Fraction(-1, 2))
        self.assertEqual(report.lower, Fraction(-1))

    def test_upper_above_threshold_stays_pending(self):
        gate = make_gate(
            {"r": (["n1", "n2"], ["b1"])},
            {"r": Fraction(1)},
            Fraction(-3, 5),  # -3/5 < upper(-1/2)
        )
        report = gate.submit_batch([("n1", True), ("b1", False)])
        self.assertEqual(report.decision, Decision.PENDING)
        self.assertIsNone(report.triggered_bound)
        # 补齐后上界降到阈值以下才放量
        report2 = gate.submit_batch([("n2", True)])
        self.assertEqual(report2.decision, Decision.RAMP_UP)
        self.assertEqual(report2.batch_index, 2)
        self.assertEqual(report2.triggered_bound, Fraction(-1))


class TestLowerEqualThresholdNotRollback(unittest.TestCase):
    """边界场景：下界等于阈值不算"高于"，不触发回滚。"""

    def test_lower_equal_threshold_pending_then_ramp(self):
        gate = make_gate(
            {"r": (["n1", "n2"], ["b1"])},
            {"r": Fraction(1)},
            Fraction(1, 2),
        )
        # lower = 1/2 == 阈值，upper = 1 > 阈值 → 继续等
        report = gate.submit_batch([("n1", False), ("b1", True)])
        self.assertEqual(report.decision, Decision.PENDING)
        self.assertEqual(report.lower, Fraction(1, 2))
        self.assertEqual(report.upper, Fraction(1))

        report2 = gate.submit_batch([("n2", True)])
        self.assertEqual(report2.decision, Decision.RAMP_UP)
        self.assertEqual(report2.triggered_bound, Fraction(1, 2))


class TestDuplicateReceipts(unittest.TestCase):
    """交错场景：同批与跨批重复回执只计一次，冲突时以首次为准。"""

    def test_duplicates_counted_once(self):
        gate = make_gate(
            {"r": (["n1"], ["b1"])},
            {"r": Fraction(1)},
            Fraction(0),
        )
        # 同批重复：n1 只记一次失败
        r1 = gate.submit_batch(
            [("n1", False), ("n1", False), ("n1", True)]
        )
        self.assertEqual(r1.decision, Decision.PENDING)
        self.assertEqual(r1.lower, Fraction(0))  # 1 - (0+1)
        self.assertEqual(r1.upper, Fraction(1))

        # 跨批重复且结果冲突：首次结果（失败）保留
        r2 = gate.submit_batch([("n1", True), ("b1", True)])
        self.assertEqual(r2.decision, Decision.ROLLBACK)
        self.assertEqual(r2.batch_index, 2)
        self.assertEqual(r2.triggered_bound, Fraction(1))  # 1 - 0


class TestEarlyRampUp(unittest.TestCase):
    """正常场景：未回齐时上界已不超过阈值，提前放量。"""

    def test_ramp_up_with_outstanding_request(self):
        gate = make_gate(
            {
                "a": (["na1"], ["ba1"]),
                "b": (["nb1", "nb2"], ["bb1"]),
            },
            {"a": Fraction(1, 2), "b": Fraction(1, 2)},
            Fraction(1, 4),
        )
        # nb2 未回报；b 地区上界 = 1/2，加权后 upper = 1/4 <= 阈值
        report = gate.submit_batch(
            [("na1", True), ("ba1", True), ("nb1", True), ("bb1", True)]
        )
        self.assertEqual(report.decision, Decision.RAMP_UP)
        self.assertEqual(report.batch_index, 1)
        self.assertEqual(report.lower, Fraction(0))
        self.assertEqual(report.upper, Fraction(1, 4))
        self.assertEqual(report.triggered_bound, Fraction(1, 4))


class TestInterleavedBatches(unittest.TestCase):
    """交错场景：空批次、重复回执与新回执混杂，批次号按提交递增。"""

    def test_batch_numbering_and_mixed_receipts(self):
        gate = make_gate(
            {"r": (["n1"], ["b1"])},
            {"r": Fraction(1)},
            Fraction(0),
        )
        r1 = gate.submit_batch([])  # 空批次也占批次号
        self.assertEqual(r1.decision, Decision.PENDING)
        self.assertEqual(r1.batch_index, 1)
        self.assertEqual((r1.lower, r1.upper), (Fraction(-1), Fraction(1)))

        r2 = gate.submit_batch([("n1", False), ("n1", False)])
        self.assertEqual(r2.decision, Decision.PENDING)
        self.assertEqual(r2.batch_index, 2)

        r3 = gate.submit_batch([("b1", True), ("n1", True)])  # n1 为重复
        self.assertEqual(r3.decision, Decision.ROLLBACK)
        self.assertEqual(r3.batch_index, 3)
        self.assertEqual(r3.triggered_bound, Fraction(1))
        self.assertEqual(gate.final_report.batch_index, 3)


class TestExactRationalArithmetic(unittest.TestCase):
    """边界场景：1/3、2/3 权重下界值保持精确有理数。"""

    def test_fraction_exactness(self):
        gate = make_gate(
            {
                "x": (["xn1"], ["xb1"]),
                "y": (["yn1"], ["yb1"]),
            },
            {"x": Fraction(1, 3), "y": Fraction(2, 3)},
            0,
        )
        report = gate.submit_batch(
            [
                ("xn1", False),
                ("xb1", True),
                ("yn1", True),
                ("yb1", False),
            ]
        )
        # S = 1/3 * 1 + 2/3 * (-1) = -1/3，两地区均已回齐
        self.assertEqual(report.decision, Decision.RAMP_UP)
        self.assertIsInstance(report.lower, Fraction)
        self.assertEqual(report.lower, Fraction(-1, 3))
        self.assertEqual(report.upper, Fraction(-1, 3))
        self.assertEqual(report.triggered_bound, Fraction(-1, 3))


class TestUnknownRequest(unittest.TestCase):
    """边界场景：未冻结的请求报错，且整批作废、不消耗批次号。"""

    def test_unknown_request_rejected_atomically(self):
        gate = make_gate(
            {"r": (["n1"], ["b1"])},
            {"r": Fraction(1)},
            Fraction(0),
        )
        before = gate.bounds()
        with self.assertRaises(UnknownRequestError):
            gate.submit_batch([("n1", False), ("ghost", True)])
        self.assertEqual(gate.bounds(), before)  # 状态未变

        report = gate.submit_batch([("n1", False)])
        self.assertEqual(report.batch_index, 1)  # 失败批次未占号


class TestValidation(unittest.TestCase):
    """构造参数校验。"""

    def test_weights_must_sum_to_one(self):
        with self.assertRaises(ValueError):
            make_gate(
                {"a": (["n1"], ["b1"]), "b": (["n2"], ["b2"])},
                {"a": Fraction(1, 2), "b": Fraction(1, 3)},
                0,
            )

    def test_weights_must_be_positive(self):
        for bad in (Fraction(0), Fraction(-1, 2)):
            with self.assertRaises(ValueError):
                make_gate(
                    {"a": (["n1"], ["b1"]), "b": (["n2"], ["b2"])},
                    {"a": bad, "b": 1 - bad},
                    0,
                )

    def test_weight_keys_must_match_regions(self):
        with self.assertRaises(ValueError):
            make_gate(
                {"a": (["n1"], ["b1"])},
                {"a": Fraction(1), "b": Fraction(0)},
                0,
            )

    def test_empty_group_rejected(self):
        with self.assertRaises(ValueError):
            RegionGroups([], ["b1"])
        with self.assertRaises(ValueError):
            RegionGroups(["n1"], [])

    def test_overlapping_groups_rejected(self):
        with self.assertRaises(ValueError):
            RegionGroups(["n1"], ["n1"])

    def test_request_frozen_in_two_regions_rejected(self):
        with self.assertRaises(ValueError):
            make_gate(
                {"a": (["n1"], ["b1"]), "b": (["n1"], ["b2"])},
                {"a": Fraction(1, 2), "b": Fraction(1, 2)},
                0,
            )

    def test_string_and_int_rationals_accepted(self):
        gate = make_gate(
            {"a": (["n1"], ["b1"]), "b": (["n2"], ["b2"])},
            {"a": "0.5", "b": "0.5"},
            0,
        )
        self.assertEqual(gate.weights["a"], Fraction(1, 2))
        self.assertEqual(gate.threshold, Fraction(0))

    def test_region_groups_dataclass_accepted(self):
        gate = make_gate(
            {"r": RegionGroups(["n1"], ["b1"])},
            {"r": Fraction(1)},
            Fraction(0),
        )
        report = gate.submit_batch([("n1", True), ("b1", True)])
        self.assertEqual(report.decision, Decision.RAMP_UP)
        self.assertEqual(report.triggered_bound, Fraction(0))


if __name__ == "__main__":
    unittest.main()
