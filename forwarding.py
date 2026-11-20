"""IPv4 转发选择模块。

路由表中同时存在两类路由：

* 直连路由：下一跳就是一个本地接口，只要接口处于启用状态即可作为出口；
* 递归路由：下一跳是一个 IP 地址，需要再次查表才能落到真正的出接口。

对同一目标的候选路由按以下优先级依次尝试：

1. 最长前缀匹配（prefixlen 越大越优先）；
2. 较低的开销（metric 越小越优先）；
3. 路由编号（route_id 越小越优先）。

递归解析时，当前候选链上已经使用过的路由会被放入一个「沿链传递的
排除集」：若递归再次需要同一条路由，说明这条候选链成环，仅放弃该链，
回溯后仍可尝试下一候选。排除集只存在于单次解析链上（每次递归传递
不可变副本），查询之间不保留任何失败状态，因此某条路由即使上一次
参与过环，也不会被永久屏蔽。

接口上下线、路由插入或撤回都会立即反映到后续查询中；查询返回出接口
以及完整的解析链（依次使用了哪些路由、各自是直连还是递归）。
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Union

IPAddressLike = Union[str, ipaddress.IPv4Address]
NetworkLike = Union[str, ipaddress.IPv4Network]


@dataclass(frozen=True)
class Route:
    """一条路由。

    恰好满足以下二者之一：

    * ``interface`` 非空 —— 直连路由，出接口即 ``interface``；
    * ``next_hop`` 非空 —— 递归路由，需要对 ``next_hop`` 再次查表。
    """

    route_id: int
    prefix: ipaddress.IPv4Network
    metric: int = 0
    interface: Optional[str] = None
    next_hop: Optional[ipaddress.IPv4Address] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "prefix", ipaddress.IPv4Network(self.prefix))
        if self.next_hop is not None:
            object.__setattr__(
                self, "next_hop", ipaddress.IPv4Address(self.next_hop)
            )
        if (self.interface is None) == (self.next_hop is None):
            raise ValueError(
                "route must have exactly one of `interface` (direct) "
                "or `next_hop` (recursive)"
            )
        if self.metric < 0:
            raise ValueError("metric must be non-negative")

    @property
    def is_direct(self) -> bool:
        return self.interface is not None


@dataclass(frozen=True)
class ResolutionStep:
    """解析链中的一步：使用了哪条路由，以及该步的性质。"""

    route: Route
    # True 表示该步直接落到出接口；False 表示该步是递归跳转，
    # 后续步骤是对 route.next_hop 继续解析的结果。
    direct: bool


@dataclass(frozen=True)
class LookupResult:
    """一次成功查询的结果：最终出接口 + 完整解析链。"""

    egress_interface: str
    chain: List[ResolutionStep]

    @property
    def route_ids(self) -> List[int]:
        """解析链上依次使用的路由编号，便于断言与调试。"""
        return [step.route.route_id for step in self.chain]


class ForwardingTable:
    """IPv4 转发表：接口状态 + 路由集合 + 递归最长前缀查询。"""

    def __init__(self) -> None:
        self._routes: Dict[int, Route] = {}
        # 接口名 -> 是否启用（up）。未登记的接口视为 down。
        self._interfaces: Dict[str, bool] = {}

    # ------------------------------------------------------------------
    # 接口管理
    # ------------------------------------------------------------------
    def add_interface(self, name: str, enabled: bool = True) -> None:
        self._interfaces[name] = bool(enabled)

    def remove_interface(self, name: str) -> None:
        self._interfaces.pop(name, None)

    def set_interface_enabled(self, name: str, enabled: bool) -> None:
        """接口上线/下线。未登记的接口在下线时保持不存在、上线时登记。"""
        if name not in self._interfaces and not enabled:
            return
        self._interfaces[name] = bool(enabled)

    def interface_enabled(self, name: str) -> bool:
        return self._interfaces.get(name, False)

    # ------------------------------------------------------------------
    # 路由管理
    # ------------------------------------------------------------------
    def add_route(
        self,
        route_id: int,
        prefix: NetworkLike,
        metric: int = 0,
        interface: Optional[str] = None,
        next_hop: Optional[IPAddressLike] = None,
    ) -> Route:
        """插入（或替换同编号）一条路由，返回创建的 Route。"""
        route = Route(
            route_id=route_id,
            prefix=ipaddress.IPv4Network(prefix),
            metric=metric,
            interface=interface,
            next_hop=(
                ipaddress.IPv4Address(next_hop) if next_hop is not None else None
            ),
        )
        self._routes[route.route_id] = route
        return route

    def remove_route(self, route_id: int) -> None:
        """撤回一条路由；不存在时静默忽略。"""
        self._routes.pop(route_id, None)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def lookup(self, destination: IPAddressLike) -> Optional[LookupResult]:
        """查询目的地址的出接口与完整解析链；无可达出口时返回 None。

        每次查询都从空白状态开始，不缓存任何失败信息，因此历史查询中
        遇到的环不会影响后续查询。
        """
        dest = ipaddress.IPv4Address(destination)
        chain: List[ResolutionStep] = []
        egress = self._resolve(dest, frozenset(), chain)
        if egress is None:
            return None
        return LookupResult(egress_interface=egress, chain=chain)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _candidates(
        self, dest: ipaddress.IPv4Address, excluded: FrozenSet[int]
    ) -> List[Route]:
        """匹配 dest 且不在排除集中的候选，按优先级排序。"""
        matches = [
            r
            for r in self._routes.values()
            if r.route_id not in excluded and dest in r.prefix
        ]
        matches.sort(key=lambda r: (-r.prefix.prefixlen, r.metric, r.route_id))
        return matches

    def _resolve(
        self,
        dest: ipaddress.IPv4Address,
        excluded: FrozenSet[int],
        chain: List[ResolutionStep],
    ) -> Optional[str]:
        """在当前候选链的排除集下解析 dest。

        成功时把本层用到的步骤追加到 ``chain`` 并返回出接口名；
        失败时返回 None，且保证 ``chain`` 未被污染（子链先暂存，
        成功后才合并）。
        """
        for route in self._candidates(dest, excluded):
            if route.is_direct:
                # 直连路由：接口必须处于启用状态，否则尝试下一候选。
                if self._interfaces.get(route.interface, False):
                    chain.append(ResolutionStep(route=route, direct=True))
                    return route.interface
                continue
            # 递归路由：把本路由加入排除集后再向下解析。排除集沿
            # 当前链传递（不可变副本），因此环只让这条链失败，
            # 回溯后兄弟候选仍可使用这些路由。
            sub_chain: List[ResolutionStep] = []
            egress = self._resolve(
                route.next_hop, excluded | {route.route_id}, sub_chain
            )
            if egress is not None:
                chain.append(ResolutionStep(route=route, direct=False))
                chain.extend(sub_chain)
                return egress
        return None
