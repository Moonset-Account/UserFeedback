"""确认归属：以发生时的渠道版本为准（含改绑边界）。"""

import unittest

from recovery_auth import (
    ChannelRebind,
    DeviceConfirmation,
    EmailConfirmation,
    RecoveryRequest,
)

from helpers import make_engine, t


def email_initial():
    return ChannelRebind("rb-email-1", t(0), t(0), "email", "a@x.com")


def device_initial():
    return ChannelRebind("rb-dev-1", t(0), t(0), "device", "dev-1")


class AttributionTest(unittest.TestCase):
    def test_proof_attributed_to_version_at_occurrence(self):
        """改绑前后的确认分别归属 v1 / v2，证据链体现版本号。"""
        engine = make_engine([
            email_initial(),
            ChannelRebind("rb-email-2", t(2), t(2), "email", "b@x.com",
                          previous_value="a@x.com"),
            EmailConfirmation("ec-old", t(1), t(1), "a@x.com"),
            EmailConfirmation("ec-new", t(3), t(3), "b@x.com"),
            device_initial(),
            DeviceConfirmation("dc-1", t(3), t(3), "dev-1"),
            RecoveryRequest("req", t(4), t(4), "high"),
        ])
        d = engine.evaluate("req")
        links = {l.proof_event_id: l for l in d.evidence_chain.links}
        self.assertTrue(links["ec-old"].accepted)
        self.assertEqual(links["ec-old"].channel_version.version, 1)
        self.assertTrue(links["ec-new"].accepted)
        self.assertEqual(links["ec-new"].channel_version.version, 2)
        self.assertEqual(d.status, "permitted")

    def test_stale_channel_value_rejected(self):
        """改绑后仍发往旧地址的确认与发生时版本不一致，不计入。"""
        engine = make_engine([
            email_initial(),
            ChannelRebind("rb-email-2", t(2), t(2), "email", "b@x.com",
                          previous_value="a@x.com"),
            EmailConfirmation("ec-stale", t(3), t(3), "a@x.com"),
            device_initial(),
            DeviceConfirmation("dc-1", t(3), t(3), "dev-1"),
            RecoveryRequest("req", t(4), t(4), "high"),
        ])
        d = engine.evaluate("req")
        links = {l.proof_event_id: l for l in d.evidence_chain.links}
        self.assertFalse(links["ec-stale"].accepted)
        self.assertEqual(links["ec-stale"].channel_version.version, 2)
        self.assertEqual(d.status, "denied")  # 仅剩 device 一个来源

    def test_rebind_boundary_start_inclusive(self):
        """改绑发生时刻起确认归属新版本（区间左闭右开）。"""
        engine = make_engine([
            email_initial(),
            ChannelRebind("rb-email-2", t(2), t(2), "email", "b@x.com",
                          previous_value="a@x.com"),
            EmailConfirmation("ec-at-boundary", t(2), t(2), "b@x.com"),
            device_initial(),
            DeviceConfirmation("dc-1", t(3), t(3), "dev-1"),
            RecoveryRequest("req", t(4), t(4), "high"),
        ])
        d = engine.evaluate("req")
        links = {l.proof_event_id: l for l in d.evidence_chain.links}
        self.assertTrue(links["ec-at-boundary"].accepted)
        self.assertEqual(links["ec-at-boundary"].channel_version.version, 2)

    def test_old_value_at_boundary_rejected(self):
        """改绑发生时刻发往旧地址的确认不再有效。"""
        engine = make_engine([
            email_initial(),
            ChannelRebind("rb-email-2", t(2), t(2), "email", "b@x.com",
                          previous_value="a@x.com"),
            EmailConfirmation("ec-at-boundary", t(2), t(2), "a@x.com"),
            device_initial(),
            DeviceConfirmation("dc-1", t(3), t(3), "dev-1"),
            RecoveryRequest("req", t(4), t(4), "high"),
        ])
        d = engine.evaluate("req")
        links = {l.proof_event_id: l for l in d.evidence_chain.links}
        self.assertFalse(links["ec-at-boundary"].accepted)
        self.assertEqual(d.status, "denied")

    def test_proof_before_any_binding_rejected(self):
        """渠道尚无任何绑定版本时的确认无法归属。"""
        engine = make_engine([
            EmailConfirmation("ec-early", t(0), t(0), "a@x.com"),
            email_initial(),  # t(0) 才初始绑定？改为更晚
            device_initial(),
            DeviceConfirmation("dc-1", t(3), t(3), "dev-1"),
            RecoveryRequest("req", t(4), t(4), "high"),
        ])
        # ec-early 与初始绑定同时刻：归属 v1（左闭）。改用更早时刻构造无版本场景。
        engine2 = make_engine([
            EmailConfirmation("ec-early", t(-1), t(-1), "a@x.com"),
            email_initial(),
            device_initial(),
            DeviceConfirmation("dc-1", t(3), t(3), "dev-1"),
            RecoveryRequest("req", t(4), t(4), "high"),
        ])
        d = engine2.evaluate("req")
        links = {l.proof_event_id: l for l in d.evidence_chain.links}
        self.assertFalse(links["ec-early"].accepted)
        self.assertIsNone(links["ec-early"].channel_version)
        self.assertEqual(d.status, "denied")


if __name__ == "__main__":
    unittest.main()
