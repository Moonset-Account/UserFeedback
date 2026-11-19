"""Acceptance tests: when may a route ship?

Covers the positive side -- routes that genuinely sever the legacy path -- so a
checker that simply blocks everything cannot pass this module.
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


class TrustedRebuildTest(CompatTestCase):
    """A hop that drops the old bytes and rebuilds from trust is acceptable."""

    def setUp(self):
        self.v1 = version("v1", V1_FIELDS)
        self.v2 = version("v2", V2_PROMOTED)

    def test_decode_drop_and_rebuild_passes(self):
        report = ac.check_route(
            route(
                "core-rebuild",
                self.v1,
                self.v2,
                hop("edge"),
                hop(
                    "core",
                    known_numbers=[1, 7, 12],
                    regenerate=[7],
                    rebuilt_from={7: "trusted"},
                ),
            )
        )
        self.assertAccepted(report)
        self.assertIn("trusted_rebuild", self.codes(report))
        self.assertNoSiblingMutation(report)

    def test_sever_then_rebuild_across_two_hops_passes(self):
        """Severing and rebuilding need not happen at the same hop."""
        report = ac.check_route(
            route(
                "split-rebuild",
                self.v1,
                self.v2,
                hop("edge", drop=[7]),
                hop("core", regenerate=[7], rebuilt_from={7: "trusted_input"}),
            )
        )
        self.assertAccepted(report)
        trusted = report.find("trusted_rebuild")
        self.assertEqual([v.field for v in trusted], [7])
        self.assertEqual(trusted[0].trusted_rebuild_at, ("core",))

    def test_rebuild_may_be_reported_after_unknown_preserving_hop(self):
        """An earlier hop may carry the bytes; the authoritative hop still wins."""
        report = ac.check_route(
            route(
                "carry-then-rebuild",
                self.v1,
                self.v2,
                hop("edge"),
                hop(
                    "core",
                    known_numbers=[7],
                    regenerate=[7],
                    rebuilt_from={7: "trusted"},
                ),
            )
        )
        self.assertAccepted(report)
        info = report.find("trusted_rebuild")[0]
        self.assertEqual(info.field, 7)
        self.assertEqual(info.first_permission_change, "v2")
        self.assertIn("edge:unknown-preserved", info.taint_path)

    def test_unknown_bytes_dropped_everywhere_passes(self):
        """A relay that severs unknown fields never delivers the legacy value."""
        report = ac.check_route(
            route("sever", self.v1, self.v2, hop("edge", unknown_bytes="dropped"))
        )
        self.assertAccepted(report)
        self.assertEqual(report.find("legacy_ordinary_promoted"), ())

    def test_fresh_number_unreachable_by_legacy_sender_passes(self):
        """A brand-new number is not something the legacy schema can address."""
        v2 = version(
            "v2",
            {
                1: ("normal", "request_id"),
                7: ("normal", "display_name"),
                12: ("normal", "timestamp"),
                40: ("normal", "approval_decision"),
            },
        )
        report = ac.check_route(route("fresh", self.v1, v2, hop("relay")))
        self.assertAccepted(report)
        self.assertEqual(report.find("legacy_ordinary_promoted"), ())

    def test_one_bad_field_does_not_hide_a_good_rebuild(self):
        """Mixed route: field 7 is rebuilt, field 20 is still smuggled through."""
        v1 = version("v1", {7: ("normal", "display_name"), 20: ("normal", "summary")})
        v2 = version(
            "v2",
            {
                7: ("normal", "approval_decision"),
                20: ("normal", "approval_scope"),
            },
        )
        report = ac.check_route(
            route(
                "mixed",
                v1,
                v2,
                hop("core", known_numbers=[7], regenerate=[7], rebuilt_from={7: "trusted"}),
            )
        )
        self.assertBlocked(report, "legacy_ordinary_promoted")
        promoted = report.find("legacy_ordinary_promoted")
        self.assertEqual([v.field for v in promoted], [20])
        rebuilt = report.find("trusted_rebuild")
        self.assertEqual([v.field for v in rebuilt], [7])
        self.assertEqual(rebuilt[0].trusted_rebuild_at, ("core",))


class FixtureRouteTest(CompatTestCase):
    """The shipped fixture must keep its mix of accepted and blocked routes."""

    def test_fixture_rebuild_routes_pass(self):
        document = load_fixture()
        routes = {raw["name"]: ac.route_from_dict(raw) for raw in document["routes"]}
        for name in ("core-severs-and-rebuilds", "edge-severs-core-rebuilds"):
            with self.subTest(route=name):
                self.assertAccepted(ac.check_route(routes[name]))

    def test_fixture_reports_are_serialisable(self):
        import json

        document = load_fixture()
        for raw in document["routes"]:
            report = ac.check_route(ac.route_from_dict(raw))
            payload = json.dumps(report.as_dict(), ensure_ascii=False)
            self.assertIn('"route"', payload)
            self.assertEqual(json.loads(payload)["ok"], report.ok)


if __name__ == "__main__":
    unittest.main()
