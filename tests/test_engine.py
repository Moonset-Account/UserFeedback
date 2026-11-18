"""治理裁决引擎的验收测试。

覆盖三类题面要求的场景：
* 可接受结果（proceed）：有效控制扣减后剩余风险在限额内；
* 应被阻止的结果（suspended）：必需资产剩余风险上界超限；
* 边界 / 交错场景：共因去重、独立组相加、封顶到零、限额恰好相等、
  无效回执、跨段覆盖、非必需资产、输入交叉约束。
"""

import unittest

from governance import (
    Asset,
    Control,
    GovernanceInputError,
    Segment,
    ValidationReceipt,
    evaluate_application,
)


def build_case():
    """构造一个可复用的标准夹具。

    资产 A1（必需，限额 30）含两个风险段：
        S1: [40, 60]   S2: [10, 40]   合计 [50, 100]
    控制：
        C1 共因组 G1，S1 消减 20，回执有效
        C2 共因组 G1，S1 消减 35（与 C1 同组重叠）
        C3 独立控制，S1 消减 10，回执有效
        C4 独立控制，S2 消减 25，回执过期（无效）
    """
    assets = [Asset("A1", low=50.0, high=100.0, required=True, go_live_limit=30.0)]
    segments = [
        Segment("S1", "A1", low=40.0, high=60.0),
        Segment("S2", "A1", low=10.0, high=40.0),
    ]
    controls = [
        Control("C1", {"S1": 20.0}, common_cause_group="G1"),
        Control("C2", {"S1": 35.0}, common_cause_group="G1"),
        Control("C3", {"S1": 10.0}),
        Control("C4", {"S2": 25.0}),
    ]
    receipts = [
        ValidationReceipt("R1", "C1", "valid", "演练通过"),
        ValidationReceipt("R2", "C2", "valid"),
        ValidationReceipt("R3", "C3", "valid"),
        ValidationReceipt("R4", "C4", "expired", "回执已过期"),
    ]
    return assets, segments, controls, receipts


class AcceptableDecisionTest(unittest.TestCase):
    """可接受结果：扣减后剩余风险在限额内，裁决 proceed。"""

    def test_proceed_when_residual_within_limit(self):
        assets, segments, controls, receipts = build_case()
        # 让 C4 也有效：S2 消减 25。
        receipts[3] = ValidationReceipt("R4", "C4", "valid")

        decision = evaluate_application(
            assets, segments, controls, receipts, default_go_live_limit=30.0
        )

        self.assertEqual(decision.decision, "proceed")
        self.assertEqual(decision.breaches, ())
        result = decision.asset_results[0]
        self.assertTrue(result.within_limit)
        # S1: 共因组 G1 取 max(20, 35)=35，独立 C3 加 10，共 45；
        #     下界封顶 40 -> 余 0，上界 60-45=15。
        # S2: C4 消减 25 -> [10, 40] - 25 => 下界封顶 10 -> 余 0，上界 15。
        s1, s2 = result.segment_results
        self.assertAlmostEqual(s1.deducted, 45.0)
        self.assertAlmostEqual(s1.residual_low, 0.0)
        self.assertAlmostEqual(s1.residual_high, 15.0)
        self.assertTrue(s1.capped)
        self.assertAlmostEqual(s2.residual_low, 0.0)
        self.assertAlmostEqual(s2.residual_high, 15.0)
        # 资产剩余区间 [0, 30]，恰好不超过限额 30。
        self.assertAlmostEqual(result.residual_low, 0.0)
        self.assertAlmostEqual(result.residual_high, 30.0)
        self.assertEqual(set(result.relied_control_ids), {"C1", "C2", "C3", "C4"})
        self.assertEqual(result.uncovered_segment_ids, ())

    def test_default_limit_applies_to_required_asset(self):
        assets = [Asset("A1", low=10.0, high=10.0, required=True)]
        segments = [Segment("S1", "A1", 5.0, 5.0), Segment("S2", "A1", 5.0, 5.0)]
        controls = [Control("C1", {"S1": 5.0})]
        receipts = [ValidationReceipt("R1", "C1", "valid")]

        decision = evaluate_application(
            assets, segments, controls, receipts, default_go_live_limit=10.0
        )

        self.assertEqual(decision.decision, "proceed")
        result = decision.asset_results[0]
        self.assertEqual(result.go_live_limit, 10.0)
        # S1 被扣完，S2 未被任何控制覆盖。
        self.assertEqual(result.uncovered_segment_ids, ("S2",))
        self.assertAlmostEqual(result.residual_high, 5.0)


class BlockedDecisionTest(unittest.TestCase):
    """应被阻止 / 拒绝的结果。"""

    def test_suspended_when_required_asset_exceeds_limit(self):
        assets, segments, controls, receipts = build_case()

        decision = evaluate_application(
            assets, segments, controls, receipts, default_go_live_limit=30.0
        )

        # C4 无效 -> S2 完全未消减，保留 [10, 40]；
        # S1 扣 45（下界封顶 40）后余 [0, 15]；
        # 资产剩余区间 [10, 55]，上界 55 > 限额 30。
        self.assertEqual(decision.decision, "suspended")
        self.assertEqual(len(decision.breaches), 1)
        breach = decision.breaches[0]
        self.assertEqual(breach.asset_id, "A1")
        self.assertAlmostEqual(breach.limit, 30.0)
        self.assertAlmostEqual(breach.residual_low, 10.0)
        self.assertAlmostEqual(breach.residual_high, 55.0)
        self.assertAlmostEqual(breach.excess, 25.0)
        # 暂停时输出仍依赖的控制依据与未覆盖风险段。
        self.assertEqual(set(breach.relied_control_ids), {"C1", "C2", "C3"})
        self.assertEqual(breach.uncovered_segment_ids, ("S2",))

        result = decision.asset_results[0]
        self.assertFalse(result.within_limit)
        self.assertAlmostEqual(result.residual_low, 10.0)
        self.assertAlmostEqual(result.residual_high, 55.0)

    def test_expired_receipt_control_is_excluded_from_deduction(self):
        # 只有 C4 一张证据且过期：S2 不应有任何扣减。
        assets, segments, controls, receipts = build_case()
        controls = [controls[3]]
        receipts = [receipts[3]]

        decision = evaluate_application(
            assets, segments, controls, receipts, default_go_live_limit=30.0
        )

        outcome = decision.control_outcomes[0]
        self.assertFalse(outcome.valid)
        self.assertIn("expired", outcome.reason)
        s2 = next(
            item for item in decision.asset_results[0].segment_results
            if item.segment_id == "S2"
        )
        self.assertEqual(s2.contributions, ())
        self.assertEqual(s2.covered_control_ids, ())
        self.assertAlmostEqual(s2.residual_low, 10.0)
        self.assertAlmostEqual(s2.residual_high, 40.0)
        self.assertEqual(decision.decision, "suspended")

    def test_missing_receipt_treated_as_invalid_evidence(self):
        assets = [Asset("A1", low=10.0, high=10.0, required=True, go_live_limit=5.0)]
        segments = [Segment("S1", "A1", 10.0, 10.0)]
        controls = [Control("C1", {"S1": 10.0})]

        decision = evaluate_application(assets, segments, controls, [])

        outcome = decision.control_outcomes[0]
        self.assertFalse(outcome.valid)
        self.assertIn("缺少验证回执", outcome.reason)
        self.assertEqual(decision.decision, "suspended")
        self.assertAlmostEqual(
            decision.asset_results[0].residual_high, 10.0
        )

    def test_required_asset_without_any_limit_is_rejected(self):
        assets = [Asset("A1", low=10.0, high=10.0, required=True)]
        segments = [Segment("S1", "A1", 10.0, 10.0)]
        controls = [Control("C1", {"S1": 10.0})]
        receipts = [ValidationReceipt("R1", "C1", "valid")]

        with self.assertRaises(GovernanceInputError) as ctx:
            evaluate_application(assets, segments, controls, receipts)
        self.assertIn("上线限额", str(ctx.exception))


class CommonCauseDedupTest(unittest.TestCase):
    """共因组重叠范围只按有效的最高消减量计算，不得重复扣减。"""

    def test_same_common_cause_group_uses_max_not_sum(self):
        assets, segments, controls, receipts = build_case()
        # 只保留同属 G1 的 C1(20) 与 C2(35)，均有效。
        controls = [controls[0], controls[1]]
        receipts = [receipts[0], receipts[1]]

        decision = evaluate_application(
            assets, segments, controls, receipts, default_go_live_limit=100.0
        )

        s1 = decision.asset_results[0].segment_results[0]
        # 不是 20+35=55，而是 max=35。
        self.assertEqual(len(s1.contributions), 1)
        contribution = s1.contributions[0]
        self.assertAlmostEqual(contribution.deducted, 35.0)
        self.assertEqual(contribution.selected_control_id, "C2")
        self.assertEqual(
            set(contribution.candidate_control_ids), {"C1", "C2"}
        )
        self.assertAlmostEqual(s1.deducted, 35.0)
        self.assertAlmostEqual(s1.residual_low, 5.0)
        self.assertAlmostEqual(s1.residual_high, 25.0)

    def test_invalid_control_ignored_even_if_it_has_largest_mitigation(self):
        # C2 消减最高但其回执失效时，共因组应回退到 C1 的 20。
        assets, segments, controls, receipts = build_case()
        controls = [controls[0], controls[1]]
        receipts = [
            receipts[0],
            ValidationReceipt("R2", "C2", "revoked", "被撤销"),
        ]

        decision = evaluate_application(
            assets, segments, controls, receipts, default_go_live_limit=100.0
        )

        s1 = decision.asset_results[0].segment_results[0]
        self.assertAlmostEqual(s1.deducted, 20.0)
        self.assertEqual(s1.contributions[0].selected_control_id, "C1")
        self.assertFalse(decision.control_outcomes[1].valid)

    def test_tie_breaks_to_smallest_control_id_for_reproducibility(self):
        assets = [Asset("A1", low=50.0, high=50.0, required=True, go_live_limit=50.0)]
        segments = [Segment("S1", "A1", 50.0, 50.0)]
        controls = [
            Control("CZ", {"S1": 30.0}, common_cause_group="G1"),
            Control("CA", {"S1": 30.0}, common_cause_group="G1"),
        ]
        receipts = [
            ValidationReceipt("R1", "CZ", "valid"),
            ValidationReceipt("R2", "CA", "valid"),
        ]

        decision = evaluate_application(assets, segments, controls, receipts)

        contribution = decision.asset_results[0].segment_results[0].contributions[0]
        self.assertAlmostEqual(contribution.deducted, 30.0)
        self.assertEqual(contribution.selected_control_id, "CA")

    def test_independent_groups_are_added_together(self):
        assets = [Asset("A1", low=50.0, high=50.0, required=True, go_live_limit=50.0)]
        segments = [Segment("S1", "A1", 50.0, 50.0)]
        controls = [
            Control("C1", {"S1": 12.0}),  # 无共因组 -> 独立
            Control("C2", {"S1": 8.0}),   # 独立
        ]
        receipts = [
            ValidationReceipt("R1", "C1", "valid"),
            ValidationReceipt("R2", "C2", "valid"),
        ]

        decision = evaluate_application(assets, segments, controls, receipts)

        s1 = decision.asset_results[0].segment_results[0]
        self.assertEqual(len(s1.contributions), 2)
        self.assertAlmostEqual(s1.deducted, 20.0)
        self.assertAlmostEqual(s1.residual_high, 30.0)

    def test_common_cause_group_is_scoped_per_segment(self):
        # 同一共因组覆盖不同风险段时，各段分别取 max（交错场景）。
        assets = [Asset("A1", low=40.0, high=40.0, required=True, go_live_limit=40.0)]
        segments = [
            Segment("S1", "A1", 20.0, 20.0),
            Segment("S2", "A1", 20.0, 20.0),
        ]
        controls = [
            Control("C1", {"S1": 15.0, "S2": 5.0}, common_cause_group="G1"),
            Control("C2", {"S1": 5.0, "S2": 12.0}, common_cause_group="G1"),
        ]
        receipts = [
            ValidationReceipt("R1", "C1", "valid"),
            ValidationReceipt("R2", "C2", "valid"),
        ]

        decision = evaluate_application(assets, segments, controls, receipts)

        s1, s2 = decision.asset_results[0].segment_results
        # S1 取 C1 的 15，S2 取 C2 的 12；两段都只有一个共因组贡献。
        self.assertAlmostEqual(s1.deducted, 15.0)
        self.assertEqual(s1.contributions[0].selected_control_id, "C1")
        self.assertAlmostEqual(s2.deducted, 12.0)
        self.assertEqual(s2.contributions[0].selected_control_id, "C2")
        self.assertAlmostEqual(
            decision.asset_results[0].residual_high, 40.0 - 15.0 - 12.0
        )


class BoundaryAndInterleavingTest(unittest.TestCase):
    """边界与交错：封顶到零、限额恰好相等、跨段覆盖、非必需资产。"""

    def test_deduction_never_exceeds_initial_segment_risk(self):
        assets = [Asset("A1", low=10.0, high=30.0, required=True, go_live_limit=30.0)]
        segments = [Segment("S1", "A1", 10.0, 30.0)]
        # 两个独立控制合计消减 100，远超段初始风险。
        controls = [
            Control("C1", {"S1": 60.0}),
            Control("C2", {"S1": 40.0}),
        ]
        receipts = [
            ValidationReceipt("R1", "C1", "valid"),
            ValidationReceipt("R2", "C2", "valid"),
        ]

        decision = evaluate_application(assets, segments, controls, receipts)

        s1 = decision.asset_results[0].segment_results[0]
        self.assertTrue(s1.capped)
        # 下界、上界分别封顶，剩余不为负。
        self.assertAlmostEqual(s1.residual_low, 0.0)
        self.assertAlmostEqual(s1.residual_high, 0.0)
        self.assertAlmostEqual(
            decision.asset_results[0].residual_high, 0.0
        )
        self.assertEqual(decision.decision, "proceed")

    def test_residual_equal_to_limit_is_allowed(self):
        # 边界：剩余上界恰好等于限额 -> 通过（<= 比较）。
        assets = [Asset("A1", low=20.0, high=20.0, required=True, go_live_limit=12.0)]
        segments = [Segment("S1", "A1", 20.0, 20.0)]
        controls = [Control("C1", {"S1": 8.0})]
        receipts = [ValidationReceipt("R1", "C1", "valid")]

        decision = evaluate_application(assets, segments, controls, receipts)

        result = decision.asset_results[0]
        self.assertAlmostEqual(result.residual_high, 12.0)
        self.assertTrue(result.within_limit)
        self.assertEqual(decision.decision, "proceed")

    def test_one_unit_over_limit_suspends(self):
        assets = [Asset("A1", low=20.0, high=20.0, required=True, go_live_limit=11.999)]
        segments = [Segment("S1", "A1", 20.0, 20.0)]
        controls = [Control("C1", {"S1": 8.0})]
        receipts = [ValidationReceipt("R1", "C1", "valid")]

        decision = evaluate_application(assets, segments, controls, receipts)

        self.assertEqual(decision.decision, "suspended")
        self.assertAlmostEqual(decision.breaches[0].excess, 20.0 - 8.0 - 11.999)

    def test_control_spanning_multiple_segments_deducts_each_independently(self):
        assets = [Asset("A1", low=30.0, high=70.0, required=True, go_live_limit=70.0)]
        segments = [
            Segment("S1", "A1", 10.0, 30.0),
            Segment("S2", "A1", 20.0, 40.0),
        ]
        controls = [Control("C1", {"S1": 10.0, "S2": 15.0})]
        receipts = [ValidationReceipt("R1", "C1", "valid")]

        decision = evaluate_application(assets, segments, controls, receipts)

        s1, s2 = decision.asset_results[0].segment_results
        self.assertAlmostEqual(s1.residual_low, 0.0)
        self.assertAlmostEqual(s1.residual_high, 20.0)
        self.assertAlmostEqual(s2.residual_low, 5.0)
        self.assertAlmostEqual(s2.residual_high, 25.0)
        self.assertAlmostEqual(
            decision.asset_results[0].residual_high, 45.0
        )

    def test_non_required_asset_does_not_trigger_suspension(self):
        assets = [
            Asset("A1", low=100.0, high=100.0, required=False),
        ]
        segments = [Segment("S1", "A1", 100.0, 100.0)]

        decision = evaluate_application(
            assets, segments, [], [], default_go_live_limit=10.0
        )

        result = decision.asset_results[0]
        self.assertIsNone(result.within_limit)
        self.assertEqual(decision.decision, "proceed")
        self.assertEqual(decision.breaches, ())

    def test_multiple_assets_one_breach_suspends_whole_application(self):
        assets = [
            Asset("A1", low=10.0, high=10.0, required=True, go_live_limit=10.0),
            Asset("A2", low=10.0, high=10.0, required=True, go_live_limit=5.0),
        ]
        segments = [
            Segment("S1", "A1", 10.0, 10.0),
            Segment("S2", "A2", 10.0, 10.0),
        ]
        controls = [Control("C1", {"S1": 10.0})]
        receipts = [ValidationReceipt("R1", "C1", "valid")]

        decision = evaluate_application(assets, segments, controls, receipts)

        self.assertEqual(decision.decision, "suspended")
        self.assertEqual(len(decision.breaches), 1)
        self.assertEqual(decision.breaches[0].asset_id, "A2")
        by_asset = {item.asset_id: item for item in decision.asset_results}
        self.assertTrue(by_asset["A1"].within_limit)
        self.assertFalse(by_asset["A2"].within_limit)


class InputValidationTest(unittest.TestCase):
    """题面交叉约束的输入侧拒绝路径。"""

    def test_segment_sum_mismatch_is_rejected(self):
        assets = [Asset("A1", low=10.0, high=10.0, required=True, go_live_limit=10.0)]
        segments = [
            Segment("S1", "A1", 4.0, 4.0),
            Segment("S2", "A1", 4.0, 4.0),  # 合计 8 != 10
        ]
        with self.assertRaises(GovernanceInputError) as ctx:
            evaluate_application(assets, segments, [], [])
        self.assertIn("不一致", str(ctx.exception))

    def test_control_covering_unknown_segment_is_rejected(self):
        assets = [Asset("A1", low=10.0, high=10.0, required=True, go_live_limit=10.0)]
        segments = [Segment("S1", "A1", 10.0, 10.0)]
        controls = [Control("C1", {"S404": 5.0})]
        receipts = [ValidationReceipt("R1", "C1", "valid")]
        with self.assertRaises(GovernanceInputError) as ctx:
            evaluate_application(assets, segments, controls, receipts)
        self.assertIn("不存在的风险段", str(ctx.exception))

    def test_control_cannot_span_segments_of_different_assets(self):
        assets = [
            Asset("A1", low=10.0, high=10.0, required=True, go_live_limit=10.0),
            Asset("A2", low=10.0, high=10.0, required=True, go_live_limit=10.0),
        ]
        segments = [
            Segment("S1", "A1", 10.0, 10.0),
            Segment("S2", "A2", 10.0, 10.0),
        ]
        controls = [Control("C1", {"S1": 5.0, "S2": 5.0})]
        receipts = [ValidationReceipt("R1", "C1", "valid")]
        with self.assertRaises(GovernanceInputError) as ctx:
            evaluate_application(assets, segments, controls, receipts)
        self.assertIn("跨资产", str(ctx.exception))

    def test_duplicate_receipts_for_one_control_are_rejected(self):
        assets = [Asset("A1", low=10.0, high=10.0, required=True, go_live_limit=10.0)]
        segments = [Segment("S1", "A1", 10.0, 10.0)]
        controls = [Control("C1", {"S1": 5.0})]
        receipts = [
            ValidationReceipt("R1", "C1", "valid"),
            ValidationReceipt("R2", "C1", "valid"),
        ]
        with self.assertRaises(GovernanceInputError) as ctx:
            evaluate_application(assets, segments, controls, receipts)
        self.assertIn("多张验证回执", str(ctx.exception))

    def test_receipt_referencing_unknown_control_is_rejected(self):
        assets = [Asset("A1", low=10.0, high=10.0, required=True, go_live_limit=10.0)]
        segments = [Segment("S1", "A1", 10.0, 10.0)]
        receipts = [ValidationReceipt("R1", "C404", "valid")]
        with self.assertRaises(GovernanceInputError) as ctx:
            evaluate_application(assets, segments, [], receipts)
        self.assertIn("不存在的控制项", str(ctx.exception))

    def test_asset_without_segment_is_rejected(self):
        assets = [Asset("A1", low=10.0, high=10.0, required=True, go_live_limit=10.0)]
        with self.assertRaises(GovernanceInputError) as ctx:
            evaluate_application(assets, [], [], [])
        self.assertIn("风险段", str(ctx.exception))

    def test_duplicate_identifiers_are_rejected(self):
        assets = [
            Asset("A1", low=10.0, high=10.0, required=True, go_live_limit=10.0),
        ]
        segments = [
            Segment("S1", "A1", 5.0, 5.0),
            Segment("S1", "A1", 5.0, 5.0),
        ]
        with self.assertRaises(GovernanceInputError) as ctx:
            evaluate_application(assets, segments, [], [])
        self.assertIn("风险段标识重复", str(ctx.exception))

    def test_negative_mitigation_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            Control("C1", {"S1": -1.0})

    def test_inverted_interval_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            Segment("S1", "A1", low=9.0, high=3.0)


class SerializationTest(unittest.TestCase):
    """裁决结果可 JSON 序列化，供下游系统留档。"""

    def test_to_dict_is_json_friendly(self):
        import json

        assets, segments, controls, receipts = build_case()
        decision = evaluate_application(
            assets, segments, controls, receipts, default_go_live_limit=30.0
        )
        encoded = json.dumps(decision.to_dict(), ensure_ascii=False)
        decoded = json.loads(encoded)

        self.assertEqual(decoded["decision"], "suspended")
        self.assertEqual(decoded["breaches"][0]["asset_id"], "A1")
        self.assertIsInstance(
            decoded["asset_results"][0]["segment_results"], list
        )
        self.assertIsInstance(
            decoded["asset_results"][0]["relied_control_ids"], list
        )


if __name__ == "__main__":
    unittest.main()
