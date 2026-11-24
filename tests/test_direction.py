"""中转许可的方向语义：双向、单向、反向、通配与“为他人转发”的边界。"""

import unittest

from route_audit import (
    NextHopMap,
    Prefix,
    Probe,
    Route,
    RoutingState,
    TraceStatus,
    Tracer,
    TransitDirection,
    TransitGrant,
)


def _r(route_id: str, domain: str, prefix: str, next_hop: str | None) -> Route:
    return Route(route_id, domain, Prefix(prefix), next_hop)


def _chain_state(grant: TransitGrant | None) -> RoutingState:
    nh = NextHopMap()
    nh.add("corp", "nh-mid", "mid")
    nh.add("mid", "nh-dc", "dc")
    nh.add("dc", "nh-local", "dc")
    grants = (grant,) if grant is not None else ()
    state = RoutingState(
        domains=["corp", "mid", "dc"], next_hop_map=nh, grants=grants
    )
    state.add_route(_r("c1", "corp", "10.0.0.0/8", "nh-mid"))
    state.add_route(_r("m1", "mid", "10.0.0.0/8", "nh-dc"))
    state.add_route(_r("d1", "dc", "10.0.0.0/8", "nh-local"))
    return state


def _probe() -> Probe:
    return Probe("p", "corp", Prefix("10.1.1.1/24"))


class TestTransitDirection(unittest.TestCase):
    def test_bidirectional_grant_permits_corp_to_dc(self) -> None:
        state = _chain_state(
            TransitGrant("mid", "corp", "dc", TransitDirection.BIDIRECTIONAL)
        )
        self.assertEqual(
            Tracer(state).trace(_probe()).status, TraceStatus.DELIVERED
        )

    def test_forward_grant_matches_only_granted_direction(self) -> None:
        state = _chain_state(
            TransitGrant("mid", "corp", "dc", TransitDirection.FORWARD)
        )
        # corp -> dc 是许可的正向
        self.assertEqual(
            Tracer(state).trace(_probe()).status, TraceStatus.DELIVERED
        )

    def test_forward_grant_rejects_reverse_flow(self) -> None:
        # 许可写的是 dc -> corp，实际流量是 corp -> dc：必须拒绝
        state = _chain_state(
            TransitGrant("mid", "dc", "corp", TransitDirection.FORWARD)
        )
        result = Tracer(state).trace(_probe())
        self.assertEqual(result.status, TraceStatus.UNAUTHORIZED_TRANSIT)
        self.assertEqual(result.unauthorized_step.domain, "mid")

    def test_reverse_grant_allows_reverse_flow(self) -> None:
        # REVERSE 表示只允许 to -> from，即 corp -> dc
        state = _chain_state(
            TransitGrant("mid", "dc", "corp", TransitDirection.REVERSE)
        )
        self.assertEqual(
            Tracer(state).trace(_probe()).status, TraceStatus.DELIVERED
        )

    def test_grant_scoped_to_transit_domain(self) -> None:
        # 许可发给别的中转域，对 mid 无效
        state = _chain_state(
            TransitGrant("other-mid", "corp", "dc")
        )
        result = Tracer(state).trace(_probe())
        self.assertEqual(result.status, TraceStatus.UNAUTHORIZED_TRANSIT)

    def test_wildcard_endpoints(self) -> None:
        state = _chain_state(
            TransitGrant("mid", "corp", "*", TransitDirection.FORWARD)
        )
        self.assertEqual(
            Tracer(state).trace(_probe()).status, TraceStatus.DELIVERED
        )

    def test_permits_helper_directly(self) -> None:
        grant = TransitGrant("mid", "corp", "dc", TransitDirection.FORWARD)
        self.assertTrue(grant.permits("mid", "corp", "dc"))
        self.assertFalse(grant.permits("mid", "dc", "corp"))
        self.assertFalse(grant.permits("other", "corp", "dc"))

        bi = TransitGrant("mid", "corp", "dc", TransitDirection.BIDIRECTIONAL)
        self.assertTrue(bi.permits("mid", "dc", "corp"))


if __name__ == "__main__":
    unittest.main()
