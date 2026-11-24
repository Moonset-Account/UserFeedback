"""审查器对合成轨迹的配对、归因与缺口标记测试。"""

import unittest
from typing import Dict, List

from sigreview.review import (
    MissingObservation,
    NegativeClass,
    RegressionReviewer,
)
from sigreview.synthetic import build_fixture_traces
from sigreview.verifier import (
    CheckResult,
    Decision,
    Purpose,
    RejectReason,
    Trace,
)
from sigreview.synthetic import RecordBuilder


class FixtureReviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.traces: List[Trace] = build_fixture_traces()
        cls.report = RegressionReviewer().review(cls.traces)
        cls.by_interface: Dict[str, object] = {
            report.interface: report for report in cls.report.interface_reports
        }

    def test_fixture_trace_count_and_interfaces(self) -> None:
        # 场景目录固定 11 条请求、6 个接口
        self.assertEqual(self.report.total_traces, 11)
        self.assertEqual(
            sorted(self.by_interface),
            sorted(
                [
                    "payments.submit",
                    "firmware.upload",
                    "mail.relay",
                    "reports.export",
                    "clock.anchor",
                    "devices.enroll",
                ]
            ),
        )

    def test_interface_a_pair_is_credited(self) -> None:
        report = self.by_interface["payments.submit"]
        self.assertTrue(report.covered)
        self.assertEqual(len(report.evidence_pairs), 1)
        pair = report.evidence_pairs[0]
        self.assertTrue(pair.credited)
        self.assertEqual(pair.missing, [])
        self.assertIsNotNone(pair.positive_trace)
        self.assertIsNotNone(pair.negative_trace)
        self.assertEqual(
            pair.negative_class, NegativeClass.PURPOSE_ENFORCED_DENY
        )
        # 正反例摘要一致、期望用途一致
        self.assertEqual(
            pair.positive_trace.data_digest, pair.negative_trace.data_digest
        )
        self.assertEqual(
            pair.positive_trace.expected_purpose,
            pair.negative_trace.expected_purpose,
        )
        # 正例放行、负例最终拒绝且原因为用途不符
        self.assertEqual(
            pair.positive_trace.final_decision, Decision.ALLOW
        )
        self.assertEqual(
            pair.negative_trace.final_decision, Decision.DENY
        )
        self.assertEqual(
            pair.negative_trace.final_reason,
            RejectReason.PURPOSE_MISMATCH,
        )

    def test_interface_b_fallback_hole_is_listed_separately(self) -> None:
        report = self.by_interface["firmware.upload"]
        self.assertFalse(report.covered)
        pair = report.evidence_pairs[0]
        self.assertFalse(pair.credited)
        self.assertTrue(pair.is_fallback_hole)
        self.assertEqual(
            pair.negative_class,
            NegativeClass.COMPATIBILITY_FALLBACK_ALLOW,
        )
        # 负例轨迹自身可核验：严格用途拒绝 -> 兼容放行
        negative = pair.negative_trace
        self.assertEqual(
            negative.strict_purpose_check, CheckResult.PURPOSE_MISMATCH
        )
        self.assertEqual(negative.final_decision, Decision.ALLOW)
        self.assertEqual(len(negative.attempts), 2)
        # 顶层缺口清单单独列出
        holes = self.report.all_fallback_holes
        self.assertEqual(len(holes), 1)
        self.assertEqual(holes[0].interface, "firmware.upload")
        # 缺口同时标记缺失“最终因用途而拒绝”的观测
        self.assertIn(
            MissingObservation.MISSING_NEGATIVE_PURPOSE_DENY, pair.missing
        )

    def test_interface_c_bad_signature_does_not_earn_credit(self) -> None:
        report = self.by_interface["mail.relay"]
        self.assertFalse(report.covered)
        self.assertEqual(len(report.bad_signature_traces), 1)
        bad = report.bad_signature_traces[0]
        self.assertFalse(bad.signature_valid)
        self.assertEqual(bad.final_reason, RejectReason.BAD_SIGNATURE)
        self.assertIsNone(bad.strict_purpose_check)
        # 签名错误请求不进入证据配对
        self.assertEqual(len(report.evidence_pairs), 1)
        pair = report.evidence_pairs[0]
        self.assertIsNotNone(pair.positive_trace)
        self.assertIsNone(pair.negative_trace)
        self.assertIn(
            MissingObservation.MISSING_NEGATIVE_PURPOSE_DENY, pair.missing
        )

    def test_interface_d_missing_positive_observation(self) -> None:
        report = self.by_interface["reports.export"]
        self.assertFalse(report.covered)
        pair = report.evidence_pairs[0]
        self.assertIsNone(pair.positive_trace)
        self.assertIsNotNone(pair.negative_trace)
        self.assertEqual(
            pair.negative_class, NegativeClass.PURPOSE_ENFORCED_DENY
        )
        self.assertIn(MissingObservation.MISSING_POSITIVE, pair.missing)

    def test_interface_e_missing_negative_observation(self) -> None:
        report = self.by_interface["clock.anchor"]
        self.assertFalse(report.covered)
        pair = report.evidence_pairs[0]
        self.assertIsNotNone(pair.positive_trace)
        self.assertIsNone(pair.negative_trace)
        self.assertIn(
            MissingObservation.MISSING_NEGATIVE_PURPOSE_DENY, pair.missing
        )

    def test_interface_f_interleaved_digest_groups(self) -> None:
        report = self.by_interface["devices.enroll"]
        # 同接口两个摘要组
        self.assertEqual(len(report.evidence_pairs), 2)
        credited = [p for p in report.evidence_pairs if p.credited]
        uncredited = [p for p in report.evidence_pairs if not p.credited]
        self.assertEqual(len(credited), 1)
        self.assertEqual(len(uncredited), 1)
        # 有一组记功即视为接口覆盖
        self.assertTrue(report.covered)
        weak = uncredited[0]
        self.assertIsNone(weak.positive_trace)
        self.assertIsNotNone(weak.negative_trace)
        self.assertIn(MissingObservation.MISSING_POSITIVE, weak.missing)
        # 两组摘要不同
        self.assertNotEqual(
            credited[0].data_digest, weak.data_digest
        )

    def test_covered_and_uncovered_interface_lists(self) -> None:
        self.assertEqual(
            self.report.covered_interfaces,
            ["devices.enroll", "payments.submit"],
        )
        self.assertEqual(
            self.report.uncovered_interfaces,
            [
                "clock.anchor",
                "firmware.upload",
                "mail.relay",
                "reports.export",
            ],
        )

    def test_report_dict_and_text_are_serializable(self) -> None:
        data = self.report.to_dict()
        self.assertEqual(data["total_traces"], 11)
        self.assertEqual(data["fallback_hole_count"], 1)
        text = self.report.render_text()
        self.assertIn("证书用途限制离线回归审查报告", text)
        self.assertIn("payments.submit", text)
        self.assertIn("严格拒绝 / 兼容放行缺口清单", text)


class CustomScenarioTest(unittest.TestCase):
    """用动态构造的轨迹补充审查器边界行为。"""

    def setUp(self) -> None:
        self.builder = RecordBuilder()
        self.reviewer = RegressionReviewer()

    def test_pairs_require_same_digest(self) -> None:
        # 同接口、用途一正一负，但数据摘要不同：不能配对
        good = self.builder.signed_request(
            "payments.submit", b"payload-alpha", [Purpose.TLS_CLIENT]
        )
        bad = self.builder.signed_request(
            "payments.submit", b"payload-beta", [Purpose.TLS_SERVER]
        )
        traces = self.builder.run([good, bad])
        report = self.reviewer.review(traces)
        interface_report = report.interface_reports[0]
        self.assertEqual(len(interface_report.evidence_pairs), 2)
        self.assertFalse(interface_report.covered)
        missing = {
            m
            for pair in interface_report.evidence_pairs
            for m in pair.missing
        }
        self.assertIn(MissingObservation.MISSING_POSITIVE, missing)
        self.assertIn(
            MissingObservation.MISSING_NEGATIVE_PURPOSE_DENY, missing
        )

    def test_purpose_enforced_even_with_compat_when_it_checks_purpose(self) -> None:
        # 兼容策略若开启用途强制，负例仍被最终拒绝 -> 可以记功
        good = self.builder.signed_request(
            "mail.relay", b"mail-shared", [Purpose.EMAIL_PROTECTION]
        )
        bad = self.builder.signed_request(
            "mail.relay", b"mail-shared", [Purpose.TLS_CLIENT]
        )
        from sigreview.verifier import VerificationEngine, CompatibilityPolicy
        from sigreview.synthetic import SYNTHETIC_SECRET

        engine = VerificationEngine(
            secret=SYNTHETIC_SECRET,
            fallback_to_compatibility=True,
            compatibility_policy=CompatibilityPolicy(enforce_purpose=True),
        )
        traces = [engine.verify(good), engine.verify(bad)]
        report = self.reviewer.review(traces)
        pair = report.interface_reports[0].evidence_pairs[0]
        self.assertTrue(pair.credited)
        self.assertEqual(
            pair.negative_trace.final_reason,
            RejectReason.PURPOSE_MISMATCH,
        )

    def test_empty_input_returns_empty_report(self) -> None:
        report = self.reviewer.review([])
        self.assertEqual(report.total_traces, 0)
        self.assertEqual(report.interface_reports, [])
        self.assertEqual(report.covered_interfaces, [])
        self.assertIn("轨迹总数：0", report.render_text())

    def test_inconsistent_expected_purpose_within_interface_rejected(self) -> None:
        good = self.builder.signed_request(
            "payments.submit", b"p", [Purpose.TLS_CLIENT]
        )
        other = self.builder.signed_request(
            "payments.submit",
            b"p",
            [Purpose.CODE_SIGNING],
            expected_purpose=Purpose.CODE_SIGNING,
        )
        traces = self.builder.run([good, other])
        with self.assertRaises(ValueError):
            self.reviewer.review(traces)


if __name__ == "__main__":
    unittest.main()
