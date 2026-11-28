"""Partner isolation: exports and replays share no identifier between partners."""

from __future__ import annotations

import json
import unittest

from support import RegistryTestCase

PARTNER_SAFE_FIELDS = {"handle", "status", "lineage"}
REPLAY_SAFE_FIELDS = {"step", "action", "handle", "phase", "institution"}


def identifier_tokens(rows: list[dict]) -> set[str]:
    """Extract only identifier-shaped values (handles and internal ids).

    Generic status/lineage vocabulary such as ``active`` or ``registered`` is
    shared by design and is not an identifier.
    """
    out: set[str] = set()
    for row in rows:
        for value in row.values():
            if isinstance(value, list):
                candidates = (v for v in value if isinstance(v, str))
            elif isinstance(value, str):
                candidates = (value,)
            else:
                continue
            for item in candidates:
                if item.startswith(("psn_", "acct_", "grp_", "sub_")):
                    out.add(item)
    return out


def flat_strings(rows: list[dict]) -> set[str]:
    out: set[str] = set()
    for row in rows:
        for value in row.values():
            if isinstance(value, str):
                out.add(value)
            elif isinstance(value, list):
                out.update(v for v in value if isinstance(v, str))
    return out


class ExportTests(RegistryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.h_a = self.login("hospital", "hospital-idp", "alice")
        self.h_b = self.login("hospital", "hospital-idp", "bob")
        self.i_a = self.login("insurer", "insurer-idp", "alice")
        self.reg.merge("alice", "bob")
        self.c_a = self.login("clinic", "clinic-idp", "alice")
        self.reg.import_legacy_binding("legacy", self.reg.account_id("bob"))
        self.reg.split("bob")

    def test_export_has_only_partner_safe_fields(self):
        for partner in ("hospital", "insurer", "clinic", "legacy"):
            for row in self.reg.export_for_partner(partner):
                self.assertLessEqual(set(row), PARTNER_SAFE_FIELDS)

    def test_exports_of_two_partners_share_no_identifier(self):
        hospital = self.reg.export_for_partner("hospital")
        insurer = self.reg.export_for_partner("insurer")
        self.assertEqual(identifier_tokens(hospital) & identifier_tokens(insurer),
                         set())

    def test_export_contains_no_internal_or_subject_ids(self):
        internals = {self.reg.account_id("alice"), self.reg.account_id("bob")}
        export = self.reg.export_json("hospital")
        for internal in internals:
            self.assertNotIn(internal, export)
        for subject in ("alice", "bob"):
            self.assertNotIn(subject, export)
        for prefix in ("acct_", "grp_"):
            self.assertNotIn(prefix, export)
        # Only psn_ handles are allowed.
        rows = json.loads(export)
        for row in rows:
            self.assertTrue(row["handle"].startswith("psn_"))

    def test_export_is_per_partner(self):
        self.assertEqual({r["handle"] for r in self.reg.export_for_partner("hospital")},
                         {self.h_a, self.h_b})
        self.assertEqual({r["handle"] for r in self.reg.export_for_partner("insurer")},
                         {self.i_a})
        self.assertEqual([r["handle"] for r in self.reg.export_for_partner("nobody")],
                         [])

    def test_export_statuses_reflect_split(self):
        rows = {r["handle"]: r["status"]
                for r in self.reg.export_for_partner("hospital")}
        self.assertEqual(rows[self.h_a], "active")
        self.assertEqual(rows[self.h_b], "active")
        legacy = self.reg.suspended_handles("legacy")
        self.assertEqual(len(legacy), 1)
        legacy_rows = self.reg.export_for_partner("legacy")
        self.assertEqual(legacy_rows[0]["status"], "suspended")
        self.assertIn("suspended", legacy_rows[0]["lineage"])

    def test_export_requires_partner(self):
        with self.assertRaises(ValueError):
            self.reg.export_for_partner("")

    def test_two_partner_full_flow_shares_no_id(self):
        # Fresh registry, deliberately interleaved so global sequence numbers
        # would coincide if they leaked.
        reg = type(self.reg)()
        reg.login("hospital", "hospital-idp", "alice")
        reg.login("insurer", "insurer-idp", "alice")
        reg.login("hospital", "hospital-idp", "bob")
        reg.login("insurer", "insurer-idp", "bob")
        reg.merge("alice", "bob")
        reg.login("hospital", "hospital-idp", "alice")
        reg.login("insurer", "insurer-idp", "bob")
        reg.split("alice")
        for a in ("hospital", "insurer"):
            for b in ("hospital", "insurer"):
                if a >= b:
                    continue
                self.assertEqual(
                    identifier_tokens(reg.export_for_partner(a))
                    & identifier_tokens(reg.export_for_partner(b)), set())


class ReplayTests(RegistryTestCase):
    def test_replay_is_partner_scoped_and_ordered(self):
        self.login("hospital", "hospital-idp", "alice")
        self.login("insurer", "insurer-idp", "bob")
        self.login("hospital", "hospital-idp", "bob")

        rows = self.reg.replay("hospital")
        self.assertEqual([r["step"] for r in rows], list(range(1, len(rows) + 1)))
        self.assertTrue(all(r["institution"] == "hospital-idp" for r in rows))
        handles = {r["handle"] for r in rows}
        self.assertEqual(len(handles), 2)

        insurer_rows = self.reg.replay("insurer")
        self.assertEqual(identifier_tokens(insurer_rows) & identifier_tokens(rows),
                         set())

    def test_replay_has_only_partner_safe_fields(self):
        self.login("hospital", "hospital-idp", "alice")
        for row in self.reg.replay("hospital"):
            self.assertLessEqual(set(row), REPLAY_SAFE_FIELDS)
            self.assertNotIn("acct_", json.dumps(row))
            self.assertNotIn("alice", json.dumps(row))

    def test_replay_only_registrations(self):
        self.login("hospital", "hospital-idp", "alice")
        self.login("hospital", "hospital-idp", "alice")
        self.login("hospital", "hospital-idp", "bob")
        all_rows = self.reg.replay("hospital")
        regs = self.reg.replay("hospital", only_registrations=True)
        self.assertGreater(len(all_rows), len(regs))
        self.assertEqual([r["action"] for r in regs], ["registered", "registered"])
        self.assertEqual(len({r["handle"] for r in regs}), 2)

    def test_replay_shows_each_step_handle(self):
        h_alice = self.login("hospital", "hospital-idp", "alice")
        self.login("hospital", "hospital-idp", "bob")
        self.reg.merge("alice", "bob")
        self.login("hospital", "hospital-idp", "bob")  # sees alice's handle
        rows = self.reg.replay("hospital")
        self.assertEqual(rows[0]["handle"], h_alice)
        self.assertEqual(rows[-1]["handle"], h_alice)
        self.assertEqual(rows[-1]["phase"], "merged")

    def test_replay_unknown_partner_is_empty(self):
        self.assertEqual(self.reg.replay("nobody"), [])
        with self.assertRaises(ValueError):
            self.reg.replay("")


if __name__ == "__main__":
    unittest.main()
