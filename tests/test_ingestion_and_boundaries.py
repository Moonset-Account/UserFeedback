from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from event_order import (
    DuplicateEvidenceError,
    EvidenceNotFoundError,
    OrderAdjudicator,
    ValidationError,
    adjudicate_order,
    build_adjudicator,
)


class IngestionAndBoundaryTests(unittest.TestCase):
    PAYLOAD = {
        "sources": ["phone", "server", "observer"],
        "sequences": [
            {
                "id": "SEQ-PHONE",
                "source": "phone",
                "events": ["tap", "send"],
            },
            {
                "evidence_id": "SEQ-SERVER",
                "source": "server",
                "event_order": ["recv", "store"],
            },
        ],
        "observations": [
            {
                "id": "OBS-1",
                "source": "observer",
                "initiation_event": "send",
                "receipt_event": "recv",
            }
        ],
        "retractions": [],
    }

    def test_from_dictionary_and_functional_api(self) -> None:
        adj = build_adjudicator(self.PAYLOAD)
        verdict = adj.query("tap", "store")

        self.assertEqual(verdict.outcome, "first_before_second")
        self.assertEqual(
            verdict.evidence_ids,
            ["SEQ-PHONE", "OBS-1", "SEQ-SERVER"],
        )
        self.assertEqual(
            set(verdict.critical_sources), {"phone", "observer", "server"}
        )

        data = adjudicate_order("tap", "store", self.PAYLOAD)
        self.assertEqual(data["outcome"], "first_before_second")
        self.assertTrue(data["chain"]["rendered"])
        json.dumps(data)

    def test_retraction_records_can_be_ids_or_dictionaries(self) -> None:
        payload = {
            "evidence": [
                {"id": "E1", "source": "s1", "before": "a", "after": "b"},
                {"id": "E2", "source": "s2", "type": "order", "before_event": "b", "after_event": "a"},
            ],
            "retractions": [
                "E1",
                {"retraction_id": "R2", "target_id": "E2", "reason": "correction"},
            ],
        }
        adj = OrderAdjudicator.from_dict(payload)

        self.assertEqual(adj.active_evidence_ids, [])
        self.assertEqual(adj.query("a", "b").outcome, "unknown")
        self.assertEqual(adj.retractions[0].target_evidence_id, "E1")
        self.assertEqual(adj.retractions[1].retraction_id, "R2")

    def test_unknown_retraction_is_lenient_by_default_but_strict_can_raise(self) -> None:
        adj = OrderAdjudicator()
        record = adj.retract_evidence("MISSING", "R-MISSING")
        self.assertEqual(record.target_evidence_id, "MISSING")
        self.assertEqual(len(adj.orphan_retractions), 1)

        strict = OrderAdjudicator(strict_unknown_retractions=True)
        with self.assertRaises(EvidenceNotFoundError):
            strict.retract_evidence("MISSING")

    def test_duplicate_event_in_sequence_is_rejected(self) -> None:
        adj = OrderAdjudicator()
        with self.assertRaises(ValidationError):
            adj.add_sequence("s", ["a", "b", "a"], "E-BAD")

    def test_self_order_and_missing_fields_are_rejected(self) -> None:
        adj = OrderAdjudicator()
        with self.assertRaises(ValidationError):
            adj.add_order_evidence("E-SELF", "s", "a", "a")
        with self.assertRaises(ValidationError):
            adj.add_observation("OBS-BAD", source="s", initiation_event=None, receipt_event="r")
        with self.assertRaises(ValidationError):
            OrderAdjudicator.from_dict({"evidence": [{"id": "E-X"}]})

    def test_conflicting_reuse_of_evidence_id_is_rejected(self) -> None:
        adj = OrderAdjudicator()
        adj.add_order_evidence("E-DUP", "s1", "a", "b")
        with self.assertRaises(DuplicateEvidenceError):
            adj.add_order_evidence("E-DUP", "s2", "b", "a")

    def test_empty_and_single_event_sequences_are_handled(self) -> None:
        adj = OrderAdjudicator()
        adj.add_sequence("empty-source", [], "E-EMPTY")
        adj.add_sequence("one-source", ["only"], "E-ONE")

        self.assertEqual(adj.query("only", "only").outcome, "same_event")
        unknown = adj.query("only", "missing")
        self.assertEqual(unknown.outcome, "unknown")
        self.assertIn("empty-source", unknown.removal_tests)
        self.assertFalse(unknown.removal_tests["empty-source"].still_has_evidence)

    def test_source_mapping_can_embed_internal_order(self) -> None:
        adj = OrderAdjudicator.from_dict(
            {
                "sources": {
                    "phone": ["boot", "unlock", "send"],
                    "server": {
                        "evidence_id": "SEQ-S",
                        "events": ["recv", "store"],
                    },
                },
                "observations": [
                    {
                        "evidence_id": "OBS-1",
                        "source": "observer",
                        "send_event": "send",
                        "receive_event": "recv",
                    }
                ],
            }
        )

        verdict = adj.query("boot", "store")
        self.assertEqual(verdict.outcome, "first_before_second")
        self.assertIn("AUTO-SEQ-phone", verdict.evidence_ids)
        self.assertIn("SEQ-S", verdict.evidence_ids)
        self.assertIn("OBS-1", verdict.evidence_ids)

    def test_retraction_before_evidence_still_takes_effect_in_lenient_mode(self) -> None:
        adj = OrderAdjudicator()
        adj.retract_evidence("E-LATE", "R-EARLY")
        adj.add_order_evidence("E-LATE", "s1", "a", "b")

        self.assertFalse(adj.is_active("E-LATE"))
        self.assertEqual(adj.query("a", "b").outcome, "unknown")

    def test_public_aliases_and_root_facade(self) -> None:
        import adjudicator

        adj = adjudicator.EventOrderResolver()
        adj.add_source_order("alias-source", ["a", "b"], "E-ALIAS")
        verdict = adj.answer("a", "b")

        self.assertTrue(verdict.is_order_known)
        self.assertEqual(verdict.dependent_source_ids, ["alias-source"])
        self.assertEqual(verdict.evidence_chain, verdict.chain)

        payload = {
            "sequences": [
                {"id": "E-FUNC", "source": "s", "events": ["a", "b"]}
            ]
        }
        data = adjudicator.adjudicate("a", "b", payload)
        self.assertEqual(data["outcome"], "first_before_second")

    def test_source_removal_does_not_mutate_case(self) -> None:
        adj = OrderAdjudicator.from_dict(self.PAYLOAD)
        before = adj.query("tap", "store")
        adj.query("tap", "store")  # removal analysis is read-only
        after = adj.query("tap", "store")

        self.assertEqual(before.to_dict(), after.to_dict())
        self.assertEqual(len(adj.active_evidence_ids), 3)


if __name__ == "__main__":
    unittest.main()
