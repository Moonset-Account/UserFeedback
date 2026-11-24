"""每域独立前缀表上的最长前缀匹配（LPM）与撤下/重注入行为。"""

import unittest

from route_audit import (
    DuplicateRouteError,
    Prefix,
    Route,
    RouteNotFoundError,
    RoutingState,
    UnknownDomainError,
)


def _route(route_id: str, domain: str, prefix: str, next_hop: str | None,
           metric: int = 100) -> Route:
    return Route(
        route_id=route_id,
        domain=domain,
        prefix=Prefix(prefix),
        next_hop=next_hop,
        metric=metric,
    )


class TestLPM(unittest.TestCase):
    def setUp(self) -> None:
        self.state = RoutingState(domains=["corp", "edge-a"])
        self.state.add_route(_route("r-default", "corp", "0.0.0.0/0", "nh-d", 300))
        self.state.add_route(_route("r-agg", "corp", "10.10.0.0/16", "nh-a", 200))
        self.state.add_route(_route("r-specific", "corp", "10.10.1.0/24", "nh-b", 100))

    def test_longest_prefix_wins_over_metric(self) -> None:
        # /24 即使 metric 更优/更差都应压过 /16 与默认路由：长度优先
        match = self.state.lookup("corp", Prefix("10.10.1.9/24"))
        self.assertIsNotNone(match)
        self.assertEqual(match.route_id, "r-specific")  # type: ignore[union-attr]

    def test_falls_back_to_aggregate(self) -> None:
        match = self.state.lookup("corp", Prefix("10.10.9.0/24"))
        self.assertEqual(match.route_id, "r-agg")  # type: ignore[union-attr]

    def test_default_route_boundary(self) -> None:
        match = self.state.lookup("corp", Prefix("198.51.100.0/24"))
        self.assertEqual(match.route_id, "r-default")  # type: ignore[union-attr]

    def test_no_match_returns_none(self) -> None:
        empty = RoutingState(domains=["corp"])
        self.assertIsNone(empty.lookup("corp", Prefix("10.0.0.0/8")))

    def test_metric_tie_break_on_equal_length(self) -> None:
        self.state.add_route(
            _route("r-specific-bad", "corp", "10.10.1.0/24", "nh-x", 150)
        )
        match = self.state.lookup("corp", Prefix("10.10.1.0/24"))
        self.assertEqual(match.route_id, "r-specific")  # type: ignore[union-attr]

    def test_insertion_order_breaks_exact_tie(self) -> None:
        self.state.add_route(
            _route("r-tie", "corp", "10.10.1.0/24", "nh-z", 100)
        )
        match = self.state.lookup("corp", Prefix("10.10.1.0/24"))
        # 长度与 metric 完全相同时，先注入者稳定胜出
        self.assertEqual(match.route_id, "r-specific")  # type: ignore[union-attr]

    def test_withdraw_exposes_next_best(self) -> None:
        removed = self.state.withdraw_route("corp", "r-specific")
        self.assertEqual(removed.route_id, "r-specific")
        match = self.state.lookup("corp", Prefix("10.10.1.0/24"))
        self.assertEqual(match.route_id, "r-agg")  # type: ignore[union-attr]

    def test_withdraw_missing_route_raises(self) -> None:
        with self.assertRaises(RouteNotFoundError):
            self.state.withdraw_route("corp", "ghost")

    def test_duplicate_route_id_raises(self) -> None:
        with self.assertRaises(DuplicateRouteError):
            self.state.add_route(
                _route("r-agg", "corp", "10.20.0.0/16", "nh-a")
            )

    def test_tables_are_isolated_per_domain(self) -> None:
        # edge-a 有相同前缀也有同名路由 id，但与 corp 的表互不可见
        self.state.add_route(_route("r-specific", "edge-a", "10.10.1.0/24", "nh-e"))
        corp_match = self.state.lookup("corp", Prefix("10.10.1.0/24"))
        edge_match = self.state.lookup("edge-a", Prefix("10.10.1.0/24"))
        self.assertEqual(corp_match.next_hop, "nh-b")  # type: ignore[union-attr]
        self.assertEqual(edge_match.next_hop, "nh-e")  # type: ignore[union-attr]

    def test_unknown_domain_raises(self) -> None:
        with self.assertRaises(UnknownDomainError):
            self.state.lookup("ghost-domain", Prefix("10.0.0.0/8"))


if __name__ == "__main__":
    unittest.main()
