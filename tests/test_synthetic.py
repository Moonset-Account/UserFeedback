"""合成场景目录的结构不变量测试。"""

import unittest
from collections import Counter

from sigreview.synthetic import build_fixture_traces
from sigreview.verifier import (
    CheckResult,
    Decision,
    Purpose,
    RejectReason,
    StrategyName,
    Trace,
)


class FixtureStructureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.traces = build_fixture_traces()

    def test_all_traces_have_attempts_and_serializable_form(self) -> None:
        for trace in self.traces:
            self.assertTrue(trace.attempts)
            data = trace.to_dict()
            self.assertEqual(data["request_id"], trace.request_id)
            self.assertTrue(data["attempts"])
            self.assertEqual(
                data["final_decision"], trace.final_decision.value
            )

    def test_positive_and_negative_pair_share_digest(self) -> None:
        by_interface = {}
        for trace in self.traces:
            by_interface.setdefault(trace.interface, []).append(trace)

        payments = by_interface["payments.submit"]
        digests = {t.data_digest for t in payments}
        self.assertEqual(len(digests), 1)  # 正反例摘要完全相同
        expected = {Purpose.TLS_CLIENT.value}
        self.assertEqual(
            {t.expected_purpose.value for t in payments}, expected
        )

    def test_firmware_fallback_trace_shape(self) -> None:
        firmware = [
            t for t in self.traces if t.interface == "firmware.upload"
        ]
        self.assertEqual(len(firmware), 2)
        negative = [
            t for t in firmware if t.final_decision is Decision.ALLOW
            and t.strict_purpose_check is CheckResult.PURPOSE_MISMATCH
        ]
        self.assertEqual(len(negative), 1)
        trace = negative[0]
        self.assertEqual(
            [a.strategy for a in trace.attempts],
            [StrategyName.STRICT, StrategyName.COMPATIBILITY],
        )

    def test_bad_signature_trace_never_runs_purpose_check(self) -> None:
        mail = [t for t in self.traces if t.interface == "mail.relay"]
        bad = [t for t in mail if not t.signature_valid]
        self.assertEqual(len(bad), 1)
        trace = bad[0]
        self.assertEqual(trace.final_reason, RejectReason.BAD_SIGNATURE)
        self.assertIsNone(trace.strict_purpose_check)
        self.assertEqual(
            trace.strategies_tried, [StrategyName.STRICT]
        )

    def test_each_interface_has_single_expected_purpose(self) -> None:
        seen = {}
        for trace in self.traces:
            seen.setdefault(trace.interface, set()).add(
                trace.expected_purpose
            )
        for interface, purposes in seen.items():
            self.assertEqual(
                len(purposes), 1, f"{interface} 期望用途不唯一"
            )

    def test_request_ids_are_unique(self) -> None:
        ids = [t.request_id for t in self.traces]
        counts = Counter(ids)
        duplicates = [rid for rid, n in counts.items() if n > 1]
        self.assertEqual(duplicates, [])


if __name__ == "__main__":
    unittest.main()
