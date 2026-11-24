"""严格 / 兼容策略与验签引擎的单元测试。"""

import unittest

from sigreview.verifier import (
    CheckResult,
    CompatibilityPolicy,
    Decision,
    Purpose,
    RejectReason,
    StrictPolicy,
    StrategyName,
    VerificationEngine,
    compute_digest,
    sign_payload,
)
from sigreview.synthetic import RecordBuilder, SYNTHETIC_SECRET


class PolicyTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.builder = RecordBuilder()
        self.interface = "payments.submit"  # 期望用途 TLS_CLIENT
        self.payload = b"payment-payload-001"

    def build_request(self, purposes, *, tamper: bool = False):
        request = self.builder.signed_request(
            self.interface, self.payload, list(purposes)
        )
        if tamper:
            request = self.builder.tamper_signature(request)
        return request


class StrictPolicyTest(PolicyTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.policy = StrictPolicy()

    def test_allows_when_signature_ok_and_purpose_matches(self) -> None:
        request = self.build_request([Purpose.TLS_CLIENT])
        attempt = self.policy.handle(request, SYNTHETIC_SECRET)
        self.assertEqual(attempt.decision, Decision.ALLOW)
        self.assertIn(CheckResult.SIGNATURE_OK, attempt.checks)
        self.assertIn(CheckResult.PURPOSE_OK, attempt.checks)
        self.assertIsNone(attempt.reason)

    def test_denies_purpose_mismatch_after_signature_ok(self) -> None:
        request = self.build_request([Purpose.TLS_SERVER])
        attempt = self.policy.handle(request, SYNTHETIC_SECRET)
        self.assertEqual(attempt.decision, Decision.DENY)
        self.assertEqual(attempt.reason, RejectReason.PURPOSE_MISMATCH)
        self.assertEqual(
            attempt.checks,
            [CheckResult.SIGNATURE_OK, CheckResult.PURPOSE_MISMATCH],
        )

    def test_bad_signature_rejects_before_purpose_check(self) -> None:
        request = self.build_request([Purpose.TLS_SERVER], tamper=True)
        attempt = self.policy.handle(request, SYNTHETIC_SECRET)
        self.assertEqual(attempt.decision, Decision.DENY)
        self.assertEqual(attempt.reason, RejectReason.BAD_SIGNATURE)
        # 关键断言：提前拒绝，轨迹里没有任何用途检查结论
        self.assertNotIn(CheckResult.PURPOSE_MISMATCH, attempt.checks)
        self.assertNotIn(CheckResult.PURPOSE_OK, attempt.checks)
        self.assertEqual(attempt.checks, [CheckResult.SIGNATURE_BAD])


class CompatibilityPolicyTest(PolicyTestBase):
    def test_skips_purpose_and_allows_legacy_client(self) -> None:
        policy = CompatibilityPolicy()  # 默认不强制用途
        request = self.build_request([Purpose.TLS_SERVER])
        attempt = policy.handle(request, SYNTHETIC_SECRET)
        self.assertEqual(attempt.decision, Decision.ALLOW)
        self.assertIn(CheckResult.SIGNATURE_OK, attempt.checks)
        self.assertIn(CheckResult.PURPOSE_SKIPPED_LEGACY, attempt.checks)
        self.assertNotIn(CheckResult.PURPOSE_MISMATCH, attempt.checks)

    def test_compatibility_still_denies_bad_signature(self) -> None:
        policy = CompatibilityPolicy()
        request = self.build_request([Purpose.TLS_SERVER], tamper=True)
        attempt = policy.handle(request, SYNTHETIC_SECRET)
        self.assertEqual(attempt.decision, Decision.DENY)
        self.assertEqual(attempt.reason, RejectReason.BAD_SIGNATURE)

    def test_enforce_purpose_mode_denies_mismatch(self) -> None:
        policy = CompatibilityPolicy(enforce_purpose=True)
        request = self.build_request([Purpose.TLS_SERVER])
        attempt = policy.handle(request, SYNTHETIC_SECRET)
        self.assertEqual(attempt.decision, Decision.DENY)
        self.assertEqual(attempt.reason, RejectReason.PURPOSE_MISMATCH)


class EngineTest(PolicyTestBase):
    def test_strict_only_engine_denies_mismatch_with_single_attempt(self) -> None:
        engine = VerificationEngine(secret=SYNTHETIC_SECRET)
        request = self.build_request([Purpose.TLS_SERVER])
        trace = engine.verify(request)
        self.assertEqual(trace.final_decision, Decision.DENY)
        self.assertEqual(trace.final_reason, RejectReason.PURPOSE_MISMATCH)
        self.assertEqual(len(trace.attempts), 1)
        self.assertEqual(
            trace.strategies_tried, [StrategyName.STRICT]
        )
        self.assertTrue(trace.signature_valid)

    def test_fallback_engine_allows_mismatch_via_compatibility(self) -> None:
        engine = VerificationEngine(
            secret=SYNTHETIC_SECRET,
            fallback_to_compatibility=True,
            compatibility_policy=CompatibilityPolicy(),
        )
        request = self.build_request([Purpose.TLS_SERVER])
        trace = engine.verify(request)
        # 严格拒绝后兼容继续放行：两次尝试、最终放行
        self.assertEqual(len(trace.attempts), 2)
        self.assertEqual(
            trace.strategies_tried,
            [StrategyName.STRICT, StrategyName.COMPATIBILITY],
        )
        self.assertEqual(trace.strict_attempt.decision, Decision.DENY)
        self.assertEqual(
            trace.strict_attempt.reason, RejectReason.PURPOSE_MISMATCH
        )
        self.assertEqual(trace.compatibility_attempt.decision, Decision.ALLOW)
        self.assertEqual(trace.final_decision, Decision.ALLOW)
        self.assertIsNone(trace.final_reason)
        self.assertEqual(
            trace.strict_purpose_check, CheckResult.PURPOSE_MISMATCH
        )

    def test_bad_signature_short_circuits_without_compatibility(self) -> None:
        engine = VerificationEngine(
            secret=SYNTHETIC_SECRET,
            fallback_to_compatibility=True,
            compatibility_policy=CompatibilityPolicy(),
        )
        request = self.build_request([Purpose.TLS_SERVER], tamper=True)
        trace = engine.verify(request)
        # 签名错误提前终止：兼容策略不被调用
        self.assertEqual(len(trace.attempts), 1)
        self.assertEqual(
            trace.strategies_tried, [StrategyName.STRICT]
        )
        self.assertEqual(trace.final_reason, RejectReason.BAD_SIGNATURE)
        self.assertFalse(trace.signature_valid)
        self.assertIsNone(trace.strict_purpose_check)

    def test_positive_request_does_not_invoke_compatibility(self) -> None:
        engine = VerificationEngine(
            secret=SYNTHETIC_SECRET,
            fallback_to_compatibility=True,
            compatibility_policy=CompatibilityPolicy(),
        )
        request = self.build_request([Purpose.TLS_CLIENT])
        trace = engine.verify(request)
        self.assertEqual(trace.final_decision, Decision.ALLOW)
        self.assertEqual(len(trace.attempts), 1)
        self.assertEqual(
            trace.strict_purpose_check, CheckResult.PURPOSE_OK
        )


class SyntheticHelperTest(unittest.TestCase):
    def test_digest_and_signature_are_deterministic(self) -> None:
        payload = b"abc"
        self.assertEqual(compute_digest(payload), compute_digest(payload))
        self.assertEqual(
            sign_payload(payload, b"k"), sign_payload(payload, b"k")
        )
        self.assertNotEqual(
            sign_payload(payload, b"k"), sign_payload(payload, b"other")
        )

    def test_tamper_changes_signature_but_keeps_digest(self) -> None:
        builder = RecordBuilder()
        original = builder.signed_request(
            "payments.submit", b"xyz", [Purpose.TLS_CLIENT]
        )
        tampered = builder.tamper_signature(original)
        self.assertNotEqual(original.signature, tampered.signature)
        self.assertEqual(original.data_digest, tampered.data_digest)
        self.assertEqual(
            original.certificate.purposes, tampered.certificate.purposes
        )


if __name__ == "__main__":
    unittest.main()
