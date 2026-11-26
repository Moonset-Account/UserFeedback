"""证据链：许可有效区间与跨改绑边界的版本迁移。"""

import unittest

from recovery_auth import (
    ChannelRebind,
    DeviceConfirmation,
    EmailConfirmation,
    Interval,
    RecoveryRequest,
)

from helpers import make_engine, t


class EvidenceChainTest(unittest.TestCase):
    def test_chain_spans_rebind_boundary(self):
        """证明在改绑前作出、请求在改绑后发起：证据链记录跨边界迁移。"""
        engine = make_engine([
            ChannelRebind("rb-email-1", t(0), t(0), "email", "a@x.com"),
            ChannelRebind("rb-dev-1", t(0), t(0), "device", "dev-1"),
            EmailConfirmation("ec-1", t(1), t(1), "a@x.com"),  # 改绑前确认
            ChannelRebind("rb-email-2", t(2), t(2), "email", "b@x.com",
                          previous_value="a@x.com"),
            DeviceConfirmation("dc-1", t(3), t(3), "dev-1"),
            RecoveryRequest("req", t(4), t(4), "high"),
        ])
        d = engine.evaluate("req")
        self.assertEqual(d.status, "permitted")
        # 旧版本上的确认仍被采纳，归属 v1
        links = {l.proof_event_id: l for l in d.evidence_chain.links}
        self.assertTrue(links["ec-1"].accepted)
        self.assertEqual(links["ec-1"].channel_version.version, 1)
        self.assertEqual(links["ec-1"].channel_version.value, "a@x.com")
        # 跨改绑边界：ec-1 之后、发起之前的邮件改绑被记录
        crossings = d.evidence_chain.crossings
        self.assertEqual(len(crossings), 1)
        self.assertEqual(crossings[0].rebind_event_id, "rb-email-2")
        self.assertEqual(crossings[0].channel_kind, "email")
        self.assertEqual(crossings[0].from_version, 1)
        self.assertEqual(crossings[0].to_version, 2)
        self.assertEqual(crossings[0].occurred_at, t(2))

    def test_no_crossing_when_no_rebind_between(self):
        """证明与发起之间无改绑时，证据链无跨边界记录。"""
        engine = make_engine([
            ChannelRebind("rb-email-1", t(0), t(0), "email", "a@x.com"),
            ChannelRebind("rb-dev-1", t(0), t(0), "device", "dev-1"),
            EmailConfirmation("ec-1", t(1), t(1), "a@x.com"),
            DeviceConfirmation("dc-1", t(2), t(2), "dev-1"),
            RecoveryRequest("req", t(4), t(4), "high"),
        ])
        d = engine.evaluate("req")
        self.assertEqual(d.evidence_chain.crossings, ())

    def test_validity_interval_output(self):
        """输出许可有效区间 [生效时刻, 到期时刻)。"""
        engine = make_engine([
            ChannelRebind("rb-email-1", t(0), t(0), "email", "a@x.com"),
            ChannelRebind("rb-dev-1", t(0), t(0), "device", "dev-1"),
            EmailConfirmation("ec-1", t(1), t(1), "a@x.com"),
            DeviceConfirmation("dc-1", t(2), t(2), "dev-1"),
            RecoveryRequest("req", t(4), t(4), "high"),
        ])
        d = engine.evaluate("req")
        self.assertEqual(d.validity_interval, Interval(t(6), t(30)))

    def test_denied_decision_has_empty_chain_evidence(self):
        """拒绝时证据链仍列出各证明的否决原因，便于客服解释。"""
        engine = make_engine([
            ChannelRebind("rb-email-1", t(0), t(0), "email", "a@x.com"),
            EmailConfirmation("ec-1", t(1), t(1), "a@x.com"),
            RecoveryRequest("req", t(4), t(4), "high"),
        ])
        d = engine.evaluate("req")
        self.assertEqual(d.status, "denied")
        self.assertIsNone(d.validity_interval)
        self.assertEqual(len(d.evidence_chain.links), 1)
        self.assertTrue(d.evidence_chain.links[0].accepted)
        self.assertEqual(d.evidence_chain.distinct_sources, {"email"})


if __name__ == "__main__":
    unittest.main()
