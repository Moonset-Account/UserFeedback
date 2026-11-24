"""路由隔离审查的核心数据模型。

本模块只做防御性的路由规划判断：给定各租户的前缀表、下一跳对等域映射、
按方向批准的中转许可以及路由变化序列，核对源前缀的出域路径，找出首个
未授权的跨域中转步骤。它不会生成、执行任何真实网络动作或利用载荷。
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from enum import Enum

from .exceptions import InvalidPrefixError


# --------------------------------------------------------------------------- #
# 前缀
# --------------------------------------------------------------------------- #
class Prefix:
    """不可变的 IP 前缀，支持最长前缀匹配（LPM）所需的包含判断。

    ``strict=False``：允许主机位非零的写法（例如 10.0.0.5/24 会被规范化为
    10.0.0.0/24），与真实设备接收路由的行为保持一致。
    """

    def __init__(self, text: str) -> None:
        if not isinstance(text, str):
            raise InvalidPrefixError(f"前缀必须是字符串，收到 {type(text).__name__}")
        try:
            self._network = ipaddress.ip_network(text.strip(), strict=False)
        except ValueError as exc:
            raise InvalidPrefixError(f"非法前缀 {text!r}: {exc}") from exc

    @property
    def text(self) -> str:
        return str(self._network)

    @property
    def prefix_length(self) -> int:
        return self._network.prefixlen

    @property
    def version(self) -> int:
        return self._network.version

    def contains(self, other: "Prefix") -> bool:
        """self 是否覆盖 other（含自身）。不同地址族返回 False。"""
        if self.version != other.version:
            return False
        # 子网的网络地址不小于外层网络地址，广播地址不大于外层广播地址
        return int(other._network.network_address) >= int(
            self._network.network_address
        ) and int(other._network.broadcast_address) <= int(
            self._network.broadcast_address
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Prefix):
            return NotImplemented
        return self._network == other._network

    def __hash__(self) -> int:
        return hash(self._network)

    def __lt__(self, other: "Prefix") -> bool:
        if self.version != other.version:
            return self.version < other.version
        return (
            int(self._network.network_address),
            self._network.prefixlen,
        ) < (int(other._network.network_address), other._network.prefixlen)

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return f"Prefix({self.text!r})"


# --------------------------------------------------------------------------- #
# 路由 / 下一跳 / 中转许可
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Route:
    """某条路由：属于哪个域、宣告的前缀、下一跳名与管理代价。

    next_hop 为 None 表示本地直连/终结（local delivery）。
    metric 越小越优，用于同前缀长度时的确定性决胜。
    """

    route_id: str
    domain: str
    prefix: Prefix
    next_hop: str | None
    metric: int = 100


class TransitDirection(Enum):
    """中转许可方向：from_domain -> to_domain，由 transit_domain 代为转发。"""

    BIDIRECTIONAL = "bidirectional"
    FORWARD = "forward"  # 仅允许 from -> to
    REVERSE = "reverse"  # 仅允许 to -> from


@dataclass(frozen=True)
class TransitGrant:
    """一条明确批准的中转方向许可。

    任一端写 "*" 表示通配（测试用，默认场景不会使用）。
    """

    transit_domain: str
    from_domain: str
    to_domain: str
    direction: TransitDirection = TransitDirection.BIDIRECTIONAL

    def permits(self, transit_domain: str, source: str, target: str) -> bool:
        """transit_domain 是否被允许把 source 的流量转发给 target。"""
        if transit_domain != self.transit_domain:
            return False

        def match(granted: str, actual: str) -> bool:
            return granted == "*" or granted == actual

        if self.direction is TransitDirection.BIDIRECTIONAL:
            return (match(self.from_domain, source) and match(self.to_domain, target)) or (
                match(self.from_domain, target) and match(self.to_domain, source)
            )
        if self.direction is TransitDirection.FORWARD:
            return match(self.from_domain, source) and match(self.to_domain, target)
        # REVERSE
        return match(self.from_domain, target) and match(self.to_domain, source)


# --------------------------------------------------------------------------- #
# 路由变化
# --------------------------------------------------------------------------- #
class ChangeAction(Enum):
    WITHDRAW = "withdraw"
    ADD = "add"


@dataclass(frozen=True)
class Change:
    """一次路由变化。

    withdraw：用 route_id 在指定域内撤下专用路由（prefix/next_hop 仅作信息记录）。
    add：携带完整 Route 对象重新注入。
    """

    change_id: str
    action: ChangeAction
    domain: str
    route_id: str
    route: Route | None = None


# --------------------------------------------------------------------------- #
# 追踪结果
# --------------------------------------------------------------------------- #
class TraceStatus(Enum):
    DELIVERED = "delivered"            # 在某域本地终结
    NO_ROUTE = "no_route"              # 当前域没有任何覆盖前缀
    UNRESOLVED_NEXT_HOP = "unresolved_next_hop"  # 下一跳名在当前域无法解释
    LOOP = "loop"                      # 重复命中同一 (域, 路由)
    UNAUTHORIZED_TRANSIT = "unauthorized_transit"  # 首个未授权跨域中转
    MAX_HOPS = "max_hops"              # 超过安全跳数上限，防御性截断


@dataclass(frozen=True)
class Step:
    """路径上的一跳：在 domain 内由 route 选中，经 next_hop 转发到 peer。

    peer 为 None 表示本地终结。authorized 仅对跨域中转跳有意义；
    源域的第一跳（出域）不视为为他人中转，恒为 True。
    """

    order: int
    domain: str
    route: Route
    next_hop: str | None
    peer: str | None
    authorized: bool
    is_origin_egress: bool = False


@dataclass(frozen=True)
class TraceResult:
    """对单个前缀执行一次完整逐跳核对的结果。"""

    probe_id: str
    target: Prefix
    origin_domain: str
    status: TraceStatus
    steps: tuple[Step, ...]
    unauthorized_step: Step | None = None

    @property
    def is_compliant(self) -> bool:
        return self.status is TraceStatus.DELIVERED


@dataclass(frozen=True)
class Finding:
    """一条隔离违规结论（题面要求的三元组都在里面）。

    - affected_prefix：走了未批准备用中转的那一段前缀（探测目标）
    - first_unauthorized_step：首个未授权的跨域步骤
    - trigger_change_id：由哪次路由变化引出；基线已存在的问题为 None
    """

    probe_id: str
    affected_prefix: Prefix
    origin_domain: str
    first_unauthorized_step: Step
    trigger_change_id: str | None
    status: TraceStatus = TraceStatus.UNAUTHORIZED_TRANSIT


@dataclass(frozen=True)
class Probe:
    """一次审查请求：从 origin_domain 出发，核对 target 前缀的出域路径。"""

    probe_id: str
    origin_domain: str
    target: Prefix
