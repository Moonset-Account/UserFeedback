"""Legacy (pre-provenance) bindings: suspended on split, never guessed."""

from __future__ import annotations

import unittest

from support import RegistryTestCase


class LegacyBindingTests(RegistryTestCase):
    def _internal(self, subject: str) -> str:
        return self.reg.account_id(subject)

    def test_legacy_binding_survives_plain_login(self):
        self.login("hospital", "hospital-idp", "alice")
        legacy = self.reg.import_legacy_binding("old-partner", self._internal("alice"))
        self.assertTrue(legacy.startswith("psn_"))
        self.assertEqual(self.login("old-partner", "legacy-idp", "alice"), legacy)

    def test_legacy_binding_suspended_by_split(self):
        self.login("hospital", "hospital-idp", "alice")
        self.login("hospital", "hospital-idp", "bob")
        legacy = self.reg.import_legacy_binding("old-partner", self._internal("alice"))
        self.reg.merge("alice", "bob")

        report = self.reg.split("alice")
        self.assertIn(legacy, report.suspended)
        self.assertNotIn(legacy, report.restored["alice"])
        self.assertNotIn(legacy, report.restored["bob"])

        self.assertIn(legacy, self.reg.suspended_handles("old-partner"))
        self.assertNotIn(legacy, self.reg.active_handles("old-partner"))
        self.assertIsNone(self.reg.handle_for("old-partner", "alice"))

        info = self.reg.explain("old-partner", legacy)
        self.assertEqual(info["status"], "suspended")
        self.assertTrue(info["legacy"])

    def test_suspended_legacy_is_not_served_on_relogin(self):
        self.login("hospital", "hospital-idp", "alice")
        self.login("hospital", "hospital-idp", "bob")
        legacy = self.reg.import_legacy_binding("old-partner", self._internal("alice"))
        self.reg.merge("alice", "bob")
        self.reg.split("alice")

        fresh = self.login("old-partner", "legacy-idp", "bob")
        self.assertNotEqual(fresh, legacy)
        self.assertIn(legacy, self.reg.suspended_handles("old-partner"))

    def test_operator_adopts_legacy_binding(self):
        self.login("hospital", "hospital-idp", "alice")
        self.login("hospital", "hospital-idp", "bob")
        legacy = self.reg.import_legacy_binding("old-partner", self._internal("alice"))
        self.reg.merge("alice", "bob")
        self.reg.split("alice")

        self.reg.adopt_legacy("old-partner", legacy, "bob")
        self.assertEqual(self.login("old-partner", "legacy-idp", "bob"), legacy)
        self.assertIsNone(self.reg.handle_for("old-partner", "alice"))
        info = self.reg.explain("old-partner", legacy)
        self.assertEqual(info["status"], "active")
        self.assertEqual(info["initiator"], "bob")
        self.assertFalse(info["legacy"])

    def test_adopt_requires_suspended_legacy(self):
        self.login("hospital", "hospital-idp", "alice")
        regular = self.login("old-partner", "idp", "alice")
        with self.assertRaises(ValueError):
            self.reg.adopt_legacy("old-partner", regular, "alice")
        with self.assertRaises(KeyError):
            self.reg.adopt_legacy("old-partner", "psn_missing", "alice")

        legacy = self.reg.import_legacy_binding("legacy-only", self._internal("alice"))
        with self.assertRaises(ValueError):
            # Still active: cannot be adopted yet.
            self.reg.adopt_legacy("legacy-only", legacy, "alice")

    def test_import_legacy_is_idempotent_and_exclusive(self):
        self.login("hospital", "hospital-idp", "alice")
        internal = self._internal("alice")
        first = self.reg.import_legacy_binding("old", internal)
        self.assertEqual(self.reg.import_legacy_binding("old", internal), first)
        with self.assertRaises(ValueError):
            self.reg.import_legacy_binding("hospital", internal)
        with self.assertRaises(KeyError):
            self.reg.import_legacy_binding("old", "acct_missing")

    def test_legacy_on_single_identity_keeps_serving(self):
        self.login("hospital", "hospital-idp", "alice")
        legacy = self.reg.import_legacy_binding("old", self._internal("alice"))
        # A regular partner login must not silently replace the legacy handle.
        self.assertEqual(self.login("old", "idp", "alice"), legacy)
        self.assertEqual(self.reg.explain("old", legacy)["status"], "active")


if __name__ == "__main__":
    unittest.main()
