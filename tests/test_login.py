"""Basic pseudonym issuance: stability, isolation, enrolment."""

from __future__ import annotations

import unittest

from support import RegistryTestCase


class LoginTests(RegistryTestCase):
    def test_same_partner_same_subject_gets_same_handle(self):
        first = self.login("hospital", "hospital-idp", "person-1")
        second = self.login("hospital", "hospital-idp", "person-1")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("psn_"))

    def test_different_partners_get_different_handles(self):
        a = self.login("hospital", "hospital-idp", "person-1")
        b = self.login("insurer", "insurer-idp", "person-1")
        self.assertNotEqual(a, b)

    def test_different_subjects_get_different_handles(self):
        a = self.login("hospital", "hospital-idp", "person-1")
        b = self.login("hospital", "hospital-idp", "person-2")
        self.assertNotEqual(a, b)

    def test_login_enrolls_unknown_subject(self):
        self.assertEqual(self.reg.subjects(), [])
        self.login("hospital", "hospital-idp", "person-9")
        self.assertEqual(self.reg.subjects(), ["person-9"])

    def test_institution_required_and_recorded(self):
        handle = self.login("clinic", "clinic-idp", "person-3")
        self.assertTrue(handle)
        replay = self.reg.replay("clinic", only_registrations=True)
        self.assertEqual(len(replay), 1)
        self.assertEqual(replay[0]["institution"], "clinic-idp")

    def test_blank_partner_or_institution_rejected(self):
        with self.assertRaises(ValueError):
            self.reg.login("", "idp", "person-1")
        with self.assertRaises(ValueError):
            self.reg.login("partner", "   ", "person-1")

    def test_enroll_rejects_duplicates(self):
        self.reg.enroll("person-1")
        with self.assertRaises(ValueError):
            self.reg.enroll("person-1")

    def test_subject_ids_cannot_masquerade_as_internal_ids(self):
        for bad in ("acct_1", "grp_1", "psn_1", "sub_1"):
            with self.assertRaises(ValueError):
                self.reg.enroll(bad)
        with self.assertRaises(ValueError):
            self.reg.enroll("")

    def test_handle_for_and_account_id(self):
        handle = self.login("hospital", "hospital-idp", "person-1")
        self.assertEqual(self.reg.handle_for("hospital", "person-1"), handle)
        self.assertIsNone(self.reg.handle_for("insurer", "person-1"))
        internal = self.reg.account_id("person-1")
        self.assertTrue(internal.startswith("acct_"))

    def test_register_matches_login_handle(self):
        info = self.reg.register("hospital", "hospital-idp", "person-1")
        self.assertEqual(info["handle"], self.login(
            "hospital", "hospital-idp", "person-1"))
        self.assertEqual(info["created_phase"], "single")
        self.assertEqual(info["status"], "active")


if __name__ == "__main__":
    unittest.main()
