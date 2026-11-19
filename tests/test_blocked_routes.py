"""Acceptance tests: routes that must be blocked.

The central requirement: ordinary fields a legacy sender can write must never be
read as consent by the new version, however many hops treat them as unknown.
"""

from __future__ import annotations

import unittest

from support import (
    V1_FIELDS,
    V2_PROMOTED,
    CompatTestCase,
    ac,
    hop,
    load_fixture,
    route,
    version,
)


class LegacyPromotionTest(CompatTestCase):
    def setUp(self):
        self.v1 = version("v1", V1_FIELDS)
        self.v2 = version("v2", V2_PROMOTED)

    def test_ordinary_field_promoted_through_blind_relays_is_blocked(self):
        report = ac.check_route(
            route("legacy", self.v1, self.v2, hop("a"), hop("b"), hop("c"))
        )
        self.assertBlocked(report, "legacy_ordinary_promoted")
        violation = report.find("legacy_ordinary_promoted")[0]
        self.assertEqual(violation.field, 7)
        self.assertEqual(violation.severity, ac.BLOCK)
        self.assertEqual(violation.first_permission_change, "v2")
        self.assertEqual(violation.origin_source.version, "v1")
        self.assertEqual(violation.origin_source.meaning, "display_name")
        self.assertEqual(violation.approval_source.version, "v2")
        self.assertEqual(violation.approval_source.meaning, "approval_decision")
        # Every blind hop is named, which is what "unknown field" really does.
        self.assertEqual(
            violation.taint_path,
            ("a:unknown-preserved", "b:unknown-preserved", "c:unknown-preserved"),
        )
        self.assertNoSiblingMutation(report)

    def test_one_hundred_unknown_hops_still_blocked(self):
        hops = tuple(hop(f"relay-{i}") for i in range(100))
        report = ac.check_route(route("long", self.v1, self.v2, *hops))
        self.assertBlocked(report, "legacy_ordinary_promoted")
        self.assertEqual(len(report.find("legacy_ordinary_promoted")[0].taint_path), 100)

    def test_passthrough_hop_does_not_launder_the_value(self):
        """Saying "I copied it" is not the same as "I vouched for it"."""
        report = ac.check_route(
            route("passthrough", self.v1, self.v2, hop("relay", passthrough=[7]))
        )
        self.assertBlocked(report, "legacy_ordinary_promoted")

    def test_regenerate_without_trusted_source_is_not_enough(self):
        report = ac.check_route(
            route(
                "untrusted",
                self.v1,
                self.v2,
                hop(
                    "relay",
                    known_numbers=[7],
                    regenerate=[7],
                    rebuilt_from={7: "untrusted"},
                ),
            )
        )
        self.assertBlocked(report, "unauthenticated_approval_value")
        self.assertEqual(report.find("unauthenticated_approval_value")[0].field, 7)

    def test_regenerate_with_missing_source_defaults_to_untrusted(self):
        report = ac.check_route(
            route(
                "implicit",
                self.v1,
                self.v2,
                hop("relay", known_numbers=[7], regenerate=[7]),
            )
        )
        self.assertBlocked(report)

    def test_blind_hop_cannot_replace_a_field_it_cannot_decode(self):
        """Appending a trusted copy leaves the legacy bytes on the wire."""
        report = ac.check_route(
            route(
                "duplicate",
                self.v1,
                self.v2,
                hop("relay", regenerate=[7], rebuilt_from={7: "trusted"}),
            )
        )
        self.assertBlocked(report, "duplicate_approval_carrier")

    def test_drop_then_reintroduce_from_untrusted_source_blocked(self):
        report = ac.check_route(
            route(
                "drop-and-rewrite",
                self.v1,
                self.v2,
                hop("edge", drop=[7]),
                hop("core", known_numbers=[7], regenerate=[7]),
            )
        )
        self.assertBlocked(report, "unauthenticated_approval_value")


class EncodingTest(CompatTestCase):
    def setUp(self):
        self.v1 = version("v1", V1_FIELDS)

    def test_oneof_approval_field_is_blocked(self):
        v2 = version("v2", dict(V2_PROMOTED, **{}) | {7: ("oneof", "approval_decision")})
        report = ac.check_route(route("oneof", self.v1, v2, hop("relay")))
        self.assertBlocked(report, "weak_approval_encoding")

    def test_repeated_approval_field_is_blocked(self):
        v2 = version("v2", {**V2_PROMOTED, 7: ("repeated", "approval_decision")})
        report = ac.check_route(route("repeated", self.v1, v2, hop("relay")))
        self.assertBlocked(report, "weak_approval_encoding")

    def test_packed_approval_field_is_blocked(self):
        v2 = version("v2", {**V2_PROMOTED, 7: ("packed", "approval_decision")})
        report = ac.check_route(route("packed", self.v1, v2, hop("relay")))
        self.assertBlocked(report, "weak_approval_encoding")

    def test_ordinary_field_in_oneof_is_not_a_finding(self):
        """The weak-encoding rule is about approval, not about every field."""
        v2 = version(
            "v2",
            {
                1: ("normal", "request_id"),
                7: ("normal", "display_name"),
                12: ("oneof", "timestamp"),
            },
        )
        report = ac.check_route(route("ordinary-oneof", self.v1, v2, hop("relay")))
        self.assertAccepted(report)


class FixtureBlockedTest(CompatTestCase):
    def test_fixture_blocked_routes(self):
        document = load_fixture()
        routes = {raw["name"]: ac.route_from_dict(raw) for raw in document["routes"]}
        expected = {
            "legacy-direct-to-approval": "legacy_ordinary_promoted",
            "blind-relay-appends-trusted-copy": "duplicate_approval_carrier",
            "number-retired-then-reused": "legacy_ordinary_promoted",
        }
        for name, code in expected.items():
            with self.subTest(route=name):
                self.assertBlocked(ac.check_route(routes[name]), code)


if __name__ == "__main__":
    unittest.main()
