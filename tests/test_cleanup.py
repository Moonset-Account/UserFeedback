"""Unit tests for the two-step attachment cleanup (synthetic fixtures only)."""

import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cleanup_core as core  # noqa: E402

NOW = datetime.date(2026, 11, 14)


class CleanupTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sec005-test-")
        self.addCleanup(self._tmp.cleanup)
        self.data = self._tmp.name
        self.write_json(core.SPACES_FILE, {"spaces": {
            "support": {"retention_days": 365},
            "sales": {"retention_days": 30},
        }})
        self.write_json(core.HOLDS_FILE, {"holds": []})
        self.write_json(core.INDEX_FILE, {"attachments": []})

    # -- fixture helpers ----------------------------------------------------

    def write_json(self, rel, payload):
        path = os.path.join(self.data, rel)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def read_json(self, rel):
        with open(os.path.join(self.data, rel), "r", encoding="utf-8") as fh:
            return json.load(fh)

    def add_attachment(self, aid, case_id, space, created, version=1,
                       content=b"data", as_dir=False, create_file=True,
                       rel_path=None):
        rel = rel_path or os.path.join("files", case_id, aid + ".bin")
        abspath = os.path.join(self.data, rel)
        if create_file:
            os.makedirs(os.path.dirname(abspath), exist_ok=True)
            if as_dir:
                os.makedirs(abspath, exist_ok=True)
            else:
                with open(abspath, "wb") as fh:
                    fh.write(content)
        index = self.read_json(core.INDEX_FILE)
        index["attachments"].append({
            "attachment_id": aid,
            "case_id": case_id,
            "space": space,
            "path": rel,
            "version": version,
            "created_at": created,
        })
        self.write_json(core.INDEX_FILE, index)
        return abspath

    def add_hold(self, hold_id, scope, target_id, active=True):
        holds = self.read_json(core.HOLDS_FILE)
        holds["holds"].append({"hold_id": hold_id, "scope": scope,
                               "target_id": target_id, "active": active})
        self.write_json(core.HOLDS_FILE, holds)

    def set_retention(self, space, days):
        spaces = self.read_json(core.SPACES_FILE)
        spaces["spaces"][space]["retention_days"] = days
        self.write_json(core.SPACES_FILE, spaces)

    def bump_version(self, aid, new_version, new_content):
        index = self.read_json(core.INDEX_FILE)
        for att in index["attachments"]:
            if att["attachment_id"] == aid:
                att["version"] = new_version
                with open(os.path.join(self.data, att["path"]), "wb") as fh:
                    fh.write(new_content)
        self.write_json(core.INDEX_FILE, index)

    def remove_from_index(self, aid):
        index = self.read_json(core.INDEX_FILE)
        index["attachments"] = [
            a for a in index["attachments"] if a["attachment_id"] != aid]
        self.write_json(core.INDEX_FILE, index)

    def plan(self):
        return core.plan_confirmation(self.data, NOW)

    def execute(self):
        return core.execute_confirmed(self.data, NOW)

    def outcome(self, result, aid):
        for outcome in result["outcomes"]:
            if outcome["attachment_id"] == aid:
                return outcome
        self.fail(f"no outcome recorded for {aid}")


class PlanTests(CleanupTestBase):
    def test_plan_selects_only_expired(self):
        self.add_attachment("att-old", "case-1", "support", "2025-01-01", version=3)
        self.add_attachment("att-new", "case-1", "support", "2026-11-01")
        entries = self.plan()
        self.assertEqual([e["attachment_id"] for e in entries], ["att-old"])
        self.assertEqual(entries[0]["confirmed_version"], 3)

    def test_retention_boundary_exactly_expired(self):
        # created exactly 365 days before NOW -> expired (age >= retention)
        self.add_attachment("att-edge", "case-1", "support", "2025-11-14")
        # one day short -> still retained
        self.add_attachment("att-short", "case-1", "support", "2025-11-15")
        planned = {e["attachment_id"] for e in self.plan()}
        self.assertIn("att-edge", planned)
        self.assertNotIn("att-short", planned)

    def test_per_space_retention(self):
        # same age: expired under sales (30d) but not under support (365d)
        self.add_attachment("att-sales", "case-1", "sales", "2026-10-01")
        self.add_attachment("att-support", "case-1", "support", "2026-10-01")
        planned = {e["attachment_id"] for e in self.plan()}
        self.assertIn("att-sales", planned)
        self.assertNotIn("att-support", planned)

    def test_unknown_space_not_planned(self):
        self.add_attachment("att-x", "case-1", "archive", "2020-01-01")
        self.assertEqual(self.plan(), [])


class ExecuteDeleteTests(CleanupTestBase):
    def test_execute_deletes_file_and_updates_index(self):
        path = self.add_attachment("att-1", "case-1", "support", "2025-01-01")
        self.plan()
        result = self.execute()
        outcome = self.outcome(result, "att-1")
        self.assertEqual(outcome["status"], "deleted")
        self.assertIsNone(outcome["reason"])
        self.assertFalse(os.path.exists(path))
        remaining = self.read_json(core.INDEX_FILE)["attachments"]
        self.assertEqual(remaining, [])
        journal = self.read_json(core.JOURNAL_FILE)
        self.assertEqual(journal["entries"]["att-1"]["status"], "deleted")

    def test_result_file_written_with_counts(self):
        self.add_attachment("att-1", "case-1", "support", "2025-01-01")
        self.add_attachment("att-2", "case-2", "support", "2025-01-01")
        self.add_hold("H-1", "case", "case-2")
        self.plan()
        result = self.execute()
        self.assertEqual(result["counts"],
                         {"deleted": 1, "skipped": 1,
                          "pending_retry": 0, "pending": 0})
        on_disk = self.read_json(core.RESULT_FILE)
        self.assertEqual(on_disk["counts"], result["counts"])


class HoldTests(CleanupTestBase):
    def test_case_level_hold_blocks(self):
        path = self.add_attachment("att-1", "case-1", "support", "2025-01-01")
        self.add_hold("H-CASE", "case", "case-1")
        self.plan()
        outcome = self.outcome(self.execute(), "att-1")
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "legal_hold:H-CASE")
        self.assertTrue(os.path.exists(path))

    def test_attachment_level_hold_blocks(self):
        self.add_attachment("att-1", "case-1", "support", "2025-01-01")
        self.add_hold("H-ATT", "attachment", "att-1")
        self.plan()
        outcome = self.outcome(self.execute(), "att-1")
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "legal_hold:H-ATT")

    def test_any_of_multiple_holds_blocks(self):
        self.add_attachment("att-1", "case-1", "support", "2025-01-01")
        self.add_hold("H-OFF", "attachment", "att-1", active=False)
        self.add_hold("H-ON", "case", "case-1", active=True)
        self.plan()
        outcome = self.outcome(self.execute(), "att-1")
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "legal_hold:H-ON")

    def test_inactive_hold_does_not_block(self):
        self.add_attachment("att-1", "case-1", "support", "2025-01-01")
        self.add_hold("H-OFF", "case", "case-1", active=False)
        self.plan()
        self.assertEqual(self.outcome(self.execute(), "att-1")["status"], "deleted")

    def test_hold_added_after_confirm_skips(self):
        path = self.add_attachment("att-1", "case-1", "support", "2025-01-01")
        self.plan()
        # legal freeze arrives between confirmation and execution
        self.add_hold("H-NEW", "attachment", "att-1")
        outcome = self.outcome(self.execute(), "att-1")
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "legal_hold:H-NEW")
        self.assertTrue(os.path.exists(path))


class RecheckTests(CleanupTestBase):
    def test_version_change_after_confirm_skips_and_preserves(self):
        path = self.add_attachment("att-1", "case-1", "support", "2025-01-01",
                                   version=1, content=b"old")
        self.plan()
        # attachment replaced after confirmation -> new content version
        self.bump_version("att-1", 2, b"new-content")
        outcome = self.outcome(self.execute(), "att-1")
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "version_changed")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"new-content")

    def test_retention_rule_tightened_after_confirm_skips(self):
        self.add_attachment("att-1", "case-1", "support", "2025-01-01")
        self.plan()
        # retention rules re-read at execution time; now no longer expired
        self.set_retention("support", 10000)
        outcome = self.outcome(self.execute(), "att-1")
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "within_retention")

    def test_missing_from_index_skips(self):
        path = self.add_attachment("att-1", "case-1", "support", "2025-01-01")
        self.plan()
        self.remove_from_index("att-1")
        outcome = self.outcome(self.execute(), "att-1")
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "missing_from_index")
        self.assertTrue(os.path.exists(path))

    def test_file_missing_on_disk_skips(self):
        self.add_attachment("att-1", "case-1", "support", "2025-01-01",
                            create_file=False)
        self.plan()
        outcome = self.outcome(self.execute(), "att-1")
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "file_missing")

    def test_unsafe_path_skipped(self):
        self.add_attachment("att-1", "case-1", "support", "2025-01-01",
                            create_file=False, rel_path="../outside.bin")
        self.plan()
        outcome = self.outcome(self.execute(), "att-1")
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "unsafe_path")


class ResumeTests(CleanupTestBase):
    def test_pending_retry_on_io_error_then_resume_deletes(self):
        # index path points at a directory -> os.remove raises OSError
        path = self.add_attachment("att-1", "case-1", "support", "2025-01-01",
                                   as_dir=True)
        self.plan()
        first = self.execute()
        outcome = self.outcome(first, "att-1")
        self.assertEqual(outcome["status"], "pending_retry")
        self.assertTrue(outcome["reason"].startswith("io_error:"))
        # fix the transient problem and re-run: entry is retried
        shutil.rmtree(path)
        with open(path, "wb") as fh:
            fh.write(b"data")
        second = self.execute()
        retry = self.outcome(second, "att-1")
        self.assertEqual(retry["status"], "deleted")
        self.assertFalse(retry["from_journal"])
        self.assertFalse(os.path.exists(path))

    def test_rerun_continues_remaining_and_keeps_terminal_entries(self):
        path_a = self.add_attachment("att-a", "case-1", "support", "2025-01-01")
        path_b = self.add_attachment("att-b", "case-2", "support", "2025-01-01")
        self.add_hold("H-1", "case", "case-2")
        self.plan()
        first = self.execute()
        self.assertEqual(self.outcome(first, "att-a")["status"], "deleted")
        self.assertEqual(self.outcome(first, "att-b")["status"], "skipped")
        self.assertFalse(os.path.exists(path_a))
        # hold lifted afterwards; terminal entries are NOT re-processed:
        # reconsideration requires a new confirmation round
        self.write_json(core.HOLDS_FILE, {"holds": []})
        second = self.execute()
        again_a = self.outcome(second, "att-a")
        again_b = self.outcome(second, "att-b")
        self.assertTrue(again_a["from_journal"])
        self.assertTrue(again_b["from_journal"])
        self.assertEqual(again_a["status"], "deleted")
        self.assertEqual(again_b["status"], "skipped")
        self.assertTrue(os.path.exists(path_b))

    def test_interrupted_run_resumes_remaining_entries(self):
        path_a = self.add_attachment("att-a", "case-1", "support", "2025-01-01")
        path_b = self.add_attachment("att-b", "case-1", "support", "2025-01-01")
        self.plan()
        # simulate a crash after att-a was processed: journal only has att-a
        journal = {"entries": {"att-a": {
            "status": "deleted", "reason": None,
            "confirmed_version": 1, "updated_at": NOW.isoformat()}}}
        os.remove(path_a)  # att-a was really deleted before the crash
        self.remove_from_index("att-a")
        self.write_json(core.JOURNAL_FILE, journal)
        result = self.execute()
        self.assertTrue(self.outcome(result, "att-a")["from_journal"])
        outcome_b = self.outcome(result, "att-b")
        self.assertEqual(outcome_b["status"], "deleted")
        self.assertFalse(outcome_b["from_journal"])
        self.assertFalse(os.path.exists(path_b))
        self.assertEqual(result["counts"]["deleted"], 2)

    def test_new_plan_round_starts_fresh_journal(self):
        path = self.add_attachment("att-1", "case-1", "support", "2025-01-01")
        self.add_hold("H-1", "case", "case-1")
        self.plan()
        self.assertEqual(self.outcome(self.execute(), "att-1")["status"], "skipped")
        # operations lifts the hold and starts a NEW confirmation round
        self.write_json(core.HOLDS_FILE, {"holds": []})
        self.plan()
        outcome = self.outcome(self.execute(), "att-1")
        self.assertEqual(outcome["status"], "deleted")
        self.assertFalse(os.path.exists(path))

    def test_status_summary_distinguishes_categories(self):
        self.add_attachment("att-del", "case-1", "support", "2025-01-01")
        self.add_attachment("att-skip", "case-2", "support", "2025-01-01")
        self.add_attachment("att-retry", "case-3", "support", "2025-01-01",
                            as_dir=True)
        self.add_hold("H-1", "case", "case-2")
        self.plan()
        summary = core.status_summary(self.data)
        self.assertEqual(summary["counts"]["pending"], 3)
        self.execute()
        summary = core.status_summary(self.data)
        self.assertEqual(summary["counts"],
                         {"deleted": 1, "skipped": 1,
                          "pending_retry": 1, "pending": 0})
        self.assertEqual(summary["entries"]["att-skip"]["reason"],
                         "legal_hold:H-1")


if __name__ == "__main__":
    unittest.main()
