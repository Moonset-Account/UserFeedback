"""Acceptance tests for the boundary and interleaved cases.

These are the scenarios that decide whether the checker is a real boundary
check or a keyword matcher: field-number reuse across a retirement, promotion at
an intermediate version only, routes fed by mixed senders, and the ordering of a
sever against a rebuild.
"""

from __future__ import annotations

import unittest

from support import (
    V1_FIELDS,
    CompatTestCase,
    ac,
    hop,
    route,
    version,
)


class FieldNumberReuseTest(CompatTestCase):
    """Gaps in the numbering do not make a number new."""

    def test_retired_number_reused_for_approval_is_blocked(self):
        v1 = version("v1", {8: ("normal", "attachment_ref"), 2: ("normal", "request_id")})
        v2 = version("v2", {2: ("normal", "request_id")})
        v3 = version(
            "v3",
            {
                2: ("normal", "request_id"),
                8: ("normal", "approval_decision"),
            },
        )
        report = ac.check_route(
            route(
                "reuse",
                v1,
                v3,
                hop("relay"),
                timeline=(v1, v2, v3),
            )
        )
        self.assertBlocked(report, "legacy_ordinary_promoted")
        violation = report.find("legacy_ordinary_promoted")[0]
        self.assertEqual(violation.field, 8)
        self.assertEqual(violation.first_permission_change, "v3")
        self.assertEqual(violation.origin_source.meaning, "attachment_ref")

    def test_number_absent_from_origin_but_present_later_is_still_blocked(self):
        """The origin schema is not the only writer on the path."""
        v1 = version("v1", {2: ("normal", "request_id")})
        v2 = version(
            "v2",
            {
                2: ("normal", "request_id"),
                8: ("normal", "approval_decision"),
            },
        )
        report = ac.check_route(
            route(
                "legacy-unknown",
                v1,
                v2,
                hop("relay"),
                legacy_unknown_field_bytes=True,
            )
        )
        self.assertBlocked(report, "unauthenticated_approval_value")

    def test_fresh_approval_only_number_is_not_an_escalation(self):
        """A number that was never ordinary cannot be a legacy-written consent."""
        v1 = version("v1", {2: ("normal", "request_id")})
        v2 = version(
            "v2",
            {
                2: ("normal", "request_id"),
                8: ("normal", "approval_decision"),
            },
        )
        report = ac.check_route(route("fresh", v1, v2, hop("relay")))
        self.assertAccepted(report)


class IntermediateVersionTest(CompatTestCase):
    """The destination is not the only reader on the path."""

    def test_promotion_then_retirement_warns_with_the_middle_version(self):
        v1 = version("v1", {5: ("normal", "summary"), 9: ("normal", "request_id")})
        v2 = version("v2", {5: ("normal", "approval_scope"), 9: ("normal", "request_id")})
        v3 = version("v3", {9: ("normal", "request_id")})
        report = ac.check_route(
            route("middle", v1, v3, hop("relay"), timeline=(v1, v2, v3))
        )
        self.assertAccepted(report)  # destination is clean
        warnings = report.find("intermediate_permission_change")
        self.assertEqual([v.field for v in warnings], [5])
        self.assertEqual(warnings[0].first_permission_change, "v2")
        self.assertEqual(warnings[0].severity, ac.WARN)
        self.assertEqual(report.first_permission_change().first_permission_change, "v2")

    def test_two_promotions_report_the_first_one(self):
        v1 = version("v1", {3: ("normal", "summary")})
        v2 = version("v2", {3: ("normal", "approval")})
        v3 = version("v3", {3: ("normal", "summary")})
        v4 = version("v4", {3: ("normal", "approval")})
        report = ac.check_route(
            route("twice", v1, v4, hop("relay"), timeline=(v1, v2, v3, v4))
        )
        self.assertBlocked(report, "legacy_ordinary_promoted")
        self.assertEqual(
            report.find("legacy_ordinary_promoted")[0].first_permission_change, "v2"
        )

    def test_timeline_must_start_at_origin(self):
        v1 = version("v1", {3: ("normal", "summary")})
        v2 = version("v2", {3: ("normal", "approval")})
        with self.assertRaises(ac.SchemaError):
            route("bad", v1, v2, hop("relay"), timeline=(v2,))


class MixedSenderTest(CompatTestCase):
    """One route can be fed by more than one client version."""

    def test_legacy_and_new_senders_judged_by_the_weakest(self):
        old = version("v1", {7: ("normal", "display_name")})
        new = version("v3", {7: ("normal", "approval_decision")})
        dst = version("v4", {7: ("normal", "approval_decision")})
        report = ac.check_route(
            route(
                "mixed",
                old,
                dst,
                hop("relay"),
                timeline=(old, new, dst),
                senders=(old, new),
            )
        )
        self.assertBlocked(report, "legacy_ordinary_promoted")
        self.assertEqual(
            report.find("legacy_ordinary_promoted")[0].origin_source.version, "v1"
        )

    def test_approval_only_senders_yield_a_warning_not_a_block(self):
        new = version("v1", {7: ("normal", "approval_decision")})
        dst = version("v2", {7: ("normal", "approval_decision")})
        report = ac.check_route(route("already-approval", new, dst, hop("relay")))
        self.assertAccepted(report)
        self.assertEqual(
            [v.severity for v in report.find("preexisting_approval_channel")],
            [ac.WARN],
        )


class SeverRebuildOrderTest(CompatTestCase):
    """Order matters: a rebuild before the legacy write does not help."""

    def setUp(self):
        self.v1 = version("v1", {7: ("normal", "display_name")})
        self.v2 = version("v2", {7: ("normal", "approval_decision")})

    def test_sever_then_rebuild_passes(self):
        report = ac.check_route(
            route(
                "ok-order",
                self.v1,
                self.v2,
                hop("edge", unknown_bytes="dropped"),
                hop("core", known_numbers=[7], regenerate=[7], rebuilt_from={7: "trusted"}),
            )
        )
        self.assertAccepted(report)

    def test_rebuild_then_preserve_is_still_rebuilt(self):
        """Once the trusted hop replaced the bytes, later relays carry that."""
        report = ac.check_route(
            route(
                "rebuild-first",
                self.v1,
                self.v2,
                hop("core", known_numbers=[7], regenerate=[7], rebuilt_from={7: "trusted"}),
                hop("edge"),
            )
        )
        self.assertAccepted(report)

    def test_drop_after_preserve_still_severs(self):
        """A late sever is enough: the destination is what reads the field."""
        report = ac.check_route(
            route(
                "late-sever",
                self.v1,
                self.v2,
                hop("edge"),
                hop("core", drop=[7]),
            )
        )
        self.assertAccepted(report)
        self.assertIn("approval_field_not_carried", [v.code for v in report.violations])


class AmbiguousBoundaryTest(CompatTestCase):
    """Cases that look identical to a naive checker but differ in outcome."""

    def test_same_route_shape_passes_and_fails_on_one_flag(self):
        """Field 40 is undeclared in v1: only the strict assumption blocks it."""
        v1 = version("v1", {7: ("normal", "display_name")})
        v2 = version("v2", {7: ("normal", "display_name"), 40: ("normal", "approval")})
        hops = (hop("relay"),)
        strict = ac.check_route(
            route("strict", v1, v2, *hops, legacy_unknown_field_bytes=True)
        )
        relaxed = ac.check_route(
            route("relaxed", v1, v2, *hops, legacy_unknown_field_bytes=False)
        )
        self.assertBlocked(strict, "unauthenticated_approval_value")
        self.assertAccepted(relaxed)

    def test_preserved_and_dropped_unknown_hops_differ_only_by_a_flag(self):
        v1 = version("v1", {7: ("normal", "display_name")})
        v2 = version("v2", {7: ("normal", "approval_decision")})
        preserved = ac.check_route(route("p", v1, v2, hop("relay")))
        dropped = ac.check_route(
            route("d", v1, v2, hop("relay", unknown_bytes="dropped"))
        )
        self.assertBlocked(preserved, "legacy_ordinary_promoted")
        self.assertAccepted(dropped)

    def test_trace_matches_the_verdict(self):
        v1 = version("v1", {7: ("normal", "display_name")})
        v2 = version("v2", {7: ("normal", "approval_decision")})
        blocked = ac.check_route(route("p", v1, v2, hop("relay")))
        trace = {t.field: t for t in blocked.traces}[7]
        self.assertEqual(trace.final_state, ac.SENDER_BYTES)
        self.assertEqual(
            [(s[0], s[1]) for s in trace.steps],
            [("origin", "sender-writable"), ("relay", "unknown(preserved)")],
        )

        dropped = ac.check_route(route("d", v1, v2, hop("relay", unknown_bytes="dropped")))
        self.assertEqual({t.field: t for t in dropped.traces}[7].final_state, ac.ABSENT)


if __name__ == "__main__":
    unittest.main()
