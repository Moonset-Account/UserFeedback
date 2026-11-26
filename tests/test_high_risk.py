"""高风险许可：发起前需两条来自不同恢复来源的证明。"""

import unittest

from recovery_auth import (
    ChannelRebind,
    DeviceConfirmation,
    EmailConfirmation,
    RecoveryRequest,
    ServiceCredential,
)

from helpers import make_engine, t


def base_channels():
    return [
        ChannelRebind("rb-email-1", t(0), t(0), "email", "a@x.com"),
        ChannelRebind("rb-dev-1", t(0), t(0), "device", "dev-1"),
    ]


class HighRiskTest(unittest.TestCase):
    def test_two_distinct_sources_granted(self):
        """邮件 + 设备两个不同来源 → 高风险许可成立。"""
        engine = make_engine(base_channels() + [
            EmailConfirmation("ec-1", t(1), t(1), "a@x.com"),
            DeviceConfirmation("dc-1", t(2), t(2), "dev-1"),
            RecoveryRequest("req", t(4), t(4), "high"),
        ])
        d = engine.evaluate("req")
        self.assertTrue(d.granted)
        self.assertEqual(d.evidence_chain.distinct_sources, {"email", "device"})
        self.assertEqual(d.status, "permitted")

    def test_same_source_twice_not_enough(self):
        """两条邮件确认同属一个恢复来源 → 高风险拒绝。"""
        engine = make_engine(base_channels() + [
            EmailConfirmation("ec-1", t(1), t(1), "a@x.com"),
            EmailConfirmation("ec-2", t(2), t(2), "a@x.com"),
            RecoveryRequest("req", t(4), t(4), "high"),
        ])
        d = engine.evaluate("req")
        self.assertFalse(d.granted)
        self.assertEqual(d.status, "denied")
        self.assertIsNone(d.validity_interval)

    def test_email_plus_service_credential_granted(self):
        """邮件 + 客服凭据 → 两个不同来源，许可成立。"""
        engine = make_engine(base_channels() + [
            EmailConfirmation("ec-1", t(1), t(1), "a@x.com"),
            ServiceCredential("sc-1", t(2), t(2), "cred-1", "agent-07"),
            RecoveryRequest("req", t(4), t(4), "high"),
        ])
        d = engine.evaluate("req")
        self.assertEqual(d.status, "permitted")
        self.assertEqual(d.evidence_chain.distinct_sources, {"email", "service"})

    def test_expired_service_credential_rejected(self):
        """客服凭据在发起前已过期 → 不计入。"""
        engine = make_engine(base_channels() + [
            EmailConfirmation("ec-1", t(1), t(1), "a@x.com"),
            ServiceCredential("sc-1", t(2), t(2), "cred-1", "agent-07",
                              valid_until=t(3)),
            RecoveryRequest("req", t(4), t(4), "high"),
        ])
        d = engine.evaluate("req")
        links = {l.proof_event_id: l for l in d.evidence_chain.links}
        self.assertFalse(links["sc-1"].accepted)
        self.assertEqual(d.status, "denied")

    def test_unverified_email_rejected(self):
        """未验真的邮件确认不计入。"""
        engine = make_engine(base_channels() + [
            EmailConfirmation("ec-1", t(1), t(1), "a@x.com", verified=False),
            DeviceConfirmation("dc-1", t(2), t(2), "dev-1"),
            RecoveryRequest("req", t(4), t(4), "high"),
        ])
        d = engine.evaluate("req")
        links = {l.proof_event_id: l for l in d.evidence_chain.links}
        self.assertFalse(links["ec-1"].accepted)
        self.assertEqual(d.status, "denied")

    def test_proof_at_exact_initiation_not_counted(self):
        """证明须发生在发起前：与发起同时刻的证明不计入。"""
        engine = make_engine(base_channels() + [
            EmailConfirmation("ec-1", t(1), t(1), "a@x.com"),
            DeviceConfirmation("dc-late", t(4), t(4), "dev-1"),
            RecoveryRequest("req", t(4), t(4), "high"),
        ])
        d = engine.evaluate("req")
        links = {l.proof_event_id: l for l in d.evidence_chain.links}
        self.assertFalse(links["dc-late"].accepted)
        self.assertEqual(d.status, "denied")

    def test_normal_risk_single_proof(self):
        """普通风险：一条有效证明即可。"""
        engine = make_engine(base_channels() + [
            EmailConfirmation("ec-1", t(1), t(1), "a@x.com"),
            RecoveryRequest("req", t(4), t(4), "normal"),
        ])
        d = engine.evaluate("req")
        self.assertEqual(d.status, "permitted")

    def test_normal_risk_no_proof_denied(self):
        engine = make_engine(base_channels() + [
            RecoveryRequest("req", t(4), t(4), "normal"),
        ])
        d = engine.evaluate("req")
        self.assertEqual(d.status, "denied")


if __name__ == "__main__":
    unittest.main()
