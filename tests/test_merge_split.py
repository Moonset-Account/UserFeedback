"""Merge, split and restoration semantics."""

from __future__ import annotations

import unittest

from support import RegistryTestCase


class MergeSplitTests(RegistryTestCase):
    def test_merge_is_visible_and_reversible(self):
        h_alice = self.login("hospital", "hospital-idp", "alice")
        h_bob = self.login("hospital", "hospital-idp", "bob")
        self.reg.merge("alice", "bob")
        self.assertEqual(self.reg.merged_subjects(), [["alice", "bob"]])

        # While merged the partner sees one identity: the earliest handle.
        merged_view = self.login("hospital", "hospital-idp", "bob")
        self.assertEqual(merged_view, h_alice)
        self.assertEqual(self.login("hospital", "hospital-idp", "alice"), h_alice)

        report = self.reg.split("alice")
        self.assertEqual(report.subject_ids, ["alice", "bob"])
        self.assertEqual(sorted(report.restored["alice"]), sorted([h_alice]))
        self.assertEqual(sorted(report.restored["bob"]), sorted([h_bob]))
        self.assertEqual(report.suspended, [])

        # Handles are never renamed: each subject gets its own one back.
        self.assertEqual(self.login("hospital", "hospital-idp", "alice"), h_alice)
        self.assertEqual(self.login("hospital", "hospital-idp", "bob"), h_bob)
        self.assertEqual(self.reg.merged_subjects(), [])

    def test_merge_keeps_each_partners_existing_handle(self):
        insurance_bob = self.login("insurer", "insurer-idp", "bob")
        self.login("hospital", "hospital-idp", "alice")
        self.reg.merge("alice", "bob")

        # The insurer only knew bob; it keeps seeing bob's own handle.
        self.assertEqual(self.login("insurer", "insurer-idp", "alice"),
                         insurance_bob)
        self.reg.split("bob")
        self.assertEqual(self.login("insurer", "insurer-idp", "bob"),
                         insurance_bob)

    def test_binding_created_during_merge_records_initiator(self):
        self.login("hospital", "hospital-idp", "alice")
        self.login("hospital", "hospital-idp", "bob")
        self.reg.merge("alice", "bob")

        # A partner new to both members registers during the merge, initiated
        # by bob.
        during = self.login("clinic", "clinic-idp", "bob")
        self.assertEqual(self.login("clinic", "clinic-idp", "alice"), during)

        report = self.reg.split("bob")
        self.assertIn(during, report.restored["bob"])
        self.assertNotIn(during, report.restored["alice"])

        # After the split the clinic's merged handle belongs to bob only; alice
        # gets a fresh handle of her own.
        self.assertEqual(self.login("clinic", "clinic-idp", "bob"), during)
        alice_after = self.login("clinic", "clinic-idp", "alice")
        self.assertNotEqual(alice_after, during)

        lineage = self.reg.explain("clinic", during)
        self.assertEqual(lineage["initiator"], "bob")
        # The binding was created while the identity was merged, so it records
        # that phase, and the split restored it.
        self.assertEqual(lineage["created_phase"], "merged")
        self.assertEqual([e["event"] for e in lineage["events"]
                          if e["event"] == "restored"], ["restored"])

    def test_split_restores_only_to_owning_subject(self):
        self.login("hospital", "hospital-idp", "alice")
        self.login("hospital", "hospital-idp", "bob")
        self.reg.merge("alice", "bob")
        by_alice = self.login("lab", "lab-idp", "alice")
        by_bob = self.login("lab", "lab-idp", "bob")
        # One merged identity, therefore one handle.
        self.assertEqual(by_alice, by_bob)

        report = self.reg.split("alice")
        self.assertIn(by_alice, report.restored["alice"])
        self.assertNotIn(by_alice, report.restored["bob"])
        self.assertEqual(self.reg.handle_for("lab", "bob"), None)
        self.assertEqual(self.reg.handle_for("lab", "alice"), by_alice)

    def test_merge_rejects_silly_input(self):
        self.login("hospital", "hospital-idp", "alice")
        self.login("hospital", "hospital-idp", "bob")
        with self.assertRaises(ValueError):
            self.reg.merge("alice", "alice")
        with self.assertRaises(KeyError):
            self.reg.merge("alice", "nobody")
        self.reg.merge("alice", "bob")
        with self.assertRaises(ValueError):
            self.reg.merge("alice", "bob")
        with self.assertRaises(KeyError):
            self.reg.split("nobody")
        self.reg.split("alice")
        with self.assertRaises(ValueError):
            self.reg.split("alice")  # already split

    def test_three_way_merge_and_split(self):
        h = {s: self.login("hospital", "hospital-idp", s)
             for s in ("a", "b", "c")}
        self.reg.merge("a", "b")
        self.reg.merge("a", "c")
        self.assertEqual(self.reg.merged_subjects(), [["a", "b", "c"]])
        merged_handle = self.login("hospital", "hospital-idp", "c")
        self.assertEqual(merged_handle, h["a"])

        report = self.reg.split("b")
        self.assertEqual(sorted(report.subject_ids), ["a", "b", "c"])
        self.assertEqual(report.suspended, [])
        for subject in ("a", "b", "c"):
            self.assertEqual(self.login("hospital", "hospital-idp", subject),
                             h[subject])

    def test_split_requires_merged_identity(self):
        self.login("hospital", "hospital-idp", "alice")
        with self.assertRaises(ValueError):
            self.reg.split("alice")


if __name__ == "__main__":
    unittest.main()
