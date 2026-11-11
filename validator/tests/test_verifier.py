"""Regression tests for the handle-authorization verifier.

The four acceptance scenarios from the task brief are covered both as the
shipped example logs and as inline-built histories, plus their negative
variants and the arrival-order invariance property.
"""

import json
import subprocess
import sys
import unittest
from itertools import permutations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from handle_audit import Verifier, Verdict, load_events  # noqa: E402
from handle_audit.report import render_json  # noqa: E402

EXAMPLES = ROOT / "examples"


def ev(eid, etype, **kw):
    base = {"event_id": eid, "type": etype}
    base.update(kw)
    return base


def run(records):
    return Verifier(load_events(records)).run()


def rule_ids(report):
    return {f.rule_id for f in report.findings}


def happy_path():
    """One clean task: create -> bind -> validate -> commit."""
    return [
        ev("e1", "HANDLE_CREATE", thread="T", seq=1, task="K", handle="H"),
        ev("e2", "BIND", thread="T", seq=2, task="K", handle="H",
           object_no="O", object_gen=1),
        ev("e3", "VALIDATE_OK", thread="T", seq=3, task="K", handle="H",
           object_no="O", object_gen=1),
        ev("e4", "WRITE_COMMIT", thread="T", seq=4, task="K", handle="H",
           object_no="O", object_gen=1),
    ]


class AcceptanceExampleTests(unittest.TestCase):
    def _load(self, name):
        with open(EXAMPLES / name, encoding="utf-8") as fh:
            return load_events(json.load(fh))

    def test_01_parallel_tasks_do_not_interfere(self):
        report = Verifier(self._load("01_parallel_isolation.json")).run()
        self.assertEqual(report.verdict, Verdict.PASS)
        self.assertEqual(report.findings, [])

    def test_02_cancel_then_new_task_revalidates(self):
        report = Verifier(
            self._load("02_cancel_then_revalidate.json")
        ).run()
        self.assertEqual(report.verdict, Verdict.PASS)
        self.assertEqual(report.findings, [])

    def test_03_same_task_stale_generation_is_violation(self):
        report = Verifier(self._load("03_stale_generation.json")).run()
        self.assertEqual(report.verdict, Verdict.VIOLATION)
        self.assertTrue(
            rule_ids(report)
            & {"commit_stale_rebind", "commit_unbound_identity"}
        )

    def test_04_missing_sync_is_undecidable_not_pass(self):
        report = Verifier(self._load("04_missing_sync.json")).run()
        self.assertEqual(report.verdict, Verdict.UNDECIDABLE)
        ids = rule_ids(report)
        self.assertIn("missing_sync_record", ids)
        self.assertIn("commit_validation_unordered", ids)
        # The structural issue list must name the lost predecessor.
        self.assertTrue(
            any("evt-validation-ack-that-was-never-shipped" in s
                for s in report.structural_issues)
        )

    def test_all_example_logs_are_invariant_under_shuffle(self):
        import random

        rng = random.Random(424242)
        for path in sorted(EXAMPLES.glob("*.json")):
            with self.subTest(example=path.name):
                records = json.loads(path.read_text(encoding="utf-8"))
                baseline = render_json(
                    Verifier(load_events(records)).run()
                )
                for _ in range(30):
                    shuffled = records[:]
                    rng.shuffle(shuffled)
                    self.assertEqual(
                        render_json(Verifier(load_events(shuffled)).run()),
                        baseline,
                        f"{path.name} verdict depends on arrival order",
                    )


class ParallelIsolationTests(unittest.TestCase):
    def _two_tasks(self):
        return [
            ev("a1", "HANDLE_CREATE", thread="T1", seq=1, task="A",
               handle="HA"),
            ev("a2", "BIND", thread="T1", seq=2, task="A", handle="HA",
               object_no="OA", object_gen=1),
            ev("a3", "VALIDATE_OK", thread="T1", seq=3, task="A",
               handle="HA", object_no="OA", object_gen=1),
            ev("a4", "WRITE_COMMIT", thread="T1", seq=4, task="A",
               handle="HA", object_no="OA", object_gen=1),
            ev("b1", "HANDLE_CREATE", thread="T2", seq=1, task="B",
               handle="HB"),
            ev("b2", "BIND", thread="T2", seq=2, task="B", handle="HB",
               object_no="OB", object_gen=1),
            ev("b3", "VALIDATE_OK", thread="T2", seq=3, task="B",
               handle="HB", object_no="OB", object_gen=1),
            ev("b4", "WRITE_COMMIT", thread="T2", seq=4, task="B",
               handle="HB", object_no="OB", object_gen=1),
        ]

    def test_independent_tasks_both_pass(self):
        report = run(self._two_tasks())
        self.assertEqual(report.verdict, Verdict.PASS)

    def test_cross_task_handle_borrow_is_violation(self):
        # Task B commits through A's handle without a B validation on it.
        records = self._two_tasks()
        records.append(
            ev("b5", "WRITE_COMMIT", thread="T2", seq=5, task="B",
               handle="HA", object_no="OA", object_gen=1)
        )
        report = run(records)
        self.assertEqual(report.verdict, Verdict.VIOLATION)
        self.assertIn("commit_without_validation", rule_ids(report))

    def test_cross_thread_commit_needs_sync_edge(self):
        # Producer validates on T1, consumer commits on T2 with no edge:
        # ordering is unprovable, hence undecidable.
        records = [
            ev("p1", "HANDLE_CREATE", thread="T1", seq=1, task="K",
               handle="H"),
            ev("p2", "BIND", thread="T1", seq=2, task="K", handle="H",
               object_no="O", object_gen=1),
            ev("p3", "VALIDATE_OK", thread="T1", seq=3, task="K",
               handle="H", object_no="O", object_gen=1),
            ev("c1", "WRITE_COMMIT", thread="T2", seq=1, task="K",
               handle="H", object_no="O", object_gen=1),
        ]
        report = run(records)
        self.assertEqual(report.verdict, Verdict.UNDECIDABLE)
        self.assertIn("commit_validation_unordered", rule_ids(report))

        # Adding the missing synchronization edge turns it into a PASS.
        records[-1]["depends_on"] = ["p3"]
        report2 = run(records)
        self.assertEqual(report2.verdict, Verdict.PASS)

    def test_sync_signal_wait_establishes_order(self):
        records = [
            ev("p1", "HANDLE_CREATE", thread="T1", seq=1, task="K",
               handle="H"),
            ev("p2", "BIND", thread="T1", seq=2, task="K", handle="H",
               object_no="O", object_gen=1),
            ev("p3", "VALIDATE_OK", thread="T1", seq=3, task="K",
               handle="H", object_no="O", object_gen=1),
            ev("p4", "SYNC", thread="T1", seq=4, sync_id="x",
               sync_op="signal"),
            ev("w1", "SYNC", thread="T2", seq=1, sync_id="x",
               sync_op="wait"),
            ev("w2", "WRITE_COMMIT", thread="T2", seq=2, task="K",
               handle="H", object_no="O", object_gen=1),
        ]
        report = run(records)
        self.assertEqual(report.verdict, Verdict.PASS)


class CancelRetryTests(unittest.TestCase):
    def test_cancel_revalidate_retry_commit_passes(self):
        records = [
            ev("1", "HANDLE_CREATE", thread="T", seq=1, task="K",
               handle="H"),
            ev("2", "BIND", thread="T", seq=2, task="K", handle="H",
               object_no="O", object_gen=1),
            ev("3", "VALIDATE_OK", thread="T", seq=3, task="K",
               handle="H", object_no="O", object_gen=1),
            ev("4", "CANCEL", thread="T", seq=4, task="K", handle="H"),
            ev("5", "RETRY", thread="T", seq=5, task="K", handle="H"),
            ev("6", "VALIDATE_OK", thread="T", seq=6, task="K",
               handle="H", object_no="O", object_gen=1),
            ev("7", "WRITE_COMMIT", thread="T", seq=7, task="K",
               handle="H", object_no="O", object_gen=1),
        ]
        self.assertEqual(run(records).verdict, Verdict.PASS)

    def test_commit_after_cancel_without_revalidation_violates(self):
        records = [
            ev("1", "HANDLE_CREATE", thread="T", seq=1, task="K",
               handle="H"),
            ev("2", "BIND", thread="T", seq=2, task="K", handle="H",
               object_no="O", object_gen=1),
            ev("3", "VALIDATE_OK", thread="T", seq=3, task="K",
               handle="H", object_no="O", object_gen=1),
            ev("4", "CANCEL", thread="T", seq=4, task="K", handle="H"),
            ev("5", "WRITE_COMMIT", thread="T", seq=5, task="K",
               handle="H", object_no="O", object_gen=1),
        ]
        report = run(records)
        self.assertEqual(report.verdict, Verdict.VIOLATION)
        self.assertIn("commit_after_cancel", rule_ids(report))

    def test_retry_without_fresh_validation_violates(self):
        records = [
            ev("1", "HANDLE_CREATE", thread="T", seq=1, task="K",
               handle="H"),
            ev("2", "BIND", thread="T", seq=2, task="K", handle="H",
               object_no="O", object_gen=1),
            ev("3", "VALIDATE_OK", thread="T", seq=3, task="K",
               handle="H", object_no="O", object_gen=1),
            ev("4", "CANCEL", thread="T", seq=4, task="K", handle="H"),
            ev("5", "RETRY", thread="T", seq=5, task="K", handle="H"),
            ev("6", "WRITE_COMMIT", thread="T", seq=6, task="K",
               handle="H", object_no="O", object_gen=1),
        ]
        report = run(records)
        self.assertEqual(report.verdict, Verdict.VIOLATION)
        self.assertIn(
            "commit_after_retry_without_revalidation", rule_ids(report)
        )

    def test_retry_without_cancel_is_violation(self):
        records = happy_path()[:3] + [
            ev("r", "RETRY", thread="T", seq=4, task="K", handle="H"),
        ]
        report = run(records)
        self.assertIn("retry_without_cancel", rule_ids(report))

    def test_cross_thread_cancel_without_edge_is_undecidable(self):
        # The cancel may or may not have happened before the commit: without
        # a synchronization edge the verifier must not guess.
        records = [
            ev("1", "HANDLE_CREATE", thread="T1", seq=1, task="K",
               handle="H"),
            ev("2", "BIND", thread="T1", seq=2, task="K", handle="H",
               object_no="O", object_gen=1),
            ev("3", "VALIDATE_OK", thread="T1", seq=3, task="K",
               handle="H", object_no="O", object_gen=1),
            ev("4", "CANCEL", thread="T2", seq=1, task="K", handle="H"),
            ev("5", "WRITE_COMMIT", thread="T1", seq=4, task="K",
               handle="H", object_no="O", object_gen=1),
        ]
        report = run(records)
        self.assertEqual(report.verdict, Verdict.UNDECIDABLE)
        self.assertIn("cancel_create_unordered", rule_ids(report))

        # With full edges proving validation -> cancel -> commit, it becomes
        # a concrete violation. Without the first sync point ordering the
        # cancel after the validation, the case stays undecidable above.
        records = [
            ev("1", "HANDLE_CREATE", thread="T1", seq=1, task="K",
               handle="H"),
            ev("2", "BIND", thread="T1", seq=2, task="K", handle="H",
               object_no="O", object_gen=1),
            ev("3", "VALIDATE_OK", thread="T1", seq=3, task="K",
               handle="H", object_no="O", object_gen=1),
            ev("sig-v", "SYNC", thread="T1", seq=4, sync_id="v",
               sync_op="signal"),
            ev("wait-v", "SYNC", thread="T2", seq=1, sync_id="v",
               sync_op="wait"),
            ev("4", "CANCEL", thread="T2", seq=2, task="K", handle="H"),
            ev("sig-c", "SYNC", thread="T2", seq=3, sync_id="c",
               sync_op="signal"),
            ev("wait-c", "SYNC", thread="T1", seq=5, sync_id="c",
               sync_op="wait"),
            ev("5", "WRITE_COMMIT", thread="T1", seq=6, task="K",
               handle="H", object_no="O", object_gen=1),
        ]
        report2 = run(records)
        self.assertEqual(report2.verdict, Verdict.VIOLATION)
        self.assertIn("commit_after_cancel", rule_ids(report2))


class GenerationTests(unittest.TestCase):
    def _rebind_history(self, commit_gen):
        return [
            ev("1", "HANDLE_CREATE", thread="T", seq=1, task="K",
               handle="H"),
            ev("2", "BIND", thread="T", seq=2, task="K", handle="H",
               object_no="O", object_gen=1),
            ev("3", "VALIDATE_OK", thread="T", seq=3, task="K",
               handle="H", object_no="O", object_gen=1),
            ev("4", "BIND", thread="T", seq=4, task="K", handle="H",
               object_no="O", object_gen=2),
            ev("5", "VALIDATE_OK", thread="T", seq=5, task="K",
               handle="H", object_no="O", object_gen=2),
            ev("6", "WRITE_COMMIT", thread="T", seq=6, task="K",
               handle="H", object_no="O", object_gen=commit_gen),
        ]

    def test_committing_old_generation_violates(self):
        report = run(self._rebind_history(1))
        self.assertEqual(report.verdict, Verdict.VIOLATION)
        # The old generation was revalidated after the rebind, then
        # invalidated again by the rebind: either rule id proves the
        # violation, commit_stale_rebind being the more specific one.
        self.assertTrue(
            rule_ids(report)
            & {"commit_stale_rebind", "commit_unbound_identity"}
        )

    def test_committing_current_generation_passes(self):
        report = run(self._rebind_history(2))
        self.assertEqual(report.verdict, Verdict.PASS)

    def test_validate_identity_mismatch_violates(self):
        records = happy_path()
        records[2]["object_no"] = "OTHER"
        report = run(records)
        self.assertEqual(report.verdict, Verdict.VIOLATION)
        self.assertIn("validate_identity_mismatch", rule_ids(report))

    def test_validate_stale_generation_violates(self):
        records = [
            ev("1", "HANDLE_CREATE", thread="T", seq=1, task="K",
               handle="H"),
            ev("2", "BIND", thread="T", seq=2, task="K", handle="H",
               object_no="O", object_gen=1),
            ev("3", "BIND", thread="T", seq=3, task="K", handle="H",
               object_no="O", object_gen=2),
            ev("4", "VALIDATE_OK", thread="T", seq=4, task="K",
               handle="H", object_no="O", object_gen=1),
        ]
        report = run(records)
        self.assertIn("validate_stale_generation", rule_ids(report))

    def test_concurrent_rebinds_make_commit_undecidable(self):
        records = [
            ev("1", "HANDLE_CREATE", thread="T1", seq=1, task="K",
               handle="H"),
            ev("2", "BIND", thread="T1", seq=2, task="K", handle="H",
               object_no="O", object_gen=1),
            ev("3", "SYNC", thread="T1", seq=3, sync_id="fork",
               sync_op="signal"),
            ev("4a", "BIND", thread="T2", seq=1, handle="H",
               object_no="O", object_gen=2),
            ev("4b", "BIND", thread="T3", seq=1, handle="H",
               object_no="P", object_gen=1),
            ev("5", "WRITE_COMMIT", thread="T1", seq=4, task="K",
               handle="H"),
        ]
        report = run(records)
        self.assertEqual(report.verdict, Verdict.UNDECIDABLE)
        self.assertIn("commit_binding_ambiguous", rule_ids(report))


class MissingSyncTests(unittest.TestCase):
    def test_dangling_depends_on_makes_commit_undecidable(self):
        records = [
            ev("1", "HANDLE_CREATE", thread="T1", seq=1, task="K",
               handle="H"),
            ev("2", "BIND", thread="T1", seq=2, task="K", handle="H",
               object_no="O", object_gen=1),
            ev("3", "VALIDATE_OK", thread="T1", seq=3, task="K",
               handle="H", object_no="O", object_gen=1),
            ev("4", "WRITE_COMMIT", thread="T2", seq=1, task="K",
               handle="H", object_no="O", object_gen=1,
               depends_on=["lost-ack"]),
        ]
        report = run(records)
        self.assertEqual(report.verdict, Verdict.UNDECIDABLE)
        ids = rule_ids(report)
        self.assertIn("missing_sync_record", ids)
        self.assertIn("commit_validation_unordered", ids)

    def test_same_history_with_complete_sync_passes(self):
        records = [
            ev("1", "HANDLE_CREATE", thread="T1", seq=1, task="K",
               handle="H"),
            ev("2", "BIND", thread="T1", seq=2, task="K", handle="H",
               object_no="O", object_gen=1),
            ev("3", "VALIDATE_OK", thread="T1", seq=3, task="K",
               handle="H", object_no="O", object_gen=1),
            ev("4", "WRITE_COMMIT", thread="T2", seq=1, task="K",
               handle="H", object_no="O", object_gen=1,
               depends_on=["3"]),
        ]
        self.assertEqual(run(records).verdict, Verdict.PASS)

    def test_dangling_sync_wait_is_undecidable(self):
        records = [
            ev("1", "HANDLE_CREATE", thread="T1", seq=1, task="K",
               handle="H"),
            ev("2", "BIND", thread="T1", seq=2, task="K", handle="H",
               object_no="O", object_gen=1),
            ev("3", "VALIDATE_OK", thread="T1", seq=3, task="K",
               handle="H", object_no="O", object_gen=1),
            ev("w", "SYNC", thread="T2", seq=1, sync_id="pt",
               sync_op="wait"),
            ev("4", "WRITE_COMMIT", thread="T2", seq=2, task="K",
               handle="H", object_no="O", object_gen=1),
        ]
        report = run(records)
        self.assertEqual(report.verdict, Verdict.UNDECIDABLE)
        self.assertIn("missing_sync_record", rule_ids(report))


class OrderingInvarianceTests(unittest.TestCase):
    """Same history, different arrival order => identical verdict."""

    HISTORIES = {
        "happy": happy_path(),
        "parallel": [
            ev("a1", "HANDLE_CREATE", thread="T1", seq=1, task="A",
               handle="HA"),
            ev("a2", "BIND", thread="T1", seq=2, task="A", handle="HA",
               object_no="OA", object_gen=1),
            ev("a3", "VALIDATE_OK", thread="T1", seq=3, task="A",
               handle="HA", object_no="OA", object_gen=1),
            ev("b1", "HANDLE_CREATE", thread="T2", seq=1, task="B",
               handle="HB"),
            ev("b2", "BIND", thread="T2", seq=2, task="B", handle="HB",
               object_no="OB", object_gen=1),
            ev("b3", "SYNC", thread="T2", seq=3, sync_id="z",
               sync_op="wait"),
            ev("a4", "SYNC", thread="T1", seq=4, sync_id="z",
               sync_op="signal"),
            ev("b4", "VALIDATE_OK", thread="T2", seq=4, task="B",
               handle="HB", object_no="OB", object_gen=1),
            ev("a5", "WRITE_COMMIT", thread="T1", seq=5, task="A",
               handle="HA", object_no="OA", object_gen=1),
            ev("b5", "WRITE_COMMIT", thread="T2", seq=5, task="B",
               handle="HB", object_no="OB", object_gen=1),
        ],
        "violation": [
            ev("1", "HANDLE_CREATE", thread="T", seq=1, task="K",
               handle="H"),
            ev("2", "BIND", thread="T", seq=2, task="K", handle="H",
               object_no="O", object_gen=1),
            ev("3", "VALIDATE_OK", thread="T", seq=3, task="K",
               handle="H", object_no="O", object_gen=1),
            ev("4", "CANCEL", thread="T", seq=4, task="K", handle="H"),
            ev("5", "WRITE_COMMIT", thread="T", seq=5, task="K",
               handle="H", object_no="O", object_gen=1),
        ],
        "unknown": [
            ev("1", "HANDLE_CREATE", thread="T1", seq=1, task="K",
               handle="H"),
            ev("2", "BIND", thread="T1", seq=2, task="K", handle="H",
               object_no="O", object_gen=1),
            ev("3", "VALIDATE_OK", thread="T1", seq=3, task="K",
               handle="H", object_no="O", object_gen=1),
            ev("4", "WRITE_COMMIT", thread="T2", seq=1, task="K",
               handle="H", object_no="O", object_gen=1,
               depends_on=["ghost"]),
        ],
    }

    def test_all_permutations_small_history(self):
        baseline = render_json(run(happy_path()))
        for order in permutations(happy_path()):
            self.assertEqual(render_json(run(list(order))), baseline)

    def test_reverse_and_rotations_match_baseline(self):
        for name, records in self.HISTORIES.items():
            with self.subTest(history=name):
                baseline = render_json(run(records))
                variants = [
                    list(reversed(records)),
                    records[len(records) // 2:] + records[: len(records) // 2],
                    records[1:] + records[:1],
                ]
                for variant in variants:
                    self.assertEqual(
                        render_json(run(variant)),
                        baseline,
                        f"history {name} verdict depends on arrival order",
                    )

    def test_shuffled_arrival_matches_baseline(self):
        import random

        rng = random.Random(20261112)
        for name, records in self.HISTORIES.items():
            with self.subTest(history=name):
                baseline = render_json(run(records))
                for _ in range(25):
                    shuffled = records[:]
                    rng.shuffle(shuffled)
                    self.assertEqual(
                        render_json(run(shuffled)),
                        baseline,
                        f"history {name} verdict changed under shuffle",
                    )


class StructuralAndRuleTests(unittest.TestCase):
    def test_unknown_handle_is_violation(self):
        report = run([
            ev("x", "BIND", thread="T", seq=1, task="K", handle="GHOST",
               object_no="O", object_gen=1),
        ])
        self.assertIn("unknown_handle", rule_ids(report))

    def test_commit_without_validation_violates(self):
        records = happy_path()
        records.pop(2)  # remove VALIDATE_OK
        report = run(records)
        self.assertIn("commit_without_validation", rule_ids(report))

    def test_commit_before_validation_violates(self):
        records = [
            ev("1", "HANDLE_CREATE", thread="T", seq=1, task="K",
               handle="H"),
            ev("2", "BIND", thread="T", seq=2, task="K", handle="H",
               object_no="O", object_gen=1),
            ev("3", "WRITE_COMMIT", thread="T", seq=3, task="K",
               handle="H", object_no="O", object_gen=1),
            ev("4", "VALIDATE_OK", thread="T", seq=4, task="K",
               handle="H", object_no="O", object_gen=1),
        ]
        report = run(records)
        self.assertEqual(report.verdict, Verdict.VIOLATION)
        self.assertIn("commit_validation_not_before", rule_ids(report))

    def test_hb_cycle_reported(self):
        records = [
            ev("a", "SYNC", thread="T1", seq=1, sync_id="p",
               sync_op="signal", depends_on=["b"]),
            ev("b", "SYNC", thread="T2", seq=1, sync_id="p",
               sync_op="wait"),
        ]
        report = run(records)
        self.assertIn("hb_cycle", rule_ids(report))

    def test_duplicate_thread_seq_is_undecidable(self):
        records = [
            ev("a", "HANDLE_CREATE", thread="T", seq=1, task="K",
               handle="H"),
            ev("b", "BIND", thread="T", seq=1, task="K", handle="H",
               object_no="O", object_gen=1),
        ]
        report = run(records)
        self.assertIn("seq_conflict", rule_ids(report))
        self.assertEqual(
            {f.verdict for f in report.findings}, {Verdict.UNDECIDABLE}
        )


class CliTests(unittest.TestCase):
    ENTRY = ROOT / "run_audit.py"

    def test_cli_exit_codes_and_output(self):
        cases = [
            ("01_parallel_isolation.json", 0, "PASS"),
            ("03_stale_generation.json", 1, "VIOLATION"),
            ("04_missing_sync.json", 2, "UNDECIDABLE"),
        ]
        for name, code, word in cases:
            with self.subTest(example=name):
                proc = subprocess.run(
                    [
                        sys.executable, "-I", str(self.ENTRY),
                        str(EXAMPLES / name),
                    ],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, code, proc.stderr)
                self.assertIn(f"VERDICT: {word}", proc.stdout)

    def test_cli_json_mode(self):
        proc = subprocess.run(
            [
                sys.executable, "-I", str(self.ENTRY),
                str(EXAMPLES / "01_parallel_isolation.json"), "--json",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["verdict"], "PASS")
        self.assertEqual(payload["findings"], [])

    def test_cli_missing_file_exit_code(self):
        proc = subprocess.run(
            [
                sys.executable, "-I", str(self.ENTRY),
                str(EXAMPLES / "does-not-exist.json"),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 3)
        self.assertIn("input error", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
