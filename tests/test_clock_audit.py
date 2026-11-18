"""clock_audit 验收测试（unittest）。

覆盖题面交叉约束：
- 可接受结果：同一时钟域、单位统一后期限比较有效；
- 应被阻止的结果：跨时钟域求差、无证明的旧进程单调期限参与比较；
- 边界/交错场景：到期临界点 diff == 0、单位不同但可统一、
  单侧有效时钟时仍给出另一侧证据路径、同一数据流中多种结论交错出现。
"""

import unittest

from clock_audit import (
    VERDICT_ACCEPT,
    VERDICT_PENDING,
    VERDICT_REJECT,
    Auditor,
    BaselineProof,
    ClockRead,
    Compare,
    DeadlineCompute,
    Restore,
    Save,
)


def read(eid, source, value, unit, generation):
    return ClockRead(
        event_id=eid, source=source, value=value, unit=unit, generation=generation
    )


class TestAcceptableFlows(unittest.TestCase):
    """可接受结果：同域、单位统一、代次规则满足。"""

    def test_same_domain_same_unit_accepted(self):
        auditor = Auditor()
        findings = auditor.ingest([
            read("r1", "monotonic", 1000, "ms", 1),
            DeadlineCompute("d1", base="r1", duration=500, duration_unit="ms"),
            read("r2", "monotonic", 1200, "ms", 1),
            Compare("c1", deadline="d1", now="r2"),
        ])
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f.verdict, VERDICT_ACCEPT)
        self.assertEqual(f.diff_ns, 300_000_000)  # 1500ms - 1200ms
        self.assertFalse(f.expired)

    def test_mixed_units_are_unified(self):
        """单位不同（ms vs s）但可统一换算时，比较可接受且差值正确。"""
        auditor = Auditor()
        findings = auditor.ingest([
            read("r1", "monotonic", 1000, "ms", 1),
            DeadlineCompute("d1", base="r1", duration=500, duration_unit="ms"),
            read("r2", "monotonic", 1, "s", 1),  # 1s == 1000ms
            Compare("c1", deadline="d1", now="r2"),
        ])
        f = findings[0]
        self.assertEqual(f.verdict, VERDICT_ACCEPT)
        self.assertEqual(f.diff_ns, 500_000_000)  # 1500ms - 1000ms
        self.assertTrue(any("统一" in n for n in f.notes))

    def test_restored_monotonic_deadline_with_baseline_proof(self):
        """旧进程单调期限具备基准转换证明时可参与当前比较。"""
        auditor = Auditor()
        findings = auditor.ingest([
            read("r1", "monotonic", 1000, "ms", 1),
            DeadlineCompute("d1", base="r1", duration=500, duration_unit="ms"),
            Save("s1", target="d1", generation=1),
            Restore("rst1", saved="s1", generation=2),
            BaselineProof("p1", from_generation=1, to_generation=2,
                          offset=4200, unit="ms"),
            read("r2", "monotonic", 5000, "ms", 2),
            Compare("c1", deadline="rst1", now="r2"),
        ])
        f = findings[0]
        self.assertEqual(f.verdict, VERDICT_ACCEPT)
        # now 换算回旧基准: 5000 + 4200 = 9200ms; diff = 1500 - 9200
        self.assertEqual(f.diff_ns, (1500 - 9200) * 1_000_000)
        self.assertTrue(f.expired)
        self.assertTrue(any("p1" in n for n in f.notes))
        # 证据路径贯穿 读取->计算->保存->恢复
        self.assertEqual(f.deadline_evidence, ["r1", "d1", "s1", "rst1"])


class TestRejectedFlows(unittest.TestCase):
    """应被阻止或拒绝的结果。"""

    def test_cross_clock_domain_compare_rejected(self):
        """monotonic 期限与 wall 当前时间求差：跨时钟域，必须阻止。"""
        auditor = Auditor()
        findings = auditor.ingest([
            read("r1", "monotonic", 1000, "ms", 1),
            DeadlineCompute("d1", base="r1", duration=500, duration_unit="ms"),
            read("r2", "wall", 1700000000000, "ms", 1),
            Compare("c1", deadline="d1", now="r2"),
        ])
        f = findings[0]
        self.assertEqual(f.verdict, VERDICT_REJECT)
        self.assertIn("跨时钟域", f.reason)
        self.assertIsNone(f.diff_ns)
        self.assertIsNone(f.expired)
        # 两侧证据路径都必须给出
        self.assertEqual(f.deadline_evidence, ["r1", "d1"])
        self.assertEqual(f.now_evidence, ["r2"])

    def test_restored_monotonic_deadline_without_proof_rejected(self):
        """恢复的旧进程单调期限无基准转换证明，比较被阻止。"""
        auditor = Auditor()
        findings = auditor.ingest([
            read("r1", "monotonic", 1000, "ms", 1),
            DeadlineCompute("d1", base="r1", duration=500, duration_unit="ms"),
            Save("s1", target="d1", generation=1),
            Restore("rst1", saved="s1", generation=2),
            read("r2", "monotonic", 5000, "ms", 2),
            Compare("c1", deadline="rst1", now="r2"),
        ])
        f = findings[0]
        self.assertEqual(f.verdict, VERDICT_REJECT)
        self.assertIn("基准转换证明", f.reason)
        self.assertIsNone(f.diff_ns)

    def test_wall_clock_cross_generation_not_blocked(self):
        """对照：wall 时钟跨代次不受单调时钟证明规则约束。"""
        auditor = Auditor()
        findings = auditor.ingest([
            read("r1", "wall", 1700000000000, "ms", 1),
            DeadlineCompute("d1", base="r1", duration=60, duration_unit="s"),
            Save("s1", target="d1", generation=1),
            Restore("rst1", saved="s1", generation=2),
            read("r2", "wall", 1700000001000, "ms", 2),
            Compare("c1", deadline="rst1", now="r2"),
        ])
        self.assertEqual(findings[0].verdict, VERDICT_ACCEPT)


class TestPendingAndEvidence(unittest.TestCase):
    """未标明来源进入待核对；单侧有效时钟仍给出另一侧证据路径。"""

    def test_unlabeled_source_goes_pending_with_other_side_evidence(self):
        auditor = Auditor()
        findings = auditor.ingest([
            read("r1", None, 9999, "ms", 1),  # 未标明来源
            DeadlineCompute("d1", base="r1", duration=10, duration_unit="ms"),
            read("r2", "monotonic", 5000, "ms", 1),
            Compare("c1", deadline="d1", now="r2"),
        ])
        f = findings[0]
        self.assertEqual(f.verdict, VERDICT_PENDING)
        self.assertIn("未标明来源", f.reason)
        # 只有 now 侧用有效时钟：结论中仍指出其证据路径
        self.assertEqual(f.now_evidence, ["r2"])
        self.assertTrue(any("r2" in n for n in f.notes))
        # deadline 侧证据路径同样保留，供人工核对
        self.assertEqual(f.deadline_evidence, ["r1", "d1"])

    def test_unknown_unit_goes_pending(self):
        auditor = Auditor()
        findings = auditor.ingest([
            read("r1", "monotonic", 1000, None, 1),  # 单位缺失
            DeadlineCompute("d1", base="r1", duration=500, duration_unit="ms"),
            read("r2", "monotonic", 1200, "ms", 1),
            Compare("c1", deadline="d1", now="r2"),
        ])
        f = findings[0]
        self.assertEqual(f.verdict, VERDICT_PENDING)
        self.assertIn("单位", f.reason)


class TestBoundaryAndInterleaved(unittest.TestCase):
    """边界与交错场景。"""

    def test_deadline_exactly_equal_to_now_is_expired(self):
        """边界：diff == 0 视为已到期，但比较本身可接受。"""
        auditor = Auditor()
        findings = auditor.ingest([
            read("r1", "monotonic", 1000, "ms", 1),
            DeadlineCompute("d1", base="r1", duration=500, duration_unit="ms"),
            read("r2", "monotonic", 1500, "ms", 1),
            Compare("c1", deadline="d1", now="r2"),
        ])
        f = findings[0]
        self.assertEqual(f.verdict, VERDICT_ACCEPT)
        self.assertEqual(f.diff_ns, 0)
        self.assertTrue(f.expired)

    def test_one_nanosecond_before_deadline_not_expired(self):
        """边界：差 1ns 未到期。"""
        auditor = Auditor()
        findings = auditor.ingest([
            read("r1", "monotonic", 1_000_000_000, "ns", 1),
            DeadlineCompute("d1", base="r1", duration=500, duration_unit="ns"),
            read("r2", "monotonic", 1_000_000_499, "ns", 1),
            Compare("c1", deadline="d1", now="r2"),
        ])
        f = findings[0]
        self.assertEqual(f.verdict, VERDICT_ACCEPT)
        self.assertEqual(f.diff_ns, 1)
        self.assertFalse(f.expired)

    def test_interleaved_flow_yields_mixed_verdicts(self):
        """交错场景：同一数据流中 ACCEPT / REJECT / PENDING 并存且互不干扰。"""
        auditor = Auditor()
        findings = auditor.ingest([
            # 可接受的一支
            read("ok-read", "monotonic", 100, "ms", 1),
            DeadlineCompute("ok-dl", base="ok-read", duration=50,
                            duration_unit="ms"),
            read("ok-now", "monotonic", 120, "ms", 1),
            Compare("c-ok", deadline="ok-dl", now="ok-now"),
            # 应被拒绝的一支：跨时钟域
            read("bad-read", "wall", 100, "ms", 1),
            DeadlineCompute("bad-dl", base="bad-read", duration=50,
                            duration_unit="ms"),
            Compare("c-cross", deadline="bad-dl", now="ok-now"),
            # 待核对的一支：来源未标明
            read("mystery-read", None, 100, "ms", 1),
            DeadlineCompute("mystery-dl", base="mystery-read", duration=50,
                            duration_unit="ms"),
            Compare("c-pending", deadline="mystery-dl", now="ok-now"),
        ])
        by_id = {f.compare_id: f for f in findings}
        self.assertEqual(by_id["c-ok"].verdict, VERDICT_ACCEPT)
        self.assertEqual(by_id["c-ok"].diff_ns, 30_000_000)
        self.assertEqual(by_id["c-cross"].verdict, VERDICT_REJECT)
        self.assertEqual(by_id["c-pending"].verdict, VERDICT_PENDING)
        # 交错流中合法一支的判定不受相邻违规影响
        self.assertFalse(by_id["c-ok"].expired)

    def test_restore_same_generation_needs_no_proof(self):
        """边界：保存与恢复在同一进程代次内，无需证明即可比较。"""
        auditor = Auditor()
        findings = auditor.ingest([
            read("r1", "monotonic", 1000, "ms", 1),
            DeadlineCompute("d1", base="r1", duration=500, duration_unit="ms"),
            Save("s1", target="d1", generation=1),
            Restore("rst1", saved="s1", generation=1),
            read("r2", "monotonic", 1400, "ms", 1),
            Compare("c1", deadline="rst1", now="r2"),
        ])
        f = findings[0]
        self.assertEqual(f.verdict, VERDICT_ACCEPT)
        self.assertEqual(f.diff_ns, 100_000_000)


if __name__ == "__main__":
    unittest.main()
