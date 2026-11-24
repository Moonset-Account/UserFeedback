"""规划器的输入/输出数据模型与输入校验。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .prefix import BITS, Prefix, intervals_disjoint


@dataclass(frozen=True, slots=True)
class Upstream:
    """一条上游及其可委派前缀、每轮通告条数上限。"""

    name: str
    prefix: Prefix
    max_advertisements: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("上游名称不能为空")
        if self.max_advertisements < 0:
            raise ValueError("max_advertisements 不能为负")


@dataclass(frozen=True, slots=True)
class Tenant:
    """一个待换号租户。

    attributes:
        name:             租用户名。
        prefix_length:    新委派前缀需要的（对齐）长度 L。
        fixed:            不能移动、新/临时前缀都必须完整包含的子前缀列表。
        allowed_upstreams: 允许放置新前缀的上游名称；空表示所有上游皆可。
        old_prefix:       旧委派前缀（None 表示新租户，无旧地址）。
        old_upstream:     旧前缀所在上游名称。
        hold_rounds:      旧地址在新地址首通告后仍须继续通告的离散轮数 h>=1。
        policy:           路由策略标签；仅相同租户且策略相同的相邻子树可汇总。
    """

    name: str
    prefix_length: int
    fixed: tuple[Prefix, ...] = ()
    allowed_upstreams: tuple[str, ...] = ()
    old_prefix: Prefix | None = None
    old_upstream: str | None = None
    hold_rounds: int = 1
    policy: str = "default"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("租用户名不能为空")
        if not 0 <= self.prefix_length <= BITS:
            raise ValueError("prefix_length 必须在 [0, 128]")
        if self.hold_rounds < 1:
            raise ValueError("hold_rounds 必须 >= 1（旧地址至少双地址通告一轮）")
        if (self.old_prefix is None) != (self.old_upstream is None):
            raise ValueError("old_prefix 与 old_upstream 必须同时给出或同时省略")
        fixed = list(self.fixed)
        for i, pfx in enumerate(fixed):
            for other in fixed[i + 1 :]:
                if not intervals_disjoint(pfx.interval, other.interval):
                    raise ValueError(f"租户 {self.name} 的固定子前缀互相重叠")
        if self.old_prefix is not None:
            for pfx in fixed:
                if not self.old_prefix.contains(pfx):
                    raise ValueError(
                        f"租户 {self.name} 的固定子前缀 {pfx} 必须位于旧前缀 {self.old_prefix} 内"
                    )
        if self.prefix_length > BITS:
            raise ValueError("委派前缀长度不能超过 128")


@dataclass(frozen=True, slots=True)
class TempPrefix:
    """允许借用的临时前缀（迁移中转用）。"""

    name: str
    prefix: Prefix
    upstream: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("临时前缀名称不能为空")


@dataclass(frozen=True, slots=True)
class Problem:
    """一次规划的完整输入。"""

    upstreams: tuple[Upstream, ...]
    tenants: tuple[Tenant, ...]
    temp_prefixes: tuple[TempPrefix, ...] = ()

    def __post_init__(self) -> None:
        if not self.upstreams:
            raise ValueError("至少需要一条上游")
        if not self.tenants:
            raise ValueError("至少需要一个租户")
        if len(self.tenants) > 8:
            raise ValueError("最多支持八个租户")
        names: set[str] = set()
        for ups in self.upstreams:
            if ups.name in names:
                raise ValueError(f"上游重名: {ups.name}")
            names.add(ups.name)
        tenant_names: set[str] = set()
        for tnt in self.tenants:
            if tnt.name in tenant_names:
                raise ValueError(f"租户重名: {tnt.name}")
            tenant_names.add(tnt.name)
            if tnt.old_upstream is not None and tnt.old_upstream not in names:
                raise ValueError(f"租户 {tnt.name} 的旧上游 {tnt.old_upstream} 不存在")
            for ups_name in tnt.allowed_upstreams:
                if ups_name not in names:
                    raise ValueError(f"租户 {tnt.name} 允许的上游 {ups_name} 不存在")
            # 新前缀块不得大于任何一条允许上游，否则无从放置。
            allowed = tnt.allowed_upstreams or tuple(u.name for u in self.upstreams)
            need_size = 1 << (128 - tnt.prefix_length)
            if all(
                need_size > self.upstream_map[name].prefix.size
                for name in allowed
            ):
                raise ValueError(
                    f"租户 {tnt.name} 的请求前缀大于其所有允许上游，无法放置"
                )
        for tmp in self.temp_prefixes:
            if tmp.upstream not in names:
                raise ValueError(f"临时前缀 {tmp.name} 的上游 {tmp.upstream} 不存在")
        # 同一上游内的临时前缀之间不得重叠。
        by_up: dict[str, list[Prefix]] = {}
        for tmp in self.temp_prefixes:
            by_up.setdefault(tmp.upstream, []).append(tmp.prefix)
        for ups_name, pfxs in by_up.items():
            for i, pfx in enumerate(pfxs):
                for other in pfxs[i + 1 :]:
                    if not intervals_disjoint(pfx.interval, other.interval):
                        raise ValueError(f"上游 {ups_name} 的临时前缀互相重叠")
        # 旧前缀必须挂在声明的旧上游内。
        ups_map = {u.name: u for u in self.upstreams}
        for tnt in self.tenants:
            if tnt.old_prefix is not None:
                if not ups_map[tnt.old_upstream].prefix.contains(tnt.old_prefix):
                    raise ValueError(
                        f"租户 {tnt.name} 的旧前缀 {tnt.old_prefix} 不在上游 {tnt.old_upstream} 内"
                    )

    @property
    def upstream_map(self) -> dict[str, Upstream]:
        return {u.name: u for u in self.upstreams}


# ---------------------------------------------------------------------------
# 输出模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Advertisement:
    """某轮某上游的一条最小通告。"""

    round: int
    upstream: str
    prefix: Prefix
    tenant: str
    policy: str


@dataclass(frozen=True, slots=True)
class Event:
    """一次委派动作。

    kind:
        ``"assign"``  把 ``prefix`` 委派给租户（新前缀或借用的临时前缀）；
        ``"withdraw"`` 撤告租户持有的 ``prefix``。
    """

    round: int
    kind: str
    tenant: str
    upstream: str
    prefix: Prefix
    temporary: bool = False

    def __post_init__(self) -> None:
        if self.kind not in ("assign", "withdraw"):
            raise ValueError("事件 kind 只能是 assign/withdraw")


@dataclass(frozen=True, slots=True)
class Schedule:
    """规划结果。

    attributes:
        events:            按轮次排序的 assign/withdraw 事件。
        advertisements:    每轮每上游的最小通告集（精确汇总后）。
        makespan:          总轮数 T（最后一个事件所在轮）。
        dual_address_rounds: 所有租户双地址（新旧同时通告）轮数之和。
        placements:        租户最终前缀 ``{tenant: Prefix}``。
    """

    events: tuple[Event, ...]
    advertisements: tuple[Advertisement, ...]
    makespan: int
    dual_address_rounds: int
    placements: dict[str, Prefix] = field(default_factory=dict)

    def events_at(self, round_index: int) -> tuple[Event, ...]:
        return tuple(e for e in self.events if e.round == round_index)

    def advertisements_at(self, round_index: int, upstream: str) -> tuple[Advertisement, ...]:
        return tuple(
            a
            for a in self.advertisements
            if a.round == round_index and a.upstream == upstream
        )
