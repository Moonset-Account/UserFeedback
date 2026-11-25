from __future__ import annotations

import unittest

from solution import (
    AMBIGUOUS_SEGMENT,
    INVALID_TICKET,
    MISSING_PARENT_EDGE,
    OK,
    PARENT_CYCLE,
    REVIEW_MISSING,
    TARGET_MISMATCH,
    TICKET_CYCLE,
    UNTRUSTED_HOP,
    CompressedSegment,
    TrustValidator,
    first_failure_position,
    validate_ticket,
    verify_ticket,
)


def full_hierarchy_hops():
    return {
        "a": {"b"},
        "b": {"c"},
        "c": {"d"},
        "d": {"e"},
        "e": {"f"},
    }


def hierarchy_parents():
    return {"b": "c", "c": "d", "d": "e"}


class NormalPathTests(unittest.TestCase):
    def test_explicit_path_is_allowed_when_each_issuer_authorizes_next_hop(self):
        result = validate_ticket(
            {"origin": "a", "target": "c", "transit": ["b"]},
            issued_hops={"a": "b", "b": "c"},
        )

        self.assertTrue(result.allowed, result.reason)
        self.assertEqual(result.code, OK)
        self.assertEqual(result.expanded_path, ("a", "b", "c"))

    def test_compressed_parent_segment_is_expanded_edge_by_edge(self):
        ticket = {
            "start_domain": "a",
            "target_domain": "f",
            "intermediate_domains": [
                "b",
                CompressedSegment(start="b", end="e"),
            ],
        }
        result = validate_ticket(
            ticket,
            issued_hops=full_hierarchy_hops(),
            parents=hierarchy_parents(),
        )

        self.assertTrue(result.allowed, result.reason)
        self.assertEqual(result.expanded_path, ("a", "b", "c", "d", "e", "f"))

    def test_dictionary_and_double_dot_segment_forms_are_supported(self):
        ticket = {
            "source": "a",
            "destination": "e",
            "hops": ["b..e"],
        }
        result = validate_ticket(
            ticket,
            issued_hops={
                "a": "b",
                "b": "c",
                "c": "d",
                "d": "e",
            },
            parents={"b": "c", "c": "d", "d": "e"},
        )

        self.assertTrue(result.allowed, result.reason)
        self.assertEqual(result.expanded_path, ("a", "b", "c", "d", "e"))

    def test_multiple_segments_and_explicit_hops_interleave(self):
        result = validate_ticket(
            {"origin": "a", "target": "h", "transit": [
                "b",
                CompressedSegment(start="b", end="d"),
                "e",
                CompressedSegment(end="g"),
            ]},
            issued_hops={
                "a": "b",
                "b": "c",
                "c": "d",
                "d": "e",
                "e": "f",
                "f": "g",
                "g": "h",
            },
            parents={"b": "c", "c": "d", "e": "f", "f": "g"},
        )

        self.assertTrue(result.allowed, result.reason)
        self.assertEqual(result.expanded_path, tuple("abcdefgh"))

    def test_zero_edge_same_origin_and_target_is_allowed(self):
        result = validate_ticket(
            {"origin": "a", "target": "a"},
            issued_hops={},
        )

        self.assertTrue(result.allowed, result.reason)
        self.assertEqual(result.expanded_path, ("a",))


class TrustFailureTests(unittest.TestCase):
    def test_first_unauthorized_hop_is_attributed_to_its_issuer(self):
        result = validate_ticket(
            {"origin": "a", "target": "c", "transit": ["b"]},
            # b has not issued c.
            issued_hops={"a": {"b"}, "b": {"other"}},
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.code, UNTRUSTED_HOP)
        self.assertEqual(result.position, 1)
        self.assertEqual(result.domain, "b")
        self.assertEqual(result.next_domain, "c")
        self.assertEqual(result.edge_index, 1)

    def test_parent_relation_does_not_imply_trust_for_restored_edges(self):
        result = validate_ticket(
            {
                "origin": "a",
                "target": "e",
                "transit": [CompressedSegment(start="a", end="e")],
            },
            issued_hops={
                "a": "b",
                "b": "c",
                # c -> d is absent even though parents contains it.
                "d": "e",
            },
            parents={"a": "b", "b": "c", "c": "d", "d": "e"},
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.code, UNTRUSTED_HOP)
        self.assertEqual(result.domain, "c")
        self.assertEqual(result.next_domain, "d")
        self.assertEqual(result.position, 2)
        self.assertEqual(result.item_index, 0)
        self.assertEqual(result.expanded_path, ("a", "b", "c"))

    def test_trusted_shortcut_does_not_replace_tickets_actual_path(self):
        result = validate_ticket(
            {"origin": "a", "target": "z", "transit": ["b"]},
            issued_hops={
                # This direct shortcut is trusted but was not the ticket path.
                "a": {"b", "z"},
                # b is not responsible for / authorized to issue z.
                "b": {"c"},
            },
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.code, UNTRUSTED_HOP)
        self.assertEqual(result.domain, "b")
        self.assertEqual(result.next_domain, "z")
        self.assertEqual(result.position, 1)
        self.assertEqual(result.expanded_path, ("a", "b"))


class ParentRestorationTests(unittest.TestCase):
    def test_missing_parent_edge_is_reported_at_child(self):
        result = validate_ticket(
            {
                "origin": "a",
                "target": "d",
                "transit": [CompressedSegment(start="a", end="d")],
            },
            issued_hops={"a": "b", "b": "c"},
            parents={"a": "b"},  # b has no parent c/d edge.
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.code, MISSING_PARENT_EDGE)
        self.assertEqual(result.domain, "b")
        self.assertEqual(result.position, 1)
        self.assertEqual(result.item_index, 0)

    def test_parent_cycle_is_rejected_at_domain_that_closes_loop(self):
        result = validate_ticket(
            {
                "origin": "a",
                "target": "far",
                "transit": [CompressedSegment(start="a", end="far")],
            },
            issued_hops={"a": "b", "b": "c", "c": "b"},
            parents={"a": "b", "b": "c", "c": "b"},
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.code, PARENT_CYCLE)
        self.assertEqual(result.domain, "c")
        self.assertEqual(result.next_domain, "b")
        self.assertEqual(result.cycle_domain, "b")
        self.assertEqual(result.cycle_start_position, 1)
        self.assertEqual(result.position, 2)

    def test_multiple_possible_parents_make_segment_non_unique(self):
        result = validate_ticket(
            {
                "origin": "a",
                "target": "d",
                "transit": [CompressedSegment(start="a", end="d")],
            },
            issued_hops={
                "a": {"b", "c"},
                "b": "d",
                "c": "d",
            },
            parents={"a": ["b", "c"], "b": "d", "c": "d"},
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.code, AMBIGUOUS_SEGMENT)
        self.assertEqual(result.domain, "a")
        self.assertEqual(result.position, 0)
        self.assertIn("b", result.details["parent_candidates"])
        self.assertIn("c", result.details["parent_candidates"])

    def test_explicit_ticket_route_cycle_is_rejected(self):
        result = validate_ticket(
            {"origin": "a", "target": "a", "transit": ["b"]},
            issued_hops={"a": "b", "b": "a"},
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.code, TICKET_CYCLE)
        self.assertEqual(result.domain, "b")
        self.assertEqual(result.cycle_domain, "a")
        self.assertEqual(result.cycle_start_position, 0)


class ReviewPolicyTests(unittest.TestCase):
    def test_required_reviewer_in_explicit_path_passes(self):
        result = validate_ticket(
            {"origin": "partner", "target": "app", "transit": ["audit", "gateway"]},
            issued_hops={
                "partner": "audit",
                "audit": "gateway",
                "gateway": "app",
            },
            review_requirements={"partner": "audit"},
        )

        self.assertTrue(result.allowed, result.reason)
        self.assertIn("audit", result.expanded_path)

    def test_required_reviewer_inside_compressed_segment_counts_after_expansion(self):
        result = validate_ticket(
            {
                "origin": "partner",
                "target": "app",
                "transit": [CompressedSegment(start="partner", end="root"), "app"],
            },
            issued_hops={
                "partner": "audit",
                "audit": "corp",
                "corp": "root",
                "root": "app",
            },
            parents={"partner": "audit", "audit": "corp", "corp": "root"},
            review_requirements={"partner": "audit"},
        )

        self.assertTrue(result.allowed, result.reason)
        self.assertEqual(
            result.expanded_path,
            ("partner", "audit", "corp", "root", "app"),
        )

    def test_missing_review_returns_source_and_enforcement_positions(self):
        result = validate_ticket(
            {"origin": "partner", "target": "app", "transit": ["gateway"]},
            issued_hops={"partner": "gateway", "gateway": "app"},
            review_requirements={"partner": "audit"},
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.code, REVIEW_MISSING)
        self.assertEqual(result.domain, "partner")
        self.assertEqual(result.source, "partner")
        self.assertEqual(result.source_position, 0)
        self.assertEqual(result.position, 0)
        self.assertEqual(result.target, "app")
        self.assertEqual(result.target_position, 2)
        self.assertEqual(result.details["enforcement_position"], 2)
        self.assertEqual(result.required_reviewer, "audit")

    def test_reviewer_in_unused_topology_does_not_count(self):
        result = validate_ticket(
            {"origin": "partner", "target": "app", "transit": ["gateway"]},
            issued_hops={
                "partner": {"gateway", "audit"},
                "gateway": "app",
                "audit": "app",
            },
            parents={"partner": "audit"},
            review_requirements={"partner": "audit"},
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.code, REVIEW_MISSING)
        self.assertEqual(result.expanded_path, ("partner", "gateway", "app"))

    def test_target_scoped_review_requirement(self):
        policy = [
            {"source": "partner", "reviewer": "audit", "targets": "secret-app"},
            {"source": "partner", "reviewer": "other-audit", "targets": "public-app"},
        ]
        hops = {"partner": "app-edge", "app-edge": "secret-app"}

        required = validate_ticket(
            {"origin": "partner", "target": "secret-app", "transit": ["app-edge"]},
            issued_hops=hops,
            review_requirements=policy,
        )
        self.assertEqual(required.code, REVIEW_MISSING)
        self.assertEqual(required.required_reviewer, "audit")

        other_target = validate_ticket(
            {"origin": "partner", "target": "public-app", "transit": ["app-edge"]},
            issued_hops={"partner": "app-edge", "app-edge": "public-app"},
            review_requirements=policy,
        )
        self.assertEqual(other_target.code, REVIEW_MISSING)
        self.assertEqual(other_target.required_reviewer, "other-audit")


class InterleavingAndOrderTests(unittest.TestCase):
    def test_earlier_trust_failure_wins_over_later_bad_segment_and_missing_review(self):
        result = validate_ticket(
            {
                "origin": "a",
                "target": "z",
                "transit": [
                    "b",
                    CompressedSegment(start="c", end="z"),
                ],
            },
            issued_hops={
                "a": "b",
                # b -> c intentionally missing.
                "c": "d",
            },
            parents={"c": "d"},
            review_requirements={"a": "audit"},
        )

        self.assertEqual(result.code, UNTRUSTED_HOP)
        self.assertEqual(result.domain, "b")
        self.assertEqual(result.position, 1)

    def test_later_segment_failure_keeps_earlier_valid_prefix(self):
        result = validate_ticket(
            {
                "origin": "a",
                "target": "z",
                "transit": [
                    "b",
                    CompressedSegment(start="b", end="z"),
                ],
            },
            issued_hops={"a": "b", "b": "c"},
            parents={"b": "c"},  # c lacks parent needed to reach z.
        )

        self.assertEqual(result.code, MISSING_PARENT_EDGE)
        self.assertEqual(result.domain, "c")
        self.assertEqual(result.position, 2)
        self.assertEqual(result.item_index, 1)
        self.assertEqual(result.expanded_path, ("a", "b", "c"))

    def test_plural_sources_policy_form(self):
        result = validate_ticket(
            {"origin": "partner-a", "target": "app", "transit": ["edge"]},
            issued_hops={"partner-a": "edge", "edge": "app"},
            review_requirements=[
                {"sources": ["partner-a", "partner-b"], "reviewer": "audit"}
            ],
        )
        self.assertEqual(result.code, REVIEW_MISSING)
        self.assertEqual(result.source, "partner-a")

    def test_any_domain_review_scope_returns_earliest_unreviewed_source(self):
        validator = TrustValidator(
            issued_hops={
                "a": "b",
                "b": "c",
                "c": "d",
            },
            review_requirements={"b": "review-b", "c": "review-c"},
            review_source_scope="any_domain",
        )

        result = validator.validate({"origin": "a", "target": "d", "transit": ["b", "c"]})

        self.assertEqual(result.code, REVIEW_MISSING)
        self.assertEqual(result.domain, "b")
        self.assertEqual(result.position, 1)

    def test_path_form_with_segment_and_metadata(self):
        result = validate_ticket(
            {
                "origin": "a",
                "target": "f",
                "path": [
                    "a",
                    {"start": "a", "end": "e", "compressed": True},
                    "f",
                ],
            },
            issued_hops=full_hierarchy_hops(),
            parents={"a": "b", **hierarchy_parents()},
        )

        self.assertTrue(result.allowed, result.reason)
        self.assertEqual(result.expanded_path, ("a", "b", "c", "d", "e", "f"))

    def test_ellipsis_notation_is_restored_from_origin(self):
        result = validate_ticket(
            {"origin": "a", "target": "e", "transit": ["...", "e"]},
            issued_hops={
                "a": "b",
                "b": "c",
                "c": "d",
                "d": "e",
            },
            parents={"a": "b", "b": "c", "c": "d", "d": "e"},
        )

        self.assertTrue(result.allowed, result.reason)
        self.assertEqual(result.expanded_path, ("a", "b", "c", "d", "e"))

    def test_extra_dead_parent_branch_does_not_make_endpoint_chain_ambiguous(self):
        result = validate_ticket(
            {
                "origin": "a",
                "target": "d",
                "transit": [CompressedSegment(start="a", end="d")],
            },
            issued_hops={"a": {"b", "x"}, "b": {"d"}},
            parents={"a": ["b", "x"], "b": "d"},
        )

        self.assertTrue(result.allowed, result.reason)
        self.assertEqual(result.expanded_path, ("a", "b", "d"))

    def test_result_dictionary_is_json_like(self):
        result = validate_ticket(
            {"origin": "a", "target": "b"},
            issued_hops={},
        )
        data = result.to_dict()

        self.assertFalse(data["allowed"])
        self.assertEqual(data["code"], UNTRUSTED_HOP)
        self.assertEqual(data["expanded_path"], ["a"])
        self.assertIsNone(data["required_reviewer"])


class InputAndBoundaryTests(unittest.TestCase):
    def test_missing_origin_is_an_invalid_ticket_result_not_exception(self):
        result = validate_ticket({"target": "b"}, issued_hops={})
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, INVALID_TICKET)

    def test_non_string_domain_is_invalid_ticket(self):
        result = validate_ticket({"origin": 7, "target": "b"})
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, INVALID_TICKET)

    def test_target_metadata_mismatch_is_reported(self):
        result = validate_ticket(
            {"origin": "a", "target": "c", "path": ["a", "b"]},
            issued_hops={"a": "b"},
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, TARGET_MISMATCH)
        self.assertEqual(result.domain, "b")
        self.assertEqual(result.position, 1)

    def test_zero_length_segment_and_inferred_start(self):
        zero_length = validate_ticket(
            {
                "origin": "a",
                "target": "b",
                "transit": [CompressedSegment(start="a", end="a")],
            },
            issued_hops={"a": "b"},
        )
        self.assertTrue(zero_length.allowed, zero_length.reason)
        self.assertEqual(zero_length.expanded_path, ("a", "b"))

        inferred = validate_ticket(
            {
                "origin": "a",
                "target": "c",
                "transit": [CompressedSegment(end="c")],
            },
            issued_hops={"a": "b", "b": "c"},
            parents={"a": "b", "b": "c"},
        )
        self.assertTrue(inferred.allowed, inferred.reason)
        self.assertEqual(inferred.expanded_path, ("a", "b", "c"))

    def test_edge_iterables_and_domain_canonicalization(self):
        result = validate_ticket(
            {"origin": "A.", "target": "C", "transit": ["B"]},
            issued_hops=[("a", "b"), ("b", "c")],
        )
        self.assertTrue(result.allowed, result.reason)
        self.assertEqual(result.expanded_path, ("a", "b", "c"))

    def test_compact_result_helpers(self):
        ok = verify_ticket({"origin": "a", "target": "b"}, issued_hops={"a": "b"})
        self.assertEqual(ok, (True, None, OK))
        self.assertIsNone(
            first_failure_position({"origin": "a", "target": "b"}, issued_hops={"a": "b"})
        )

        bad = verify_ticket({"origin": "a", "target": "c", "transit": ["b"]}, issued_hops={"a": "b"})
        self.assertEqual(bad, (False, 1, UNTRUSTED_HOP))
        self.assertEqual(
            first_failure_position(
                {"origin": "a", "target": "c", "transit": ["b"]},
                issued_hops={"a": "b"},
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
