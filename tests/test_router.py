"""Router publication, precedence, and atomicity tests."""

from __future__ import annotations

import unittest

from gateway_routing import (
    Route,
    RouteConflictError,
    RouteTable,
    Router,
    RoutingError,
)


class PublishBasicsTests(unittest.TestCase):
    def test_empty_router(self):
        router = Router()
        self.assertEqual(len(router), 0)
        self.assertEqual(router.methods(), ())
        self.assertIsNone(router.resolve("GET", "/a"))

    def test_publish_and_resolve_literal(self):
        router = Router()
        router.publish("GET", ["/a/b"])
        match = router.resolve("get", "/a/b")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.route.template.text, "/a/b")
        self.assertEqual(match.params, {})

    def test_publish_with_payload(self):
        router = Router()
        router.publish("GET", [Route.create("/a", payload="handler-a")])
        match = router.resolve("GET", "/a")
        assert match is not None
        self.assertEqual(match.route.payload, "handler-a")

    def test_unknown_method(self):
        router = Router()
        router.publish("GET", ["/a"])
        self.assertIsNone(router.resolve("POST", "/a"))

    def test_wrong_path(self):
        router = Router()
        router.publish("GET", ["/a"])
        self.assertIsNone(router.resolve("GET", "/b"))

    def test_malformed_path_resolves_to_none(self):
        router = Router()
        router.publish("GET", ["/a"])
        for path in ("a", "/a/", "/a//b", "", 3):
            self.assertIsNone(router.resolve("GET", path), path)  # type: ignore[arg-type]

    def test_invalid_method_argument(self):
        router = Router()
        with self.assertRaises(RoutingError):
            router.publish("   ", ["/a"])

    def test_methods_are_normalized(self):
        router = Router()
        router.publish(" post ", ["/a"])
        self.assertEqual(router.methods(), ("POST",))

    def test_publish_replaces_previous_batch(self):
        router = Router()
        router.publish("GET", ["/a", "/b"])
        router.publish("GET", ["/c"])
        self.assertEqual([r.template.text for r in router.routes("GET")], ["/c"])
        self.assertIsNone(router.resolve("GET", "/a"))
        self.assertIsNotNone(router.resolve("GET", "/c"))

    def test_methods_are_independent(self):
        # Same template under two methods is not a conflict.
        router = Router()
        router.publish_batch({"GET": ["/a/{x}"], "POST": ["/a/{x}"]})
        self.assertEqual(router.methods(), ("GET", "POST"))
        self.assertEqual(len(router), 2)

    def test_table_is_immutable_snapshot(self):
        router = Router()
        router.publish("GET", ["/a"])
        table_before = router.table("GET")
        router.publish("GET", ["/b"])
        self.assertEqual([r.template.text for r in table_before.routes], ["/a"])
        self.assertEqual([r.template.text for r in router.table("GET").routes], ["/b"])


class ConflictRejectionTests(unittest.TestCase):
    def test_canonical_overlap_rejected(self):
        router = Router()
        with self.assertRaises(RouteConflictError) as caught:
            router.publish("GET", ["/a/{x}", "/{y}/b"])
        error = caught.exception
        self.assertEqual(len(error.conflicts), 1)
        conflict = error.conflicts[0]
        self.assertEqual(conflict.witness_path, "/a/b")
        self.assertEqual(conflict.method, "GET")
        self.assertIn("/a/b", str(error))

    def test_equal_languages_rejected(self):
        router = Router()
        with self.assertRaises(RouteConflictError):
            router.publish("GET", ["/a/{x}", "/a/{y}"])

    def test_identical_templates_rejected(self):
        router = Router()
        with self.assertRaises(RouteConflictError):
            router.publish("GET", ["/a/b", "/a/b"])

    def test_overlap_reports_every_offending_pair(self):
        router = Router()
        with self.assertRaises(RouteConflictError) as caught:
            router.publish("GET", ["/a/{x}", "/{y}/b", "/c/{z}", "/{w}/d"])
        # Four overlapping pairs; the disjoint ones (/a/*~*/c/* and
        # /*/b~*/*/d) are not reported.
        self.assertEqual(len(caught.exception.conflicts), 4)
        witnesses = {c.witness_path for c in caught.exception.conflicts}
        self.assertEqual(witnesses, {"/a/b", "/a/d", "/c/b", "/c/d"})

    def test_conflict_lists_both_rules(self):
        router = Router()
        with self.assertRaises(RouteConflictError) as caught:
            router.publish("GET", ["/a/{x}", "/{y}/b"])
        conflict = caught.exception.conflicts[0]
        rendered = conflict.describe()
        self.assertIn("/a/{x}", rendered)
        self.assertIn("/{y}/b", rendered)
        self.assertIn("/a/b", rendered)

    def test_star_overlap_witness_is_real(self):
        router = Router()
        with self.assertRaises(RouteConflictError) as caught:
            router.publish("GET", ["/a/**", "/{y}/b/**"])
        witness = caught.exception.conflicts[0].witness_path
        self.assertEqual(witness, "/a/b")
        self.assertIsNotNone(Route.create("/a/**").template.match_path(witness))
        self.assertIsNotNone(Route.create("/{y}/b/**").template.match_path(witness))

    def test_named_routes_in_message(self):
        router = Router()
        with self.assertRaises(RouteConflictError) as caught:
            router.publish(
                "GET",
                [Route.create("/a/{x}", name="alpha"), Route.create("/{y}/b", name="beta")],
            )
        message = str(caught.exception)
        self.assertIn("alpha", message)
        self.assertIn("beta", message)


class AtomicPublicationTests(unittest.TestCase):
    def test_failed_first_publish_leaves_empty_table(self):
        router = Router()
        with self.assertRaises(RouteConflictError):
            router.publish("GET", ["/a/{x}", "/{y}/b"])
        self.assertEqual(router.methods(), ())
        self.assertIsNone(router.resolve("GET", "/a/b"))

    def test_failed_republish_keeps_previous_routes(self):
        router = Router()
        router.publish("GET", ["/old/{id}"])
        before = router.snapshot()
        with self.assertRaises(RouteConflictError):
            router.publish("GET", ["/new/{x}", "/{y}/b"])
        self.assertEqual(router.snapshot(), before)
        self.assertIsNotNone(router.resolve("GET", "/old/7"))
        self.assertIsNone(router.resolve("GET", "/new/1"))

    def test_failed_batch_is_all_or_nothing(self):
        router = Router()
        router.publish_batch({"GET": ["/g/{id}"], "POST": ["/p/{id}"]})
        before = router.snapshot()
        with self.assertRaises(RouteConflictError) as caught:
            router.publish_batch(
                {
                    "GET": ["/ok/{id}"],          # valid on its own
                    "POST": ["/a/{x}", "/{y}/b"],  # conflicting
                }
            )
        # Nothing changed for either method, including the valid GET batch.
        self.assertEqual(router.snapshot(), before)
        self.assertIsNotNone(router.resolve("GET", "/g/1"))
        self.assertIsNone(router.resolve("GET", "/ok/1"))
        self.assertEqual(caught.exception.methods, ("POST",))
        self.assertEqual(len(caught.exception.for_method("post")), 1)

    def test_replace_all_rejects_and_keeps_old_world(self):
        router = Router()
        router.publish_batch({"GET": ["/g"], "POST": ["/p"]})
        before = router.snapshot()
        with self.assertRaises(RouteConflictError):
            router.replace_all({"GET": ["/a/{x}", "/{y}/b"]})
        self.assertEqual(router.snapshot(), before)

    def test_replace_all_drops_absent_methods(self):
        router = Router()
        router.publish_batch({"GET": ["/g"], "POST": ["/p"]})
        router.replace_all({"GET": ["/g2"]})
        self.assertEqual(router.methods(), ("GET",))
        self.assertIsNone(router.resolve("POST", "/p"))


class PrecedenceTests(unittest.TestCase):
    def test_more_specific_wins_over_param(self):
        router = Router()
        router.publish("GET", ["/a/{x}", "/a/b"])
        match = router.resolve("GET", "/a/b")
        assert match is not None
        self.assertEqual(match.route.template.text, "/a/b")
        self.assertEqual(match.params, {})

    def test_param_still_matches_other_segments(self):
        router = Router()
        router.publish("GET", ["/a/{x}", "/a/b"])
        match = router.resolve("GET", "/a/c")
        assert match is not None
        self.assertEqual(match.route.template.text, "/a/{x}")
        self.assertEqual(match.params, {"x": "c"})

    def test_literal_length_is_not_the_priority_rule(self):
        # These two do not conflict only because one set is contained in the
        # other; the literal has fewer literal segments but must still win.
        router = Router()
        router.publish("GET", ["/{a}/{b}", "/x/{y}"])
        match = router.resolve("GET", "/x/z")
        assert match is not None
        self.assertEqual(match.route.template.text, "/x/{y}")

    def test_star_loses_to_specific_prefix(self):
        router = Router()
        router.publish("GET", ["/a/**", "/a/b"])
        match = router.resolve("GET", "/a/b")
        assert match is not None
        self.assertEqual(match.route.template.text, "/a/b")
        # The star still handles deeper paths.
        match = router.resolve("GET", "/a/b/c")
        assert match is not None
        self.assertEqual(match.route.template.text, "/a/**")

    def test_star_matches_prefix_base(self):
        router = Router()
        router.publish("GET", ["/a/**", "/a"])
        match = router.resolve("GET", "/a")
        assert match is not None
        self.assertEqual(match.route.template.text, "/a")
        match = router.resolve("GET", "/a/x")
        assert match is not None
        self.assertEqual(match.route.template.text, "/a/**")

    def test_nested_specificity_chain(self):
        router = Router()
        router.publish("GET", ["/**", "/a/**", "/a/{x}", "/a/b", "/a/b/c"])
        expectations = {
            "/": "/**",
            "/z": "/**",
            "/a": "/a/**",
            "/a/q": "/a/{x}",
            "/a/b": "/a/b",
            "/a/b/c": "/a/b/c",
            # Neither /a/{x} nor /a/b/c matches four segments; /a/** does.
            "/a/b/c/d": "/a/**",
            "/a/b/x/y/z": "/a/**",
        }
        for path, expected in expectations.items():
            match = router.resolve("GET", path)
            assert match is not None, path
            self.assertEqual(match.route.template.text, expected, path)

    def test_specificity_beats_registration_order(self):
        router = Router()
        router.publish("GET", ["/a/{x}", "/a/b"])
        first = router.resolve("GET", "/a/b")
        router.publish("GET", ["/a/b", "/a/{x}"])
        second = router.resolve("GET", "/a/b")
        assert first is not None and second is not None
        self.assertEqual(first.route.template.text, second.route.template.text)

    def test_params_only_from_winner(self):
        router = Router()
        router.publish("GET", ["/{x}/{y}", "/a/{z}"])
        match = router.resolve("GET", "/a/7")
        assert match is not None
        self.assertEqual(match.route.template.text, "/a/{z}")
        self.assertEqual(match.params, {"z": "7"})


class DryRunTests(unittest.TestCase):
    def test_check_reports_conflicts_without_publishing(self):
        router = Router()
        conflicts = router.check("GET", ["/a/{x}", "/{y}/b"])
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].witness_path, "/a/b")
        self.assertEqual(router.methods(), ())
        self.assertEqual(router.check("GET", ["/a/{x}", "/a/b"]), ())

    def test_single_template_may_be_passed_without_a_list(self):
        router = Router()
        router.publish("GET", "/only")
        self.assertIsNotNone(router.resolve("GET", "/only"))

    def test_non_iterable_route_argument_is_rejected(self):
        router = Router()
        with self.assertRaises(RoutingError):
            router.publish("GET", 42)  # type: ignore[arg-type]


class RouteTableValidationTests(unittest.TestCase):
    def test_direct_table_construction_validates(self):
        with self.assertRaises(RouteConflictError):
            RouteTable(
                method="GET",
                routes=(Route.create("/a/{x}"), Route.create("/{y}/b")),
            )

    def test_route_defaults(self):
        route = Route.create("/a")
        self.assertEqual(route.payload, None)
        self.assertIsNone(route.name)
        self.assertEqual(str(route), "/a")

    def test_route_accepts_plain_string(self):
        route = Route("/a/{x}")
        self.assertEqual(route.template.text, "/a/{x}")
        self.assertIsNotNone(route.template.match_path("/a/1"))

    def test_route_rejects_bad_template(self):
        from gateway_routing import TemplateError

        with self.assertRaises(TemplateError):
            Route.create("/a/**/b")

    def test_route_with_name(self):
        route = Route.create("/a", name="alpha")
        self.assertEqual(str(route), "alpha(/a)")


class EdgeCaseTests(unittest.TestCase):
    def test_root_route(self):
        router = Router()
        router.publish("GET", ["/", "/a"])
        match = router.resolve("GET", "/")
        assert match is not None
        self.assertEqual(match.route.template.text, "/")

    def test_universal_star_makes_others_redundant_but_allowed(self):
        router = Router()
        router.publish("GET", ["/**", "/a/{x}"])
        match = router.resolve("GET", "/a/q")
        assert match is not None
        self.assertEqual(match.route.template.text, "/a/{x}")
        match = router.resolve("GET", "/zzz")
        assert match is not None
        self.assertEqual(match.route.template.text, "/**")

    def test_unicode_and_encoded_segments(self):
        router = Router()
        router.publish("GET", ["/café/{id}"])
        match = router.resolve("GET", "/café/%20x")
        assert match is not None
        self.assertEqual(match.params, {"id": "%20x"})

    def test_duplicate_param_last_wins(self):
        router = Router()
        router.publish("GET", ["/a/{x}/{x}"])
        match = router.resolve("GET", "/a/1/2")
        assert match is not None
        self.assertEqual(match.params, {"x": "2"})

    def test_snapshot_returns_all_methods(self):
        router = Router()
        router.publish_batch({"GET": ["/g"], "PUT": ["/p"]})
        self.assertEqual(set(router.snapshot()), {"GET", "PUT"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
