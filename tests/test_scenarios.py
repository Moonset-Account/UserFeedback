"""End-to-end scenarios taken directly from the task statement."""

from __future__ import annotations

import itertools
import unittest

from gateway_routing import (
    Route,
    RouteConflictError,
    Router,
    analyze_templates,
    parse_template,
)


class StatementScenarioTests(unittest.TestCase):
    def test_canonical_example_is_a_conflict(self):
        """``/a/{x}`` vs ``/{y}/b`` — the example given in the task."""

        router = Router()
        with self.assertRaises(RouteConflictError) as caught:
            router.publish("GET", ["/a/{x}", "/{y}/b"])
        conflict = caught.exception.conflicts[0]
        self.assertEqual(conflict.witness_path, "/a/b")
        self.assertIn("/a/b", str(caught.exception))

    def test_literal_segment_count_does_not_decide_priority(self):
        """Both sides have one literal and one parameter, so counting literals
        cannot order them.  Only strict set containment may do so."""

        pair = analyze_templates("/a/{x}", "/{y}/b")
        self.assertFalse(pair.a_subset_b)
        self.assertFalse(pair.b_subset_a)
        self.assertTrue(pair.conflict)

        # Enumeration agrees that neither language contains the other.
        left = parse_template("/a/{x}")
        right = parse_template("/{y}/b")
        self.assertIsNotNone(left.match_path("/a/z"))
        self.assertIsNone(right.match_path("/a/z"))
        self.assertIsNotNone(right.match_path("/z/b"))
        self.assertIsNone(left.match_path("/z/b"))

    def test_two_teams_register_disjoint_templates(self):
        router = Router()
        router.publish_batch(
            {
                "GET": ["/teams/{team}/members", "/users/{user}"],
                "POST": ["/teams/{team}/members"],
            }
        )
        match = router.resolve("GET", "/teams/red/members")
        assert match is not None
        self.assertEqual(match.route.template.text, "/teams/{team}/members")
        self.assertEqual(match.params, {"team": "red"})
        self.assertIsNone(router.resolve("GET", "/users"))

    def test_batch_failure_keeps_last_good_table(self):
        router = Router()
        router.publish("GET", ["/a/{x}/c"])
        before = router.snapshot()
        with self.assertRaises(RouteConflictError):
            router.publish("GET", ["/a/b/c", "/a/{y}/c", "/{z}/b/c"])
        self.assertEqual(router.snapshot(), before)
        match = router.resolve("GET", "/a/1/c")
        assert match is not None
        self.assertEqual(match.route.template.text, "/a/{x}/c")


class WorkedExampleTable(unittest.TestCase):
    """A realistic table published in one go; every path resolves deterministically."""

    ROUTES = [
        "/**",
        "/health",
        "/teams/{team}/**",
        "/teams/{team}/members",
        "/teams/{team}/members/{member}",
        "/users/{user}",
        "/users/{user}/repos/{repo}",
    ]

    def setUp(self):
        self.router = Router()
        self.router.publish("GET", self.ROUTES)

    def winner(self, path: str) -> str:
        match = self.router.resolve("GET", path)
        assert match is not None, path
        return match.route.template.text

    def test_expected_winners(self):
        expected = {
            "/health": "/health",
            "/anything": "/**",
            "/teams/red": "/teams/{team}/**",
            "/teams/red/other/deep": "/teams/{team}/**",
            "/teams/red/members": "/teams/{team}/members",
            "/teams/red/members/bob": "/teams/{team}/members/{member}",
            "/users/ann": "/users/{user}",
            "/users/ann/repos/web": "/users/{user}/repos/{repo}",
            "/": "/**",
        }
        for path, template in expected.items():
            self.assertEqual(self.winner(path), template, path)

    def test_every_registered_pair_is_consistent(self):
        for left, right in itertools.combinations(self.routes(), 2):
            pair = analyze_templates(left.template, right.template)
            self.assertFalse(pair.conflict, f"{left} vs {right}")
            # Non-conflicting overlapping rules must be strictly ordered.
            if pair.intersects:
                self.assertTrue(
                    pair.a_subset_b != pair.b_subset_a,
                    f"{left} vs {right} are comparable but unordered",
                )

    def test_winner_is_minimal_among_all_matching_rules(self):
        probe_paths = []
        segments = ("health", "teams", "red", "members", "bob", "users", "ann", "x")
        for length in range(0, 5):
            for combo in itertools.product(segments, repeat=length):
                probe_paths.append("/" + "/".join(combo) if combo else "/")
        for path in probe_paths:
            match = self.router.resolve("GET", path)
            if match is None:
                self.fail(f"universal /** route should match {path}")
            matching = [
                route
                for route in self.routes()
                if route.template.match_path(path) is not None
            ]
            self.assertIn(match.route, matching)
            for other in matching:
                pair = analyze_templates(match.route.template, other.template)
                # The winner's language is contained in every other candidate's.
                self.assertTrue(pair.a_subset_b, f"{match.route} vs {other} at {path}")

    def routes(self):
        return self.router.routes("GET")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
