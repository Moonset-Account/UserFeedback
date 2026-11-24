"""前缀委派迁移排程搜索。

搜索目标（按优先级）：

1. 最早安全完成：最小化总轮数 ``makespan``；
2. 同轮数下最小化所有租户的双地址（两段前缀同时通告）轮数之和。

迁移语义（离散轮次）：

* 单跳：租户在第 r 轮拿到最终新前缀，旧前缀在第 r..r+h-1 轮继续通告
  （h = ``hold_rounds`` 个双地址轮），第 r+h-1 轮末撤告，第 r+h 轮起释放；
* 两跳（借用临时前缀）：先在第 r1 轮拿到临时前缀并按同样规则撤掉旧前缀，
  再在第 r2 轮拿到最终前缀并撤掉临时前缀；双地址轮共 2h；
* 一段前缀在撤告轮（end）结束前不能转给其他租户，最早 r = end+1 可复用；
* 每轮每条上游“精确汇总后”的通告条数不得超过其上限；
* 新前缀必须与租户自身旧前缀不同（换号不能原地保留）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .aggregate import minimal_advertisements
from .model import Advertisement, Event, Problem, Schedule, Tenant
from .prefix import BITS, Prefix, intervals_disjoint, merge_intervals

_MAX_SLOT_ENUM = 4096
_HORIZON_SENTINEL = -1


class NoFeasibleSchedule(Exception):
    """在给定上游容量、固定点与借用前缀约束下不存在可行排程。"""


@dataclass(slots=True)
class Alloc:
    """一段存活中的前缀委派。end 为最后一个通告轮（撤告在该轮末）。"""

    tenant: str
    upstream: str
    prefix: Prefix
    temporary: bool
    start: int
    end: int
    temp_name: str | None = None

    @property
    def open(self) -> bool:
        return self.end == _HORIZON_SENTINEL


# ---------------------------------------------------------------------------
# 槽位枚举（纯区间端点运算，不按地址展开）
# ---------------------------------------------------------------------------
def _blocked_intervals(
    problem: Problem,
    tenant: Tenant,
    active: dict[str, list[Alloc]],
    at_round: int,
) -> list[tuple[int, int]]:
    blocked: list[tuple[int, int]] = []
    for other in problem.tenants:
        if other.name == tenant.name:
            continue
        for alloc in active.get(other.name, ()):
            end = 10**9 if alloc.end == _HORIZON_SENTINEL else alloc.end
            if alloc.start <= at_round <= end:
                blocked.append(alloc.prefix.interval)
    return merge_intervals(blocked)


def _hits_blocked(interval: tuple[int, int], blocked: list[tuple[int, int]]) -> bool:
    return any(not intervals_disjoint(interval, biv) for biv in blocked)


def _aligned_slots_in(
    upstream_prefix: Prefix,
    length: int,
    blocked: list[tuple[int, int]],
    must_cover: list[Prefix],
    *,
    limit: int = _MAX_SLOT_ENUM,
) -> list[Prefix]:
    """枚举上游内满足长度、固定点覆盖且不与占用区间相交的对齐槽位。"""
    block_size = 1 << (BITS - length)
    unit = upstream_prefix.unit
    if block_size > upstream_prefix.size:
        return []
    first = upstream_prefix.addr
    last = upstream_prefix.end - block_size

    if must_cover:
        lo = min(p.addr for p in must_cover)
        anchor = (lo // block_size) * block_size
        candidate_addrs = [anchor, anchor + block_size]
        candidate_addrs = [a for a in candidate_addrs if first <= a <= last]
    else:
        count = (last - first) // block_size + 1
        if count > limit:
            raise NoFeasibleSchedule(
                f"上游 {upstream_prefix} 空闲槽位过多（{count}），请通过固定点或临时前缀收窄搜索"
            )
        candidate_addrs = [first + i * block_size for i in range(count)]

    slots: list[Prefix] = []
    seen: set[Prefix] = set()
    for addr in candidate_addrs:
        pfx = Prefix(addr, length, block_size, unit)
        if pfx in seen or not upstream_prefix.contains(pfx):
            continue
        seen.add(pfx)
        if _hits_blocked(pfx.interval, blocked):
            continue
        if any(not pfx.contains(c) for c in must_cover):
            continue
        slots.append(pfx)
    return slots


def _temp_slots(
    problem: Problem,
    tenant: Tenant,
    active: dict[str, list[Alloc]],
    at_round: int,
    used_temp_names: set[str],
) -> list[tuple[str, Prefix, str]]:
    blocked = _blocked_intervals(problem, tenant, active, at_round)
    out: list[tuple[str, Prefix, str]] = []
    for tmp in problem.temp_prefixes:
        if tmp.name in used_temp_names:
            continue
        slots = _aligned_slots_in(tmp.prefix, tenant.prefix_length, blocked, list(tenant.fixed))
        for slot in slots:
            if tenant.old_prefix is not None and slot == tenant.old_prefix:
                continue
            out.append((tmp.name, slot, tmp.upstream))
    return out


# ---------------------------------------------------------------------------
# 每轮通告与上限
# ---------------------------------------------------------------------------
def _alive(alloc: Alloc, round_index: int) -> bool:
    return alloc.start <= round_index and (
        alloc.end == _HORIZON_SENTINEL or round_index <= alloc.end
    )


def _round_holdings(
    problem: Problem,
    active: dict[str, list[Alloc]],
    round_index: int,
) -> dict[str, list[tuple[Prefix, str, str]]]:
    per_up: dict[str, list[tuple[Prefix, str, str]]] = {}
    for tnt in problem.tenants:
        for alloc in active.get(tnt.name, ()):
            if _alive(alloc, round_index):
                per_up.setdefault(alloc.upstream, []).append(
                    (alloc.prefix, tnt.name, tnt.policy)
                )
    return per_up


def _caps_ok(problem: Problem, active: dict[str, list[Alloc]], round_index: int) -> bool:
    for ups_name, entries in _round_holdings(problem, active, round_index).items():
        cap = problem.upstream_map[ups_name].max_advertisements
        if len(minimal_advertisements(entries)) > cap:
            return False
    return True


# ---------------------------------------------------------------------------
# 搜索状态
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _Node:
    active: dict[str, list[Alloc]]
    done: set[str]
    events: list[Event]
    dual_used: dict[str, int]
    used_temp: set[str]
    next_round: int


def _clone(node: _Node) -> _Node:
    # Alloc 会在 _apply_assign 中被原地改写 end，必须逐对象深拷贝，
    # 否则兄弟递归分支会读到被污染的持有状态。
    return _Node(
        active={
            k: [
                Alloc(a.tenant, a.upstream, a.prefix, a.temporary, a.start, a.end, a.temp_name)
                for a in v
            ]
            for k, v in node.active.items()
        },
        done=set(node.done),
        events=list(node.events),
        dual_used=dict(node.dual_used),
        used_temp=set(node.used_temp),
        next_round=node.next_round,
    )


def _tenant_complete(tnt: Tenant, allocs: list[Alloc], at_round: int) -> bool:
    """at_round 轮时租户是否只剩最终前缀存活（旧/临时持有均已撤告）。"""
    has_final = False
    for a in allocs:
        if a.end != _HORIZON_SENTINEL and at_round > a.end:
            continue
        if a.temporary:
            return False
        if tnt.old_prefix is not None and a.prefix == tnt.old_prefix:
            return False
        has_final = True
    return has_final


def _has_final(tnt: Tenant, allocs: list[Alloc]) -> bool:
    return any(
        (not a.temporary) and (tnt.old_prefix is None or a.prefix != tnt.old_prefix)
        for a in allocs
    )


def _final_options(
    problem: Problem,
    tnt: Tenant,
    blocked: list[tuple[int, int]],
) -> list[tuple[Prefix, str]]:
    allowed = tnt.allowed_upstreams or tuple(u.name for u in problem.upstreams)
    options: list[tuple[Prefix, str]] = []
    for ups_name in allowed:
        ups = problem.upstream_map[ups_name]
        for slot in _aligned_slots_in(ups.prefix, tnt.prefix_length, blocked, list(tnt.fixed)):
            if tnt.old_prefix is not None and slot == tnt.old_prefix:
                continue
            options.append((slot, ups_name))
    options.sort(key=lambda item: (item[1], item[0].addr))
    return options


def _apply_assign(
    problem: Problem,
    node: _Node,
    tnt: Tenant,
    prefix: Prefix,
    upstream: str,
    r: int,
    t_max: int,
    *,
    temporary: bool,
    temp_name: str | None,
) -> _Node | None:
    child = _clone(node)
    allocs = child.active.setdefault(tnt.name, [])

    close_end = r + tnt.hold_rounds - 1
    if close_end > t_max:
        return None

    for a in allocs:
        if a.open:
            a.end = close_end
            child.dual_used[tnt.name] += tnt.hold_rounds
            child.events.append(
                Event(close_end, "withdraw", tnt.name, a.upstream, a.prefix, a.temporary)
            )

    allocs.append(
        Alloc(tnt.name, upstream, prefix, temporary, r, _HORIZON_SENTINEL, temp_name)
    )
    if temporary:
        assert temp_name is not None
        child.used_temp.add(temp_name)
    child.events.append(Event(r, "assign", tnt.name, upstream, prefix, temporary))
    child.next_round = r

    check_until = max([r] + [a.end for a in allocs if a.end != _HORIZON_SENTINEL])
    for round_index in range(r, min(check_until, t_max) + 1):
        if not _caps_ok(problem, child.active, round_index):
            return None
    return child


def _memo_key(node: _Node) -> tuple:
    allocs = tuple(
        sorted(
            (a.tenant, a.upstream, a.prefix.addr, a.prefix.length, a.start, a.end, a.temporary)
            for allocs in node.active.values()
            for a in allocs
        )
    )
    return allocs, tuple(sorted(node.done)), tuple(sorted(node.used_temp)), node.next_round


def _earliest_wait_target(node: _Node, t_max: int) -> int:
    rounds = []
    for allocs in node.active.values():
        for a in allocs:
            if a.end != _HORIZON_SENTINEL and a.end >= node.next_round:
                rounds.append(a.end + 1)
    candidates = [r for r in rounds if r <= t_max + 1]
    return min(candidates) if candidates else t_max + 1


def plan(problem: Problem) -> Schedule:
    """求最早安全完成、其次双地址轮数最少的排程（单次全量搜索）。"""
    t_max = _upper_bound_horizon(problem)
    result = _search(problem, t_max, full=True)
    if result is None:
        raise NoFeasibleSchedule("在搜索时限内找不到满足全部约束的排程")
    return result[0]


def _upper_bound_horizon(problem: Problem) -> int:
    per_tenant = 2 * (max(t.hold_rounds for t in problem.tenants) + 1)
    return len(problem.tenants) * per_tenant + 2


def _search(
    problem: Problem,
    t_max: int,
    *,
    full: bool,
) -> tuple[Schedule, int] | None:
    initial_active: dict[str, list[Alloc]] = {}
    for tnt in problem.tenants:
        if tnt.old_prefix is not None:
            initial_active[tnt.name] = [
                Alloc(tnt.name, tnt.old_upstream, tnt.old_prefix, False, 1, _HORIZON_SENTINEL)
            ]

    root = _Node(
        active=initial_active,
        done=set(),
        events=[],
        dual_used={t.name: 0 for t in problem.tenants},
        used_temp=set(),
        next_round=1,
    )

    best_schedule: Schedule | None = None
    best_key: tuple[int, int] | None = None
    memo: set[tuple] = set()

    def dfs(node: _Node) -> None:
        nonlocal best_schedule, best_key
        if len(node.done) == len(problem.tenants):
            schedule = _materialize(problem, node)
            dual_total = sum(node.dual_used.values())
            key = (schedule.makespan, dual_total)
            if best_key is None or key < best_key:
                best_schedule = schedule
                best_key = key
            return
        if node.next_round > t_max:
            return
        # 深拷贝保证 Alloc 状态独立后，含 next_round 的状态指纹是安全剪枝键：
        # 相同存活持有/完成集合/已用临时前缀/时间轮次的后续可行域完全一致。
        mkey = _memo_key(node)
        if mkey in memo:
            return
        memo.add(mkey)
        r = node.next_round

        # 1) 完成所有已可结束的租户。
        completable = [
            tnt.name
            for tnt in problem.tenants
            if tnt.name not in node.done
            and _has_final(tnt, node.active.get(tnt.name, []))
            and _tenant_complete(tnt, node.active.get(tnt.name, []), r)
        ]
        if completable:
            child = _clone(node)
            child.done.update(completable)
            dfs(child)
            if not full and best_schedule is not None:
                return

        # 2) 仍有租户在等待撤告：若还有未迁移租户可在当前轮 assign，则让分支
        #    3 继续并行处理；否则推进到最早撤告完成轮。
        waiting = any(
            tnt.name not in node.done
            and _has_final(tnt, node.active.get(tnt.name, []))
            and not _tenant_complete(tnt, node.active.get(tnt.name, []), r)
            for tnt in problem.tenants
        )
        ready_to_assign = any(
            tnt.name not in node.done
            and not _has_final(tnt, node.active.get(tnt.name, []))
            for tnt in problem.tenants
        )
        if waiting and not ready_to_assign:
            target = _earliest_wait_target(node, t_max)
            if target > r:
                child = _clone(node)
                child.next_round = min(target, t_max + 1)
                dfs(child)
            return

        # 3) 同轮 assign：嵌套累积递归。每次选一个尚未拿到最终前缀的租户，
        #    在子状态里继续选下一个，从而正确累积同轮多租户动作，并枚举
        #    “哪些租户排在同一轮”的全部子集；也可选择本轮不再 assign。
        pending = [
            tnt for tnt in problem.tenants
            if tnt.name not in node.done and not _has_final(tnt, node.active.get(tnt.name, []))
        ]
        branched = False
        for tnt in pending:
            allocs = node.active.get(tnt.name, [])
            has_temp = any(a.temporary for a in allocs)
            blocked = _blocked_intervals(problem, tnt, node.active, r)

            moves: list[tuple[Prefix, str, bool, str | None]] = [
                (prefix, upstream, False, None)
                for prefix, upstream in _final_options(problem, tnt, blocked)
            ]
            if not has_temp:
                moves += [
                    (prefix, upstream, True, temp_name)
                    for temp_name, prefix, upstream in _temp_slots(
                        problem, tnt, node.active, r, node.used_temp
                    )
                ]

            for prefix, upstream, temporary, temp_name in moves:
                child = _apply_assign(
                    problem, node, tnt, prefix, upstream, r, t_max,
                    temporary=temporary, temp_name=temp_name,
                )
                if child is None:
                    continue
                branched = True
                dfs(child)  # 子状态里继续同轮 assign 或进入后续轮
                if not full and best_schedule is not None:
                    return

        # 4) 当前轮不再 assign：推进到下一个事件轮（撤告完成或下一轮）。
        if not branched:
            target = _earliest_wait_target(node, t_max)
            nxt = target if target > r else min(r + 1, t_max + 1)
            if nxt > r:
                child = _clone(node)
                child.next_round = nxt
                dfs(child)

    dfs(root)
    if best_schedule is None:
        return None
    return best_schedule, best_key[1]


def _materialize(problem: Problem, node: _Node) -> Schedule:
    events = sorted(
        node.events,
        key=lambda e: (e.round, 0 if e.kind == "assign" else 1, e.tenant, e.prefix.addr),
    )
    makespan = max((e.round for e in events), default=1)

    advertisements: list[Advertisement] = []
    for round_index in range(1, makespan + 1):
        for ups_name, entries in _round_holdings(problem, node.active, round_index).items():
            for pfx, tenant, pol in minimal_advertisements(entries):
                advertisements.append(Advertisement(round_index, ups_name, pfx, tenant, pol))
    advertisements.sort(key=lambda a: (a.round, a.upstream, a.prefix.addr))

    placements: dict[str, Prefix] = {}
    for tnt in problem.tenants:
        finals = [
            a
            for a in node.active[tnt.name]
            if not a.temporary and (tnt.old_prefix is None or a.prefix != tnt.old_prefix)
        ]
        if finals:
            placements[tnt.name] = finals[-1].prefix

    return Schedule(
        events=tuple(events),
        advertisements=tuple(advertisements),
        makespan=makespan,
        dual_address_rounds=sum(node.dual_used.values()),
        placements=placements,
    )
