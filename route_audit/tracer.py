"""逐跳出域路径追踪与中转授权核对。"""

from __future__ import annotations

from .model import (
    Prefix,
    Probe,
    Step,
    TraceStatus,
    TraceResult,
)
from .state import RoutingState

# 防御性跳数上限，避免数据错误导致无限追踪
DEFAULT_MAX_HOPS = 32


class Tracer:
    """在给定 RoutingState 上，沿逐跳 LPM 核对一个前缀的完整出域路径。"""

    def __init__(self, state: RoutingState, max_hops: int = DEFAULT_MAX_HOPS) -> None:
        self.state = state
        self.max_hops = max_hops

    def trace(self, probe: Probe) -> TraceResult:
        self.state.require_domain(probe.origin_domain)

        current_domain = probe.origin_domain
        # 路径上已经过的“来源域”，用于授权判断与环路检测
        previous_domain: str | None = None
        steps: list[Step] = []
        # (域, route_id) 去重，防止跨域环路
        visited: set[tuple[str, str]] = set()

        while True:
            if len(steps) >= self.max_hops:
                return self._result(probe, TraceStatus.MAX_HOPS, steps, None)

            route = self.state.lookup(current_domain, probe.target)
            if route is None:
                return self._result(probe, TraceStatus.NO_ROUTE, steps, None)

            visit_key = (current_domain, route.route_id)
            if visit_key in visited:
                return self._result(probe, TraceStatus.LOOP, steps, None)
            visited.add(visit_key)

            order = len(steps)
            is_origin_egress = previous_domain is None

            # 本地直连/终结：路由本身声明无下一跳
            if route.next_hop is None:
                steps.append(
                    Step(
                        order=order,
                        domain=current_domain,
                        route=route,
                        next_hop=None,
                        peer=None,
                        authorized=True,
                        is_origin_egress=is_origin_egress,
                    )
                )
                return self._result(probe, TraceStatus.DELIVERED, steps, None)

            # 下一跳名只在“当前路由域”的解释空间里解析
            peer = self.state.next_hop_map.resolve(current_domain, route.next_hop)
            if peer is None:
                steps.append(
                    Step(
                        order=order,
                        domain=current_domain,
                        route=route,
                        next_hop=route.next_hop,
                        peer=None,
                        authorized=False,
                        is_origin_egress=is_origin_egress,
                    )
                )
                return self._result(
                    probe, TraceStatus.UNRESOLVED_NEXT_HOP, steps, None
                )

            # 下一跳指向本域自身：视为本地终结
            if peer == current_domain:
                steps.append(
                    Step(
                        order=order,
                        domain=current_domain,
                        route=route,
                        next_hop=route.next_hop,
                        peer=peer,
                        authorized=True,
                        is_origin_egress=is_origin_egress,
                    )
                )
                return self._result(probe, TraceStatus.DELIVERED, steps, None)

            # 授权判定：
            # - 源域自身的第一跳（出域）不构成“为其他域转发”，无需许可；
            # - 之后每一跳都是 current_domain 把 previous_domain 的流量
            #   转发给 peer，必须持有对应方向的中转许可。
            if is_origin_egress:
                authorized = True
            else:
                assert previous_domain is not None
                authorized = self.state.transit_permitted(
                    transit_domain=current_domain,
                    source=previous_domain,
                    target=peer,
                )

            step = Step(
                order=order,
                domain=current_domain,
                route=route,
                next_hop=route.next_hop,
                peer=peer,
                authorized=authorized,
                is_origin_egress=is_origin_egress,
            )
            steps.append(step)

            if not authorized:
                return self._result(
                    probe,
                    TraceStatus.UNAUTHORIZED_TRANSIT,
                    steps,
                    unauthorized_step=step,
                )

            previous_domain = current_domain
            current_domain = peer

    @staticmethod
    def _result(
        probe: Probe,
        status: TraceStatus,
        steps: list[Step],
        unauthorized_step: Step | None,
    ) -> TraceResult:
        return TraceResult(
            probe_id=probe.probe_id,
            target=probe.target,
            origin_domain=probe.origin_domain,
            status=status,
            steps=tuple(steps),
            unauthorized_step=unauthorized_step,
        )
