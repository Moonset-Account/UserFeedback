"""Handle lineage: every handle must be able to explain its evolution."""

from __future__ import annotations

import unittest

from support import RegistryTestCase


class LineageTests(RegistryTestCase):
    def test_single_handle_lineage(self):
        handle = self.login("hospital", "hospital-idp", "alice")
        self.login("hospital", "hospital-idp", "alice")
        info = self.reg.explain("hospital", handle)
        self.assertEqual(info["status"], "active")
        self.assertEqual(info["initiator"], "alice")
        self.assertEqual(info["created_phase"], "single")
        events = [e["event"] for e in info["events"]]
        self.assertEqual(events[0], "issued")
        self.assertEqual(events.count("seen"), 2)
        self.assertFalse(info["legacy"])

    def test_merged_handle_lineage_covers_merge_and_split(self):
        handle = self.login("hospital", "hospital-idp", "alice")
        self.login("hospital", "hospital-idp", "bob")
        self.reg.merge("alice", "bob")
        self.login("hospital", "hospital-idp", "bob")
        self.reg.split("alice")

        info = self.reg.explain("hospital", handle)
        events = [e["event"] for e in info["events"]]
        self.assertIn("merged", events)
        self.assertIn("restored", events)
        self.assertLess(events.index("merged"), events.index("restored"))
        self.assertEqual(info["status"], "active")

    def test_folded_handle_reports_merged_status(self):
        h_alice = self.login("hospital", "hospital-idp", "alice")
        h_bob = self.login("hospital", "hospital-idp", "bob")
        self.reg.merge("alice", "bob")
        # The merged identity is served under its earliest handle; the other
        # handle is still valid but folded in (status "merged").
        self.assertEqual(self.reg.explain("hospital", h_alice)["status"], "active")
        self.assertEqual(self.reg.explain("hospital", h_bob)["status"], "merged")

        self.reg.split("alice")
        self.assertEqual(self.reg.explain("hospital", h_alice)["status"], "active")
        self.assertEqual(self.reg.explain("hospital", h_bob)["status"], "active")

    def test_explain_for_partner_hides_subject(self):
        handle = self.login("hospital", "hospital-idp", "alice")
        partner_view = self.reg.explain_for_partner("hospital", handle)
        self.assertEqual(set(partner_view), {"handle", "status", "timeline"})
        self.assertNotIn("alice", str(partner_view))

    def test_explain_unknown_handle(self):
        with self.assertRaises(KeyError):
            self.reg.explain("hospital", "psn_nope")
        with self.assertRaises(KeyError):
            self.reg.explain_for_partner("hospital", "psn_nope")


if __name__ == "__main__":
    unittest.main()
