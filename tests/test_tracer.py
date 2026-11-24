"""逐跳追踪：授权中转、未授权中转、本地终结、无路由、下一跳不可解释、环路。"""

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


def _route(route_id: str, domain: str, prefix: str, next_hop: str | None,
           metric: int = 100) -> Route:
    return Route(route_id, domain, Prefix(prefix), next_hop, metric)


def _four_domain_state(grants=()) -> RoutingState:
    nh = NextHopMap()
    # corp 视角
    nh.add("corp", "nh-a", "edge-a")
    nh.add("corp", "nh-b", "edge-b")
    # edge-a / edge-b 视角（同名 nh-dc 在两个域里各自登记）
    nh.add("edge-a", "nh-dc", "dc")
    nh.add("edge-b", "nh-dc", "dc")
    # dc 本地终结：下一跳指向自身
    nh.add("dc", "nh-local", "dc")
    return RoutingState(
        domains=["corp", "edge-a", "edge-b", "dc"],
        next_hop_map=nh,
        grants=grants,
    )


class TestTracer(unittest.TestCase):
    def _build_authorized_topology(self) -> RoutingState:
        state = _four_domain_state(
            grants=[
                TransitGrant("edge-a", "corp", "dc",
                             TransitDirection.BIDIRECTIONAL),
            ]
        )
        state.add_route(_route("c1", "corp", "10.10.1.0/24", "nh-a"))
        state.add_route(_route("a1", "edge-a", "10.10.0.0/16", "nh-dc"))
        state.add_route(_route("d1", "dc", "10.10.0.0/16", "nh-local"))
        return state

    def test_authorized_path_is_delivered(self) -> None:
        state = self._build_authorized_topology()
        result = Tracer(state).trace(
            Probe("p1", "corp", Prefix("10.10.1.0/24"))
        )
        self.assertEqual(result.status, TraceStatus.DELIVERED)
        self.assertTrue(result.is_compliant)
        self.assertEqual([s.domain for s in result.steps],
                         ["corp", "edge-a", "dc"])
        # 第一跳是源域自身出域，不视为中转
        self.assertTrue(result.steps[0].is_origin_egress)
        self.assertTrue(result.steps[0].authorized)
        # edge-a 的中转跳有明确许可
        self.assertFalse(result.steps[1].is_origin_egress)
        self.assertTrue(result.steps[1].authorized)
        self.assertEqual(result.steps[2].peer, "dc")

    def test_origin_egress_needs_no_grant(self) -> None:
        # 没有任何中转许可，但只有源域出域 + 目的域本地终结，仍然合规
        state = _four_domain_state()
        nh = state.next_hop_map
        nh.add("corp", "nh-direct", "dc")
        state.add_route(_route("c1", "corp", "10.0.0.0/8", "nh-direct"))
        state.add_route(_route("d1", "dc", "10.0.0.0/8", "nh-local"))
        result = Tracer(state).trace(
            Probe("p", "corp", Prefix("10.1.2.3/24"))
        )
        self.assertEqual(result.status, TraceStatus.DELIVERED)

    def test_unauthorized_transit_reports_first_step_only(self) -> None:
        # corp -> edge-b（无许可）-> dc；必须停在 edge-b 这一跳
        state = _four_domain_state()
        state.add_route(_route("c1", "corp", "10.10.1.0/24", "nh-b"))
        state.add_route(_route("b1", "edge-b", "10.10.0.0/16", "nh-dc"))
        state.add_route(_route("d1", "dc", "10.10.0.0/16", "nh-local"))

        result = Tracer(state).trace(
            Probe("p1", "corp", Prefix("10.10.1.0/24"))
        )
        self.assertEqual(result.status, TraceStatus.UNAUTHORIZED_TRANSIT)
        self.assertIsNotNone(result.unauthorized_step)
        step = result.unauthorized_step
        self.assertEqual(step.domain, "edge-b")
        self.assertEqual(step.peer, "dc")
        self.assertFalse(step.authorized)
        # 首个未授权步骤出现后立即停止，不继续走到 dc
        self.assertEqual([s.domain for s in result.steps], ["corp", "edge-b"])

    def test_local_delivery_via_none_next_hop(self) -> None:
        state = _four_domain_state()
        state.add_route(_route("lo", "corp", "10.10.1.0/24", None))
        result = Tracer(state).trace(
            Probe("p", "corp", Prefix("10.10.1.0/24"))
        )
        self.assertEqual(result.status, TraceStatus.DELIVERED)
        self.assertIsNone(result.steps[0].peer)

    def test_no_route_status(self) -> None:
        state = self._build_authorized_topology()
        result = Tracer(state).trace(
            Probe("p", "corp", Prefix("172.16.0.0/12"))
        )
        self.assertEqual(result.status, TraceStatus.NO_ROUTE)
        self.assertEqual(result.steps, ())

    def test_unresolved_next_hop(self) -> None:
        state = _four_domain_state()
        # corp 有路由指向一个在 corp 解释空间里不存在的下一跳名
        state.add_route(_route("c1", "corp", "10.10.1.0/24", "nh-ghost"))
        result = Tracer(state).trace(
            Probe("p", "corp", Prefix("10.10.1.0/24"))
        )
        self.assertEqual(result.status, TraceStatus.UNRESOLVED_NEXT_HOP)
        self.assertEqual(result.steps[0].next_hop, "nh-ghost")

    def test_loop_is_detected(self) -> None:
        state = _four_domain_state(
            grants=[
                # 两个域的回程转发均授权，排除授权干扰，
                # 纯粹验证 (域, 路由) 环路检测
                TransitGrant("edge-a", "corp", "*"),
                TransitGrant("corp", "*", "*"),
            ]
        )
        # corp <-> edge-a 互相把同一前缀指给对方，形成跨域环路
        state.add_route(_route("c1", "corp", "10.10.1.0/24", "nh-a"))
        state.add_route(_route("a1", "edge-a", "10.10.1.0/24", "nh-back"))
        state.next_hop_map.add("edge-a", "nh-back", "corp")
        result = Tracer(state).trace(
            Probe("p", "corp", Prefix("10.10.1.0/24"))
        )
        self.assertEqual(result.status, TraceStatus.LOOP)

    def test_max_hops_cap(self) -> None:
        # 构造一条长授权链，验证防御性跳数上限
        domains = [f"d{i}" for i in range(6)]
        nh = NextHopMap()
        grants = []
        for i in range(len(domains) - 1):
            nh.add(domains[i], "nh", domains[i + 1])
            if i > 0:
                grants.append(
                    TransitGrant(domains[i], domains[i - 1], domains[i + 1])
                )
        state = RoutingState(
            domains=domains, next_hop_map=nh, grants=grants
        )
        for i, name in enumerate(domains[:-1]):
            state.add_route(_route(f"r{i}", name, "10.0.0.0/8", "nh"))
        nh.add(domains[-1], "nh", domains[-1])
        state.add_route(_route("rend", domains[-1], "10.0.0.0/8", "nh"))

        result = Tracer(state, max_hops=3).trace(
            Probe("p", domains[0], Prefix("10.0.0.1/24"))
        )
        self.assertEqual(result.status, TraceStatus.MAX_HOPS)
        self.assertEqual(len(result.steps), 3)

    def test_same_next_hop_name_resolved_in_current_domain(self) -> None:
        # 重名 nh-dc：默认解释为 dc，这里在 edge-a 内覆盖成 edge-b（模拟错误对接）
        state = _four_domain_state(
            grants=[TransitGrant("edge-a", "corp", "edge-b")]
        )
        state.next_hop_map.add("edge-a", "nh-dc", "edge-b")  # 覆盖为错误对端
        state.next_hop_map.add("edge-b", "nh-end", "edge-b")  # edge-b 本地终结
        state.add_route(_route("c1", "corp", "10.10.1.0/24", "nh-a"))
        state.add_route(_route("a1", "edge-a", "10.10.0.0/16", "nh-dc"))
        state.add_route(_route("b1", "edge-b", "10.10.0.0/16", "nh-end"))
        result = Tracer(state).trace(
            Probe("p", "corp", Prefix("10.10.1.0/24"))
        )
        # edge-a 按自己的解释把流量送给 edge-b，许可恰好覆盖该方向
        self.assertEqual(result.status, TraceStatus.DELIVERED)
        self.assertEqual(result.steps[1].peer, "edge-b")


if __name__ == "__main__":
    unittest.main()
