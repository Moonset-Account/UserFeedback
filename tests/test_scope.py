"""前缀重名按当前路由域解释、下一跳同名不同义、IPv6 与交错边界。"""

import unittest

from route_audit import (
    NextHopMap,
    Prefix,
    Probe,
    Route,
    RoutingState,
    TraceStatus,
    Tracer,
    TransitGrant,
)


def _r(route_id: str, domain: str, prefix: str, next_hop: str | None,
       metric: int = 100) -> Route:
    return Route(route_id, domain, Prefix(prefix), next_hop, metric)


class TestDomainScoping(unittest.TestCase):
    def test_same_prefix_different_next_hops_per_domain(self) -> None:
        # 相同前缀 10.0.0.0/8 在两个域里指向不同对端，解释互不串用
        nh = NextHopMap()
        nh.add("d1", "shared-nh", "d2")
        nh.add("d2", "shared-nh", "d3")
        # 同名 shared-nh 在 d3 里反而指回 d1（制造潜在环路的“错误”数据）
        nh.add("d3", "shared-nh", "d1")
        nh.add("d1", "end", "d1")

        state = RoutingState(
            domains=["d1", "d2", "d3"],
            next_hop_map=nh,
            grants=[
                TransitGrant("d2", "d1", "d3"),
                TransitGrant("d3", "d2", "d1"),
            ],
        )
        state.add_route(_r("r1", "d1", "10.0.0.0/8", "shared-nh"))
        state.add_route(_r("r2", "d2", "10.0.0.0/8", "shared-nh"))
        state.add_route(_r("r3", "d3", "10.0.0.0/8", "shared-nh"))

        result = Tracer(state).trace(Probe("p", "d1", Prefix("10.0.0.1/24")))
        # d1 -> d2 -> d3 -> d1 后再次命中 r1，环路被防御性截断
        self.assertEqual(result.status, TraceStatus.LOOP)
        self.assertEqual([s.domain for s in result.steps], ["d1", "d2", "d3"])
        # 每一跳的同名下一跳都按各自域解释
        self.assertEqual(
            [s.peer for s in result.steps], ["d2", "d3", "d1"]
        )

    def test_unresolved_name_in_one_domain_does_not_leak_from_another(self) -> None:
        nh = NextHopMap()
        nh.add("d1", "nh-x", "d2")  # 只在 d1 登记
        state = RoutingState(domains=["d1", "d2"], next_hop_map=nh)
        state.add_route(_r("r1", "d1", "10.0.0.0/8", "nh-x"))
        state.add_route(_r("r2", "d2", "10.0.0.0/8", "nh-x"))  # d2 无法解释

        result = Tracer(state).trace(Probe("p", "d1", Prefix("10.0.0.0/24")))
        self.assertEqual(result.status, TraceStatus.UNRESOLVED_NEXT_HOP)
        self.assertEqual(result.steps[-1].domain, "d2")


class TestIPv6AndBoundaries(unittest.TestCase):
    def test_ipv6_full_compliant_path(self) -> None:
        nh = NextHopMap()
        nh.add("src", "nh", "transit")
        nh.add("transit", "nh", "dst")
        nh.add("dst", "nh", "dst")
        state = RoutingState(
            domains=["src", "transit", "dst"],
            next_hop_map=nh,
            grants=[TransitGrant("transit", "src", "dst")],
        )
        state.add_route(_r("s1", "src", "2001:db8::/32", "nh"))
        state.add_route(_r("t1", "transit", "2001:db8::/32", "nh"))
        state.add_route(_r("d1", "dst", "2001:db8::/32", "nh"))

        result = Tracer(state).trace(
            Probe("p6", "src", Prefix("2001:db8:1::/48"))
        )
        self.assertEqual(result.status, TraceStatus.DELIVERED)
        self.assertEqual(result.target.version, 6)

    def test_ipv4_and_ipv6_tables_do_not_cross_match(self) -> None:
        state = RoutingState(domains=["d"])
        state.add_route(_r("v4", "d", "0.0.0.0/0", None))
        state.add_route(_r("v6", "d", "::/0", None))
        # IPv6 目标只能命中 IPv6 默认路由，反之亦然
        self.assertEqual(
            state.lookup("d", Prefix("2001:db8::/48")).route_id, "v6"
        )
        self.assertEqual(
            state.lookup("d", Prefix("192.0.2.0/24")).route_id, "v4"
        )

    def test_exact_prefix_length_boundary(self) -> None:
        state = RoutingState(domains=["d"])
        state.add_route(_r("a", "d", "10.0.0.0/25", None))
        state.add_route(_r("b", "d", "10.0.0.128/25", None))
        # /25 边界两侧各归各的，不互相覆盖
        self.assertEqual(
            state.lookup("d", Prefix("10.0.0.0/25")).route_id, "a"
        )
        self.assertEqual(
            state.lookup("d", Prefix("10.0.0.128/25")).route_id, "b"
        )
        # 边界外无覆盖
        state_full = RoutingState(domains=["d"])
        state_full.add_route(_r("a", "d", "10.0.0.0/25", None))
        self.assertIsNone(
            state_full.lookup("d", Prefix("10.0.0.128/25"))
        )

    def test_interleaved_changes_only_matching_withdraw_alters_path(self) -> None:
        # 交错撤下两个不同域里的路由，验证变化作用域严格限定到指定域
        nh = NextHopMap()
        nh.add("corp", "nh-a", "edge-a")
        nh.add("edge-a", "nh-dc", "dc")
        nh.add("dc", "nh-local", "dc")
        state = RoutingState(
            domains=["corp", "edge-a", "dc"],
            next_hop_map=nh,
            grants=[TransitGrant("edge-a", "corp", "dc")],
        )
        state.add_route(_r("c1", "corp", "10.0.0.0/8", "nh-a"))
        state.add_route(_r("a1", "edge-a", "10.0.0.0/8", "nh-dc"))
        state.add_route(_r("d1", "dc", "10.0.0.0/8", "nh-local"))

        tracer = Tracer(state)
        probe = Probe("p", "corp", Prefix("10.1.0.0/16"))
        self.assertEqual(tracer.trace(probe).status, TraceStatus.DELIVERED)

        # 先撤 dc 的路由：路径在终点变成 NO_ROUTE，但不构成未授权中转
        state.withdraw_route("dc", "d1")
        self.assertEqual(tracer.trace(probe).status, TraceStatus.NO_ROUTE)

        # 恢复 dc，再撤 edge-a：corp 出域后 edge-a 无路由
        state.add_route(_r("d1", "dc", "10.0.0.0/8", "nh-local"))
        state.withdraw_route("edge-a", "a1")
        self.assertEqual(tracer.trace(probe).status, TraceStatus.NO_ROUTE)

        # corp 的路由始终未受影响
        self.assertEqual(
            state.lookup("corp", Prefix("10.1.0.0/16")).route_id, "c1"
        )


if __name__ == "__main__":
    unittest.main()
