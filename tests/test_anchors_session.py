from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from topology_audit import AuditSession, Topology  # noqa: E402
from topology_audit.sample import sample_data  # noqa: E402


def identical_topology(prefix: str, count: int, device_type: str = "generic") -> Topology:
    return Topology.from_dict(
        {
            "devices": [
                {"id": f"{prefix}{i}", "type": device_type} for i in range(count)
            ]
        }
    )


class AnchorSessionTests(unittest.TestCase):
    def test_all_permutations_then_monotonic_confirmation(self) -> None:
        session = AuditSession(identical_topology("l", 3), identical_topology("o", 3))
        self.assertEqual(6, session.report.match_count)
        self.assertEqual(["o0", "o1", "o2"], session.report.candidates["l0"])

        first = session.confirm("l0", "o0")
        self.assertTrue(first.accepted)
        narrowed = first.session.report
        self.assertEqual(2, narrowed.match_count)
        self.assertEqual(6, len(narrowed.baseline_matches))
        self.assertEqual({"l0": "o0"}, narrowed.mapping)
        self.assertTrue(narrowed.candidates_are_narrowed_from(session.report))

        second = first.session.confirm("l1", "o1")
        self.assertTrue(second.accepted)
        final = second.session.report
        self.assertEqual(1, final.match_count)
        self.assertEqual({"l0": "o0", "l1": "o1", "l2": "o2"}, final.mapping)
        self.assertTrue(final.candidates_are_narrowed_from(narrowed))

    def test_conflicting_confirmation_is_rejected_and_does_not_widen(self) -> None:
        session = AuditSession(identical_topology("l", 2), identical_topology("o", 2))
        accepted = session.confirm("l0", "o0")
        self.assertTrue(accepted.accepted)
        before = accepted.session.report

        rejected = accepted.session.confirm("l0", "o1")
        self.assertFalse(rejected.accepted)
        self.assertEqual("rejected", rejected.feedback.status)
        self.assertEqual(before.candidates, rejected.session.report.candidates)
        self.assertEqual(1, rejected.session.report.match_count)
        self.assertTrue(rejected.report.candidates_are_narrowed_from(session.report))

    def test_used_observed_id_conflicts_with_later_anchor(self) -> None:
        session = AuditSession(identical_topology("l", 2), identical_topology("o", 2))
        result = session.confirm("l0", "o0")
        self.assertTrue(result.accepted)

        collision = result.session.confirm("l1", "o0")
        self.assertFalse(collision.accepted)
        self.assertEqual("rejected", collision.feedback.status)
        self.assertEqual(["l0->o0"], list(collision.feedback.conflicts_with))
        self.assertEqual(1, collision.session.report.match_count)
        self.assertEqual({"l0": "o0", "l1": "o1"}, collision.session.report.mapping)

    def test_frozen_joint_conflict_is_diagnosed(self) -> None:
        session = AuditSession.create(
            identical_topology("l", 2),
            identical_topology("o", 2),
            frozen={"l0": "o0", "l1": "o0"},
        )
        report = session.report
        self.assertEqual(1, report.match_count)
        self.assertEqual({"l0": "o0", "l1": "o1"}, report.mapping)
        self.assertEqual(
            [("l0->o0", "l1->o0")],
            [tuple(x) for x in report.conflict_sets],
        )
        self.assertEqual(1, len(report.rejected_anchors))

    def test_intrinsically_impossible_anchor_is_reported(self) -> None:
        data = sample_data()
        data["frozen"] = {"alpha-meter": "ttyUSB0"}  # valve, not meter

        session = AuditSession.create(**data)
        report = session.report

        self.assertEqual(1, report.match_count)
        self.assertEqual("ttyUSB1", report.mapping["alpha-meter"])
        self.assertEqual(1, len(report.unsatisfiable_anchors))
        feedback = report.unsatisfiable_anchors[0]
        self.assertEqual("alpha-meter", feedback.anchor.logical)
        self.assertEqual("ttyUSB0", feedback.anchor.observed)
        self.assertEqual(0, feedback.support_count)

    def test_unknown_anchor_endpoints_are_unsatisfiable_not_fatal(self) -> None:
        session = AuditSession(identical_topology("l", 1), identical_topology("o", 1))
        result = session.confirm("missing", "o0")
        self.assertFalse(result.accepted)
        self.assertEqual("unsatisfiable", result.feedback.status)
        self.assertIn("unknown logical device", result.feedback.reason or "")
        self.assertEqual(1, result.session.report.match_count)

    def test_interleaved_bad_and_good_confirmations_only_narrow(self) -> None:
        session = AuditSession(identical_topology("l", 3), identical_topology("o", 3))
        snapshots = [session.report.candidates.copy()]

        bad_type_or_value = session.confirm("l0", "does-not-exist")
        self.assertFalse(bad_type_or_value.accepted)
        current = bad_type_or_value.session
        snapshots.append(current.report.candidates.copy())

        good = current.confirm("l0", "o1")
        self.assertTrue(good.accepted)
        current = good.session
        snapshots.append(current.report.candidates.copy())

        conflict = current.confirm("l1", "o1")
        self.assertFalse(conflict.accepted)
        current = conflict.session
        snapshots.append(current.report.candidates.copy())

        last = current.confirm("l1", "o0")
        self.assertTrue(last.accepted)
        current = last.session

        self.assertEqual(1, current.report.match_count)
        self.assertEqual("o1", current.report.mapping["l0"])
        self.assertEqual("o0", current.report.mapping["l1"])
        self.assertEqual("o2", current.report.mapping["l2"])

        # Every accepted state remains a subset of the initial state; rejected
        # steps leave candidates exactly unchanged.
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[2], snapshots[3])
        for logical_id in ("l0", "l1", "l2"):
            self.assertTrue(
                set(current.report.candidates[logical_id]).issubset(
                    {"o0", "o1", "o2"}
                )
            )


if __name__ == "__main__":
    unittest.main()
