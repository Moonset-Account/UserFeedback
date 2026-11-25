from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from event_order import OUTCOME_BEFORE, OUTCOME_SAME, OUTCOME_UNKNOWN, OrderAdjudicator


class EvidenceChainAndSourceRemovalTests(unittest.TestCase):
    def test_source_sequence_produces_chain_and_single_source_dependency(self) -> None:
        adj = OrderAdjudicator()
        adj.add_sequence(
            "phone-log",
            ["phone_boot", "app_open", "message_tap"],
            evidence_id="E-phone-sequence",
        )

        verdict = adj.query("phone_boot", "message_tap")

        self.assertEqual(verdict.outcome, OUTCOME_BEFORE)
        self.assertIsNotNone(verdict.chain)
        chain = verdict.chain
        assert chain is not None
        self.assertEqual(chain.before, "phone_boot")
        self.assertEqual(chain.after, "message_tap")
        self.assertEqual(len(chain.steps), 2)
        # One sequence record backs both adjacent transitions.
        self.assertEqual(chain.evidence_ids, ["E-phone-sequence"])
        self.assertEqual(
            chain.step_evidence_ids,
            ["E-phone-sequence", "E-phone-sequence"],
        )
        self.assertEqual(chain.source_ids, ["phone-log"])
        self.assertIn("E-phone-sequence", chain.render())
        self.assertEqual(verdict.critical_sources, ("phone-log",))
        removal = verdict.removal_tests["phone-log"]
        self.assertFalse(removal.still_has_evidence)
        self.assertIsNone(removal.remaining_chain)
        self.assertTrue(removal.critical_for_adjudicated_conclusion)
        # The verdict is plain JSON data.
        json.dumps(verdict.to_dict())

    def test_observation_connects_concrete_initiation_and_receipt_events(self) -> None:
        adj = OrderAdjudicator()
        adj.add_observation(
            "OBS-1",
            source="packet-capture",
            initiation_event="client_send_42",
            receipt_event="server_recv_42",
        )

        verdict = adj.query("client_send_42", "server_recv_42")

        self.assertEqual(verdict.outcome, OUTCOME_BEFORE)
        self.assertEqual(verdict.evidence_ids, ["OBS-1"])
        self.assertEqual(verdict.source_ids, ["packet-capture"])
        self.assertEqual(verdict.critical_sources, ("packet-capture",))

    def test_interleaved_source_orders_are_joined_by_observations(self) -> None:
        adj = OrderAdjudicator()
        adj.add_sequence("source-a", ["a1", "a2", "a3"], "E-A")
        adj.add_sequence("source-b", ["b1", "b2", "b3"], "E-B")
        adj.add_observation(
            "OBS-A2-B1",
            source="observer-1",
            initiation_event="a2",
            receipt_event="b1",
        )
        adj.add_observation(
            "OBS-B2-A3",
            source="observer-2",
            initiation_event="b2",
            receipt_event="a3",
        )

        first_chain = adj.query("a1", "b3")
        self.assertEqual(first_chain.outcome, OUTCOME_BEFORE)
        self.assertEqual(
            first_chain.evidence_ids,
            ["E-A", "OBS-A2-B1", "E-B"],
        )
        self.assertEqual(
            first_chain.source_ids,
            ["source-a", "observer-1", "source-b"],
        )
        self.assertEqual(
            set(first_chain.critical_sources),
            {"source-a", "source-b", "observer-1"},
        )
        self.assertNotIn("observer-2", first_chain.critical_sources)
        self.assertTrue(
            first_chain.removal_tests["observer-2"].still_has_evidence
        )

        second_chain = adj.query("b1", "a3")
        self.assertEqual(second_chain.outcome, OUTCOME_BEFORE)
        self.assertEqual(
            second_chain.evidence_ids,
            ["E-B", "OBS-B2-A3"],
        )
        self.assertEqual(
            set(second_chain.critical_sources),
            {"source-b", "observer-2"},
        )

    def test_redundant_independent_sources_survive_any_single_removal(self) -> None:
        adj = OrderAdjudicator()
        adj.add_sequence("phone-log", ["tap", "send"], "E-phone")
        adj.add_sequence("server-log", ["recv", "store"], "E-server")
        adj.add_observation(
            "OBS-net-1",
            source="net-observer-1",
            initiation_event="send",
            receipt_event="recv",
        )
        adj.add_observation(
            "OBS-net-2",
            source="net-observer-2",
            initiation_event="send",
            receipt_event="recv",
        )

        verdict = adj.query("send", "recv")

        self.assertEqual(verdict.outcome, OUTCOME_BEFORE)
        self.assertEqual(verdict.critical_sources, ())
        self.assertTrue(verdict.removal_tests["net-observer-1"].still_has_evidence)
        self.assertTrue(verdict.removal_tests["net-observer-2"].still_has_evidence)
        self.assertEqual(
            verdict.removal_tests["net-observer-1"].remaining_chain.evidence_ids,
            ["OBS-net-2"],
        )
        self.assertEqual(
            verdict.removal_tests["net-observer-2"].remaining_chain.evidence_ids,
            ["OBS-net-1"],
        )
        # Sources not used by the direct edge are also tested and do not break it.
        self.assertTrue(verdict.removal_tests["phone-log"].still_has_evidence)
        self.assertTrue(verdict.removal_tests["server-log"].still_has_evidence)

    def test_unique_linking_source_is_critical_in_end_to_end_chain(self) -> None:
        adj = OrderAdjudicator()
        adj.add_sequence("phone", ["boot", "unlock", "send"], "E-phone")
        adj.add_observation(
            "OBS-bridge",
            source="bridge-source",
            initiation_event="send",
            receipt_event="recv",
        )
        adj.add_sequence("server", ["recv", "auth", "write"], "E-server")

        verdict = adj.query("boot", "write")

        self.assertEqual(verdict.outcome, OUTCOME_BEFORE)
        self.assertEqual(
            verdict.evidence_ids,
            ["E-phone", "OBS-bridge", "E-server"],
        )
        self.assertEqual(
            set(verdict.critical_sources),
            {"phone", "bridge-source", "server"},
        )
        for source in ("phone", "bridge-source", "server"):
            test = verdict.removal_tests[source]
            self.assertFalse(test.still_has_evidence, source)
            self.assertTrue(test.critical_for_adjudicated_conclusion)

    def test_unknown_disconnected_and_same_event_boundaries(self) -> None:
        empty = OrderAdjudicator()
        unknown = empty.query("missing-a", "missing-b")
        self.assertEqual(unknown.outcome, OUTCOME_UNKNOWN)
        self.assertIsNone(unknown.chain)
        self.assertEqual(unknown.critical_sources, ())
        self.assertEqual(unknown.removal_tests, {})

        adj = OrderAdjudicator()
        adj.add_source("solo")
        same = adj.query("event-x", "event-x")
        self.assertEqual(same.outcome, OUTCOME_SAME)
        self.assertIsNotNone(same.chain)
        self.assertEqual(same.chain.steps, ())
        self.assertEqual(same.critical_sources, ())
        self.assertTrue(same.removal_tests["solo"].still_has_evidence)

        adj.add_order_evidence("E1", "solo", "known-a", "known-b")
        disconnected = adj.query("known-a", "other-known")
        self.assertEqual(disconnected.outcome, OUTCOME_UNKNOWN)
        self.assertEqual(disconnected.critical_sources, ())
        self.assertFalse(
            disconnected.removal_tests["solo"].still_has_evidence
        )


if __name__ == "__main__":
    unittest.main()
