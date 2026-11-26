"""按任一历史入账点复算：入账与发生交错时的可见性。"""

import unittest

from recovery_auth import (
    Cancellation,
    ChannelRebind,
    DeviceConfirmation,
    EmailConfirmation,
    Interval,
    RecoveryRequest,
)

from helpers import make_engine, t


class AsOfRecomputeTest(unittest.TestCase):
    def test_late_recorded_rebind_changes_attribution(self):
        """较晚入账的改绑改变确认归属：同一请求在不同入账点结论不同。"""
        events = [
            ChannelRebind("rb-email-1", t(0), t(0), "email", "a@x.com"),
            ChannelRebind("rb-dev-1", t(0), t(0), "device", "dev-1"),
            EmailConfirmation("ec-1", t(1), t(1), "a@x.com"),
            DeviceConfirmation("dc-1", t(2), t(2), "dev-1"),
            RecoveryRequest("req", t(4), t(4), "high"),
            # 改绑业务发生在 t(0)30m（早于 ec-1），但 t(8) 才入账
            ChannelRebind("rb-email-late", t(0, 30), t(8), "email", "c@x.com",
                          previous_value="a@x.com"),
        ]
        engine = make_engine(events)
        # 入账点 t(7)：迟到改绑不可见，ec-1 归属 v1 → 许可成立
        early = engine.evaluate("req", as_of=t(7))
        self.assertEqual(early.status, "permitted")
        # 入账点 t(9)：ec-1 发生时渠道已是 c@x.com → 确认失效 → 拒绝
        late = engine.evaluate("req", as_of=t(9))
        self.assertEqual(late.status, "denied")
        links = {l.proof_event_id: l for l in late.evidence_chain.links}
        self.assertFalse(links["ec-1"].accepted)

    def test_late_recorded_cancel_visible_only_after_recording(self):
        """较晚入账的取消：入账前入账点复算仍许可，入账后复算被推翻。"""
        events = [
            ChannelRebind("rb-email-1", t(0), t(0), "email", "a@x.com"),
            ChannelRebind("rb-dev-1", t(0), t(0), "device", "dev-1"),
            EmailConfirmation("ec-1", t(1), t(1), "a@x.com"),
            DeviceConfirmation("dc-1", t(2), t(2), "dev-1"),
            RecoveryRequest("req", t(4), t(4), "high"),
            Cancellation("cx-1", t(5), t(12), "req"),
        ]
        engine = make_engine(events)
        at_11 = engine.evaluate("req", as_of=t(11))
        self.assertEqual(at_11.status, "permitted")
        self.assertEqual(at_11.validity_interval, Interval(t(6), t(30)))
        at_12 = engine.evaluate("req", as_of=t(12))
        self.assertEqual(at_12.status, "revoked")

    def test_request_not_visible_raises(self):
        """请求本身在入账点不可见时无法复算。"""
        events = [
            ChannelRebind("rb-email-1", t(0), t(0), "email", "a@x.com"),
            RecoveryRequest("req", t(4), t(4), "normal"),
        ]
        engine = make_engine(events)
        with self.assertRaises(LookupError):
            engine.evaluate("req", as_of=t(3))

    def test_recompute_is_deterministic_per_ledger_point(self):
        """同一入账点重复复算结果一致（无状态泄漏）。"""
        events = [
            ChannelRebind("rb-email-1", t(0), t(0), "email", "a@x.com"),
            ChannelRebind("rb-dev-1", t(0), t(0), "device", "dev-1"),
            EmailConfirmation("ec-1", t(1), t(1), "a@x.com"),
            DeviceConfirmation("dc-1", t(2), t(2), "dev-1"),
            RecoveryRequest("req", t(4), t(4), "high"),
            Cancellation("cx-1", t(5), t(12), "req"),
        ]
        engine = make_engine(events)
        self.assertEqual(engine.evaluate("req", as_of=t(12)),
                         engine.evaluate("req", as_of=t(12)))
        self.assertEqual(engine.evaluate("req", as_of=t(11)).status, "permitted")
        # 再回到较晚入账点，结果不受先前调用影响
        self.assertEqual(engine.evaluate("req", as_of=t(12)).status, "revoked")


if __name__ == "__main__":
    unittest.main()
