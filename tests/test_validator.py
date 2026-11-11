"""Tests for the DNS label-index integrity validator.

Wire layout used by the shared-suffix fixtures (domain_size=60)::

     0: "www"  ->  4        12: "ns"  -> 15
     4: "sub"  ->  8        15: "co"  -> 19
     8: "org"  -> 40 (jump) 19: -----> 40 (jump, shared tail)
    40: "exa"  -> 44
    44: "com"  -> 48
    48: root, termination marker

Name A (start 0) = www.sub.org.exa.com.  five labels of 3 bytes:
5 * (1 + 3) + 1 root octet = 21.
Name B (start 12) = ns.co.exa.com.  two 2-byte labels and two 3-byte
labels: (1 + 2) * 2 + (1 + 3) * 2 + 1 root octet = 15.
"""

from __future__ import annotations

import unittest

from dns_index_validator import (
    IntegrityError,
    NameReport,
    NodeRecord,
    ResourceRecord,
    validate_name_index,
)

DOMAIN_SIZE = 60


def _node(position, length, next_position, *, is_label_start=True):
    return NodeRecord(
        position=position,
        label_length=length,
        is_label_start=is_label_start,
        next_position=next_position,
    )


def shared_tail_nodes():
    """The ``exa.com.`` tail plus two independent names referencing it."""
    return {
        # Name A: www.sub.org -> tail
        0: _node(0, 3, 4),
        4: _node(4, 3, 8),
        8: _node(8, 3, 40),          # compression jump
        # Name B: ns.co -> tail
        12: _node(12, 2, 15),
        15: _node(15, 2, 40),        # compression jump, same tail as A
        # Shared tail
        40: _node(40, 3, 44),
        44: _node(44, 3, 48),
        48: _node(48, 0, None),      # root label / termination marker
    }


def rr(record_id, name_start):
    return ResourceRecord(record_id=record_id, name_start=name_start)


class SharedTailTests(unittest.TestCase):
    def test_two_names_share_legal_suffix_independently(self):
        nodes = shared_tail_nodes()
        records = [rr(1, 0), rr(2, 12)]

        reports = validate_name_index(
            nodes, records,
            domain_size=DOMAIN_SIZE,
            max_expanded_bytes=30,
            jump_budget=2,
        )

        self.assertEqual([r.name_start for r in reports], [0, 12])
        self.assertTrue(all(r.ok for r in reports))
        self.assertEqual(reports[0].expanded_bytes, 21)
        self.assertEqual(reports[1].expanded_bytes, 15)
        # Each name takes exactly one jump into the shared tail.
        self.assertEqual(reports[0].jumps_used, 1)
        self.assertEqual(reports[1].jumps_used, 1)
        self.assertIsNone(reports[0].error_code)
        self.assertIsNone(reports[0].error_position)

    def test_reports_keep_resource_record_order_and_group_records(self):
        nodes = shared_tail_nodes()
        # Records arrive in non-sorted order; two records share one name.
        records = [rr(30, 12), rr(7, 0), rr(9, 0)]

        reports = validate_name_index(
            nodes, records,
            domain_size=DOMAIN_SIZE,
            max_expanded_bytes=30,
            jump_budget=2,
        )

        # One report per distinct name, in first-seen order.
        self.assertEqual([r.name_start for r in reports], [12, 0])
        self.assertEqual(reports[0].affected_records, (30,))
        self.assertEqual(reports[1].affected_records, (7, 9))


class BadRecordCoexistenceTests(unittest.TestCase):
    def test_bad_and_good_records_coexist(self):
        nodes = shared_tail_nodes()
        nodes[20] = _node(20, 3, 24, is_label_start=False)  # not a label start
        # 24 is in-domain but has no node -> incomplete evidence.
        records = [
            rr(100, 20),   # NON_LABEL_START
            rr(101, 24),   # INCOMPLETE_EVIDENCE
            rr(102, 70),   # OUT_OF_BOUNDS (70 >= domain_size)
            rr(103, 0),    # healthy name A
            rr(104, -1),   # OUT_OF_BOUNDS (negative)
        ]

        reports = validate_name_index(
            nodes, records,
            domain_size=DOMAIN_SIZE,
            max_expanded_bytes=30,
            jump_budget=2,
        )

        self.assertEqual([r.name_start for r in reports], [20, 24, 70, 0, -1])
        statuses = {r.name_start: r for r in reports}

        self.assertFalse(statuses[20].ok)
        self.assertEqual(statuses[20].error_code,
                         IntegrityError.NON_LABEL_START)
        self.assertEqual(statuses[20].error_position, 20)
        self.assertEqual(statuses[20].affected_records, (100,))

        self.assertEqual(statuses[24].error_code,
                         IntegrityError.INCOMPLETE_EVIDENCE)
        self.assertEqual(statuses[24].error_position, 24)

        self.assertEqual(statuses[70].error_code,
                         IntegrityError.OUT_OF_BOUNDS)
        self.assertEqual(statuses[70].error_position, 70)

        self.assertEqual(statuses[-1].error_code,
                         IntegrityError.OUT_OF_BOUNDS)
        self.assertEqual(statuses[-1].error_position, -1)

        # The healthy record is unaffected by its neighbours.
        self.assertTrue(statuses[0].ok)
        self.assertEqual(statuses[0].affected_records, (103,))
        self.assertIsNone(statuses[0].error_position)

    def test_error_categories_are_distinguishable(self):
        codes = {
            IntegrityError.OUT_OF_BOUNDS,
            IntegrityError.INCOMPLETE_EVIDENCE,
            IntegrityError.NON_LABEL_START,
            IntegrityError.EXPANSION_EXCEEDED,
        }
        self.assertEqual(len(codes), 4)
        self.assertEqual(
            {c.value for c in codes},
            {"out_of_bounds", "incomplete_evidence",
             "non_label_start", "expansion_exceeded"},
        )


class BudgetTests(unittest.TestCase):
    """Two adjacent calls with different budgets; no state may leak."""

    def _two_jump_name_nodes(self):
        nodes = shared_tail_nodes()
        # Name C: 30 -> 34 -> 12 -> tail, i.e. two jumps before the
        # shared-tail jump.  Physical adjacency would be 30+1+1 = 32, so the
        # 30->34 edge is a compression jump.
        nodes[30] = _node(30, 1, 34)
        nodes[34] = _node(34, 1, 12)
        return nodes

    def test_adjacent_calls_with_different_jump_budgets(self):
        nodes = self._two_jump_name_nodes()
        records = [rr(1, 30)]

        generous = validate_name_index(
            nodes, records, domain_size=DOMAIN_SIZE,
            max_expanded_bytes=40, jump_budget=3,
        )
        # Jumps: 30->34, 34->12, then 15->40 into the shared tail.
        self.assertTrue(generous[0].ok)
        self.assertEqual(generous[0].jumps_used, 3)

        # Same inputs, immediately replayed with a tighter budget.
        strict = validate_name_index(
            nodes, records, domain_size=DOMAIN_SIZE,
            max_expanded_bytes=40, jump_budget=1,
        )
        self.assertFalse(strict[0].ok)
        self.assertEqual(strict[0].error_code,
                         IntegrityError.JUMP_BUDGET_EXCEEDED)
        # First non-compliant position is the node whose edge exhausts the
        # budget: node 34 takes the second jump.
        self.assertEqual(strict[0].error_position, 34)
        self.assertEqual(strict[0].jumps_used, 2)

        # And again with the generous budget to prove determinism / isolation.
        replay = validate_name_index(
            nodes, records, domain_size=DOMAIN_SIZE,
            max_expanded_bytes=40, jump_budget=3,
        )
        self.assertTrue(replay[0].ok)
        self.assertEqual(replay[0].path, generous[0].path)

    def test_zero_budget_rejects_any_jump(self):
        nodes = shared_tail_nodes()
        reports = validate_name_index(
            nodes, [rr(1, 0)], domain_size=DOMAIN_SIZE,
            max_expanded_bytes=30, jump_budget=0,
        )
        self.assertEqual(reports[0].error_code,
                         IntegrityError.JUMP_BUDGET_EXCEEDED)
        self.assertEqual(reports[0].error_position, 8)

    def test_invalid_budget_arguments(self):
        nodes = shared_tail_nodes()
        with self.assertRaises(ValueError):
            validate_name_index(
                nodes, [rr(1, 0)], domain_size=DOMAIN_SIZE,
                max_expanded_bytes=30, jump_budget=-1,
            )
        with self.assertRaises(ValueError):
            validate_name_index(
                nodes, [rr(1, 0)], domain_size=DOMAIN_SIZE,
                max_expanded_bytes=0, jump_budget=1,
            )
        with self.assertRaises(ValueError):
            validate_name_index(
                nodes, [rr(1, 0)], domain_size=0,
                max_expanded_bytes=30, jump_budget=1,
            )


class LoopTests(unittest.TestCase):
    def test_single_path_revisiting_position_is_a_loop(self):
        nodes = shared_tail_nodes()
        nodes[30] = _node(30, 2, 34)
        nodes[34] = _node(34, 2, 30)   # back edge
        reports = validate_name_index(
            nodes, [rr(1, 30), rr(2, 0)],
            domain_size=DOMAIN_SIZE, max_expanded_bytes=30, jump_budget=5,
        )
        loop_report, healthy_report = reports
        self.assertFalse(loop_report.ok)
        self.assertEqual(loop_report.error_code, IntegrityError.LOOP)
        # Revisited position itself is the first non-compliant position.
        self.assertEqual(loop_report.error_position, 30)
        self.assertEqual(loop_report.path, (30, 34))
        self.assertEqual(loop_report.affected_records, (1,))
        # Sharing the same node table does not poison the other name.
        self.assertTrue(healthy_report.ok)

    def test_self_loop_is_detected(self):
        nodes = shared_tail_nodes()
        nodes[30] = _node(30, 2, 30)
        reports = validate_name_index(
            nodes, [rr(9, 30)],
            domain_size=DOMAIN_SIZE, max_expanded_bytes=30, jump_budget=5,
        )
        self.assertEqual(reports[0].error_code, IntegrityError.LOOP)
        self.assertEqual(reports[0].error_position, 30)


class ExpansionLimitTests(unittest.TestCase):
    def test_expansion_over_limit_reports_position(self):
        nodes = shared_tail_nodes()
        # Name A needs 21 bytes; check the boundary both ways.
        passing = validate_name_index(
            nodes, [rr(1, 0)], domain_size=DOMAIN_SIZE,
            max_expanded_bytes=21, jump_budget=2,
        )
        self.assertTrue(passing[0].ok)

        failing = validate_name_index(
            nodes, [rr(1, 0)], domain_size=DOMAIN_SIZE,
            max_expanded_bytes=20, jump_budget=2,
        )
        self.assertFalse(failing[0].ok)
        self.assertEqual(failing[0].error_code,
                         IntegrityError.EXPANSION_EXCEEDED)
        # The root label at 48 is the octet that pushes 20 bytes to 21.
        self.assertEqual(failing[0].error_position, 48)

    def test_root_label_counts_toward_limit(self):
        nodes = shared_tail_nodes()
        # Name B needs 15 bytes including the root zero octet.
        reports = validate_name_index(
            nodes, [rr(1, 12)], domain_size=DOMAIN_SIZE,
            max_expanded_bytes=14, jump_budget=2,
        )
        self.assertEqual(reports[0].error_code,
                         IntegrityError.EXPANSION_EXCEEDED)
        self.assertEqual(reports[0].error_position, 48)

    def test_root_only_name(self):
        nodes = shared_tail_nodes()
        nodes[52] = _node(52, 0, None)
        reports = validate_name_index(
            nodes, [rr(1, 52)], domain_size=DOMAIN_SIZE,
            max_expanded_bytes=1, jump_budget=0,
        )
        self.assertTrue(reports[0].ok)
        self.assertEqual(reports[0].expanded_bytes, 1)
        self.assertEqual(reports[0].path, (52,))


class EvidenceTests(unittest.TestCase):
    def test_in_domain_missing_node_is_not_out_of_bounds(self):
        nodes = shared_tail_nodes()
        reports = validate_name_index(
            nodes, [rr(1, 50), rr(2, 99)],
            domain_size=DOMAIN_SIZE, max_expanded_bytes=30, jump_budget=2,
        )
        self.assertEqual(reports[0].error_code,
                         IntegrityError.INCOMPLETE_EVIDENCE)
        self.assertEqual(reports[1].error_code,
                         IntegrityError.OUT_OF_BOUNDS)

    def test_non_root_node_with_termination_marker_is_incomplete(self):
        nodes = shared_tail_nodes()
        nodes[50] = _node(50, 3, None)  # label bytes claimed, no successor
        reports = validate_name_index(
            nodes, [rr(1, 50)],
            domain_size=DOMAIN_SIZE, max_expanded_bytes=30, jump_budget=2,
        )
        self.assertEqual(reports[0].error_code,
                         IntegrityError.INCOMPLETE_EVIDENCE)
        self.assertEqual(reports[0].error_position, 50)

    def test_structurally_impossible_node_records_raise(self):
        nodes = shared_tail_nodes()
        nodes[50] = _node(50, 0, 52)  # zero-length label with a successor
        with self.assertRaises(ValueError):
            validate_name_index(
                nodes, [rr(1, 0)], domain_size=DOMAIN_SIZE,
                max_expanded_bytes=30, jump_budget=2,
            )

    def test_report_is_frozen_value_object(self):
        nodes = shared_tail_nodes()
        reports = validate_name_index(
            nodes, [rr(1, 0)], domain_size=DOMAIN_SIZE,
            max_expanded_bytes=30, jump_budget=2,
        )
        report = reports[0]
        self.assertIsInstance(report, NameReport)
        with self.assertRaises(Exception):
            report.ok = False  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
