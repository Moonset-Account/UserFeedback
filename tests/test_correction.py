"""已执行决定：附更正和当时可见的依据。"""

import unittest

from recovery_auth import (
    Cancellation,
    ChannelRebind,
    DeviceConfirmation,
    EmailConfirmation,
    Execution,
    Interval,
    RecoveryRequest,
)

from helpers import make_engine, t


def base_events():
    return [
        ChannelRebind("rb-email-1", t(0), t(0), "email", "a@x.com"),
        ChannelRebind("rb-dev-1", t(0), t(0), "device", "dev-1"),
        EmailConfirmation("ec-1", t(1), t(1), "a@x.com"),
        DeviceConfirmation("dc-1", t(2), t(2), "dev-1"),
        RecoveryRequest("req", t(4), t(4), "high"),
    ]


class CorrectionTest(unittest.TestCase):
    def test_executed_decision_overturned_carries_correction_and_basis(self):
        """取消入账晚于执行：复算推翻许可，输出更正 + 当时可见依据。"""
        events = base_events() + [
            # 操作在 t(7) 实际执行（许可已于 t(6) 生效）
            Execution("ex-1", t(7), t(7), "req"),
            # 取消业务发生在生效前 t(5)，但 t(8) 才入账（晚于执行）
            Cancellation("cx-1", t(5), t(8), "req", "用户撤回"),
        ]
        engine = make_engine(events)
        d = engine.evaluate("req", as_of=t(9))
        self.assertEqual(d.status, "revoked")
        self.assertIsNotNone(d.correction)
        corr = d.correction
        self.assertTrue(corr.changed)
        self.assertEqual(corr.executed_at, t(7))
        # 当时可见的依据：执行时刻取消尚未入账，许可有效
        then = corr.then_visible_basis
        self.assertTrue(then.granted)
        self.assertFalse(then.revoked)
        self.assertEqual(then.validity_interval, Interval(t(6), t(30)))
        self.assertEqual(set(then.distinct_sources), {"email", "device"})
        # 当前复算结论：被推翻
        now = corr.current_finding
        self.assertTrue(now.revoked)
        self.assertIsNone(now.validity_interval)

    def test_executed_decision_consistent_marks_no_change(self):
        """复算与执行时一致：更正标记为无变化，仍附当时依据。"""
        events = base_events() + [
            Execution("ex-1", t(7), t(7), "req"),
        ]
        engine = make_engine(events)
        d = engine.evaluate("req")
        self.assertEqual(d.status, "permitted")
        self.assertIsNotNone(d.correction)
        self.assertFalse(d.correction.changed)
        self.assertIn("无需更正", d.correction.reason)
        self.assertEqual(
            d.correction.then_visible_basis.validity_interval,
            Interval(t(6), t(30)),
        )

    def test_no_execution_no_correction(self):
        """未执行的决定不附更正。"""
        engine = make_engine(base_events())
        d = engine.evaluate("req")
        self.assertIsNone(d.correction)

    def test_correction_uses_first_execution(self):
        """多次执行记录时以最早执行为准。"""
        events = base_events() + [
            Execution("ex-2", t(9), t(9), "req"),
            Execution("ex-1", t(7), t(7), "req"),
            Cancellation("cx-1", t(5), t(8), "req"),
        ]
        engine = make_engine(events)
        d = engine.evaluate("req", as_of=t(10))
        self.assertEqual(d.correction.executed_at, t(7))
        self.assertTrue(d.correction.changed)


if __name__ == "__main__":
    unittest.main()
