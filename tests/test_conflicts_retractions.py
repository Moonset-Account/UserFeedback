from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from event_order import (
    OUTCOME_AFTER,
    OUTCOME_BEFORE,
    OUTCOME_CONFLICT,
    OrderAdjudicator,
)


class ConflictAndRetractionTests(unittest.TestCase):
    def _conflicted_case(self) -> OrderAdjudicator:
        adj = OrderAdjudicator()
        adj.add_observation(
            "OBS-CLOCK",
            source="independent-observer",
            initiation_event="device_a_send",
            receipt_event="device_b_recv",
            scope="incident-42",
        )
        adj.add_sequence(
            "device-b-log",
            ["device_b_recv", "device_a_send"],
            evidence_id="E-DEVICE-B",
            scope="incident-42",
        )
        adj.add_order_evidence(
            "E-OTHER",
            "third-source",
            "backup_start",
            "backup_complete",
            scope="incident-42",
        )
        return adj

    def test_same_scope_contradiction_is_listed_with_evidence_ids(self) -> None:
        adj = self._conflicted_case()

        conflicts = adj.conflicts(scope="incident-42")
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict.scope, "incident-42")
        self.assertEqual(
            set(conflict.evidence_ids),
            {"OBS-CLOCK", "E-DEVICE-B"},
        )
        self.assertEqual(
            set(conflict.source_ids),
            {"independent-observer", "device-b-log"},
        )

        verdict = adj.query(
            "device_a_send", "device_b_recv", scope="incident-42"
        )
        self.assertEqual(verdict.outcome, OUTCOME_CONFLICT)
        self.assertIsNotNone(verdict.forward_chain)
        self.assertIsNotNone(verdict.reverse_chain)
        self.assertEqual(len(verdict.conflicts), 1)
        self.assertEqual(
            set(verdict.conflicts[0].evidence_ids),
            {"OBS-CLOCK", "E-DEVICE-B"},
        )
        # A conflict verdict exposes both directional dependency analyses.
        self.assertIn(
            "independent-observer", verdict.forward_critical_sources
        )
        self.assertIn("device-b-log", verdict.reverse_critical_sources)
        json.dumps(verdict.to_dict())

    def test_other_independent_source_remains_queryable(self) -> None:
        adj = self._conflicted_case()

        other = adj.query("backup_start", "backup_complete", scope="incident-42")

        self.assertEqual(other.outcome, OUTCOME_BEFORE)
        self.assertEqual(other.evidence_ids, ["E-OTHER"])
        self.assertEqual(other.critical_sources, ("third-source",))
        # The unrelated query must not carry the A/B conflict.
        self.assertEqual(other.conflicts, ())

    def test_retracting_one_observation_reanswers_from_remaining_evidence(self) -> None:
        adj = self._conflicted_case()
        before = adj.query(
            "device_a_send", "device_b_recv", scope="incident-42"
        )
        self.assertEqual(before.outcome, OUTCOME_CONFLICT)

        record = adj.retract_evidence(
            "OBS-CLOCK",
            retraction_id="R-CORRECTION-1",
            reason="observation clock was misassociated",
            recorded_by="investigator",
        )

        self.assertEqual(record.target_evidence_id, "OBS-CLOCK")
        self.assertEqual(record.retraction_id, "R-CORRECTION-1")
        self.assertFalse(adj.is_active("OBS-CLOCK"))
        self.assertTrue(adj.is_active("E-DEVICE-B"))
        self.assertEqual(adj.conflicts(scope="incident-42"), [])

        after = adj.query(
            "device_a_send", "device_b_recv", scope="incident-42"
        )
        self.assertEqual(after.outcome, OUTCOME_AFTER)
        self.assertIsNotNone(after.chain)
        assert after.chain is not None
        self.assertEqual(after.chain.evidence_ids, ["E-DEVICE-B"])
        self.assertEqual(after.critical_sources, ("device-b-log",))
        self.assertEqual(len(adj.retractions), 1)
        self.assertEqual(adj.retractions[0].retraction_id, "R-CORRECTION-1")

        # Independent evidence is still available after the correction.
        other = adj.query("backup_start", "backup_complete", scope="incident-42")
        self.assertEqual(other.outcome, OUTCOME_BEFORE)

    def test_dynamic_add_retract_and_readd_interleaving(self) -> None:
        adj = OrderAdjudicator()
        adj.add_order_evidence("E1", "source-1", "a", "b")
        self.assertEqual(adj.query("a", "b").outcome, OUTCOME_BEFORE)

        adj.add_order_evidence("E2", "source-2", "b", "a")
        self.assertEqual(adj.query("a", "b").outcome, OUTCOME_CONFLICT)
        self.assertEqual(len(adj.conflicts()), 1)

        adj.retract_evidence("E1", "R1")
        self.assertEqual(adj.query("a", "b").outcome, OUTCOME_AFTER)
        self.assertEqual(adj.conflicts(), [])

        adj.add_order_evidence("E3", "source-3", "a", "b")
        renewed = adj.query("a", "b")
        self.assertEqual(renewed.outcome, OUTCOME_CONFLICT)
        self.assertEqual(
            {eid for c in renewed.conflicts for eid in c.evidence_ids},
            {"E2", "E3"},
        )

    def test_transitive_cycle_is_terminating_and_reports_both_chains(self) -> None:
        adj = OrderAdjudicator()
        adj.add_order_evidence("E1", "s1", "a", "x")
        adj.add_order_evidence("E2", "s2", "x", "b")
        adj.add_order_evidence("E3", "s3", "b", "y")
        adj.add_order_evidence("E4", "s4", "y", "a")

        verdict = adj.query("a", "b")

        self.assertEqual(verdict.outcome, OUTCOME_CONFLICT)
        self.assertEqual(verdict.forward_chain.evidence_ids, ["E1", "E2"])
        self.assertEqual(verdict.reverse_chain.evidence_ids, ["E3", "E4"])
        # No direct pairwise reverse claim exists; the two chains themselves
        # expose all involved IDs without BFS looping on the cycle.
        self.assertEqual(verdict.conflicts, ())
        self.assertEqual(
            set(verdict.forward_critical_sources), {"s1", "s2"}
        )
        self.assertEqual(
            set(verdict.reverse_critical_sources), {"s3", "s4"}
        )

    def test_different_scopes_are_not_same_scope_conflicts(self) -> None:
        adj = OrderAdjudicator()
        adj.add_order_evidence("E-S1", "s1", "a", "b", scope="scope-1")
        adj.add_order_evidence("E-S2", "s2", "b", "a", scope="scope-2")

        self.assertEqual(adj.conflicts(), [])
        self.assertEqual(adj.query("a", "b", scope="scope-1").outcome, OUTCOME_BEFORE)
        self.assertEqual(adj.query("a", "b", scope="scope-2").outcome, OUTCOME_AFTER)

        all_scopes = adj.query("a", "b")
        self.assertEqual(all_scopes.outcome, OUTCOME_CONFLICT)
        self.assertEqual(all_scopes.conflicts, ())
        self.assertIn("both ordering directions", all_scopes.reasons[0])


if __name__ == "__main__":
    unittest.main()
