"""取消事件：较晚入账的取消推翻仍待生效的许可 / 截断有效期。"""

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


def granted_events():
    """高风险许可成立的基础事件：发起 t(4)，生效 t(6)，到期 t(30)。"""
    return [
        ChannelRebind("rb-email-1", t(0), t(0), "email", "a@x.com"),
        ChannelRebind("rb-dev-1", t(0), t(0), "device", "dev-1"),
        EmailConfirmation("ec-1", t(1), t(1), "a@x.com"),
        DeviceConfirmation("dc-1", t(2), t(2), "dev-1"),
        RecoveryRequest("req", t(4), t(4), "high"),
    ]


class CancellationTest(unittest.TestCase):
    def test_late_recorded_cancel_overturns_pending_permission(self):
        """取消发生在生效前（t(5) < t(6)）但入账晚：复算可见后许可被推翻。"""
        events = granted_events() + [
            Cancellation("cx-1", t(5), t(10), "req", "用户撤回"),
        ]
        engine = make_engine(events)
        # 取消入账前复算：许可有效
        before = engine.evaluate("req", as_of=t(9))
        self.assertEqual(before.status, "permitted")
        self.assertEqual(before.validity_interval, Interval(t(6), t(30)))
        # 取消入账后复算：仍待生效的许可被推翻
        after = engine.evaluate("req", as_of=t(10))
        self.assertEqual(after.status, "revoked")
        self.assertIsNone(after.validity_interval)
        self.assertTrue(after.revoked)

    def test_cancel_within_validity_truncates_interval(self):
        """生效后、到期前的取消将有效区间截断。"""
        events = granted_events() + [
            Cancellation("cx-1", t(10), t(10), "req"),
        ]
        engine = make_engine(events)
        d = engine.evaluate("req")
        self.assertEqual(d.status, "truncated")
        self.assertEqual(d.validity_interval, Interval(t(6), t(10)))

    def test_cancel_after_expiry_has_no_effect(self):
        """到期之后的取消不影响许可区间。"""
        events = granted_events() + [
            Cancellation("cx-1", t(31), t(31), "req"),
        ]
        engine = make_engine(events)
        d = engine.evaluate("req")
        self.assertEqual(d.status, "permitted")
        self.assertEqual(d.validity_interval, Interval(t(6), t(30)))

    def test_cancel_exactly_at_effective_from_overturns(self):
        """取消恰在生效时刻：有效区间为空，视为推翻。"""
        events = granted_events() + [
            Cancellation("cx-1", t(6), t(6), "req"),
        ]
        engine = make_engine(events)
        d = engine.evaluate("req")
        self.assertTrue(d.revoked)
        self.assertIsNone(d.validity_interval)
        self.assertEqual(d.status, "revoked")

    def test_earliest_business_cancel_wins(self):
        """多次取消按业务发生时刻取最早者。"""
        events = granted_events() + [
            Cancellation("cx-late", t(20), t(8), "req"),
            Cancellation("cx-early", t(10), t(9), "req"),
        ]
        engine = make_engine(events)
        d = engine.evaluate("req")
        self.assertEqual(d.validity_interval, Interval(t(6), t(10)))

    def test_cancel_for_other_request_ignored(self):
        events = granted_events() + [
            Cancellation("cx-1", t(5), t(5), "other-req"),
        ]
        engine = make_engine(events)
        d = engine.evaluate("req")
        self.assertEqual(d.status, "permitted")


if __name__ == "__main__":
    unittest.main()
