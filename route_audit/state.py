"""每域独立的前缀表、下一跳映射与最长前缀匹配（LPM）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count

from .exceptions import (
    DuplicateRouteError,
    RouteNotFoundError,
    UnknownDomainError,
)
from .model import Change, ChangeAction, Prefix, Route, TransitGrant


@dataclass
class NextHopMap:
    """下一跳名 -> 对等域的映射表，按路由域各自维护。

    题面要求“前缀重名时按当前路由域解释”：同一个下一跳名（例如 nh1）
    在不同域里可以指向不同的对等域，查询时必须显式传入当前域。
    映射值等于当前域自身时，表示本地终结（local delivery）。
    """

    _entries: dict[str, dict[str, str]] = field(default_factory=dict)

    def add(self, domain: str, next_hop: str, peer_domain: str) -> None:
        self._entries.setdefault(domain, {})[next_hop] = peer_domain

    def resolve(self, domain: str, next_hop: str) -> str | None:
        """在 domain 的解释空间里解析 next_hop；无法解释返回 None。"""
        return self._entries.get(domain, {}).get(next_hop)

    def known_domains(self) -> set[str]:
        return set(self._entries)


class RoutingState:
    """某个时刻的全网路由快照：各域独立前缀表 + 下一跳映射 + 中转许可。"""

    def __init__(
        self,
        domains: Iterable[str],
        next_hop_map: NextHopMap | None = None,
        grants: Iterable[TransitGrant] = (),
    ) -> None:
        self._domains: set[str] = set(domains)
        # domain -> route_id -> Route
        self._routes: dict[str, dict[str, Route]] = {d: {} for d in self._domains}
        # 单调插入序号，用于同前缀长度且 metric 相同时的确定性决胜（先到优先）
        self._sequence = count()
        self._insert_order: dict[str, int] = {}
        self.next_hop_map = next_hop_map or NextHopMap()
        self.grants: tuple[TransitGrant, ...] = tuple(grants)

    # ----- 域管理 ---------------------------------------------------------- #
    @property
    def domains(self) -> set[str]:
        return set(self._domains)

    def require_domain(self, domain: str) -> None:
        if domain not in self._domains:
            raise UnknownDomainError(f"未登记的路由域 {domain!r}")

    # ----- 路由写入 -------------------------------------------------------- #
    def add_route(self, route: Route) -> None:
        self.require_domain(route.domain)
        table = self._routes[route.domain]
        if route.route_id in table:
            raise DuplicateRouteError(
                f"域 {route.domain!r} 内 route_id={route.route_id!r} 已存在"
            )
        table[route.route_id] = route
        self._insert_order[route.route_id] = next(self._sequence)

    def withdraw_route(self, domain: str, route_id: str) -> Route:
        """撤下指定域内的路由并返回它；找不到则抛 RouteNotFoundError。"""
        self.require_domain(domain)
        table = self._routes[domain]
        if route_id not in table:
            raise RouteNotFoundError(
                f"域 {domain!r} 内找不到待撤下的 route_id={route_id!r}"
            )
        removed = table.pop(route_id)
        self._insert_order.pop(route_id, None)
        return removed

    def all_routes(self, domain: str) -> tuple[Route, ...]:
        return tuple(self._routes.get(domain, {}).values())

    # ----- 最长前缀匹配 ---------------------------------------------------- #
    def lookup(self, domain: str, target: Prefix) -> Route | None:
        """在 domain 的前缀表内对 target 做最长前缀匹配。

        选择顺序：
        1. 覆盖 target 的前缀中前缀长度最大者（LPM）；
        2. 长度相同取 metric 最小者；
        3. 仍相同取先注入者（稳定、可复现）。
        """
        self.require_domain(domain)
        candidates = [
            route
            for route in self._routes[domain].values()
            if route.prefix.contains(target)
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda r: (
                -r.prefix.prefix_length,
                r.metric,
                self._insert_order[r.route_id],
            )
        )
        return candidates[0]

    # ----- 中转许可 -------------------------------------------------------- #
    def transit_permitted(
        self, transit_domain: str, source: str, target: str
    ) -> bool:
        """transit_domain 是否持有把 source 流量转发给 target 的方向性许可。"""
        return any(
            grant.permits(transit_domain, source, target) for grant in self.grants
        )

    # ----- 变化应用 -------------------------------------------------------- #
    def apply_change(self, change: Change) -> None:
        if change.action is ChangeAction.WITHDRAW:
            self.withdraw_route(change.domain, change.route_id)
        else:
            if change.route is None:
                raise RouteNotFoundError(
                    f"变化 {change.change_id!r} 为 add 但未携带路由"
                )
            if change.route.route_id != change.route_id:
                raise RouteNotFoundError(
                    f"变化 {change.change_id!r} 的 route_id 与路由对象不一致"
                )
            self.add_route(change.route)
