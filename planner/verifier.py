"""排程结果验证。

两套验证：

* :func:`verify_schedule` —— 符号验证，只做区间端点/trie 运算，
  可直接用于 128 位真实前缀，不按地址数展开；
* :func:`verify_small_projection` —— 在小地址投影（宽度 W，2**W 个地址）
  上逐地址枚举，验证全程可达、归属唯一（无重叠）、固定点不移动。

两者对同一排程应给出一致结论，测试中交叉使用。
"""

from __future__ import annotations

from dataclasses import dataclass

from .aggregate import decompose_interval, minimal_advertisements
from .model import Event, Problem, Schedule
from .prefix import BITS, Prefix, intervals_disjoint, merge_intervals


@dataclass(frozen=True, slots=True)
class VerificationReport:
    ok: bool
    errors: tuple[str, ...]

    def raise_if_failed(self) -> None:
        if not self.ok:
            raise AssertionError("排程验证失败:\n  - " + "\n  - ".join(self.errors))


def _alive_window(events: tuple[Event, ...], target: Event) -> tuple[int, int]:
    start = target.round
    end = None
    for ev in events:
        if (
            ev.kind == "withdraw"
            and ev.tenant == target.tenant
            and ev.prefix == target.prefix
            and ev.round >= start
        ):
            end = ev.round
            break
    return start, end if end is not None else 10**9


def verify_schedule(problem: Problem, schedule: Schedule) -> VerificationReport:
    """对 128 位排程做纯符号验证（不枚举地址）。"""
    errors: list[str] = []
    ups_map = problem.upstream_map
    tenant_map = {t.name: t for t in problem.tenants}
    events = schedule.events

    # ---- 事件基本合法性 -------------------------------------------------
    assigned: dict[tuple[str, Prefix], Event] = {}
    for ev in events:
        if ev.tenant not in tenant_map:
            errors.append(f"轮 {ev.round}: 未知租户 {ev.tenant}")
            continue
        if ev.upstream not in ups_map:
            errors.append(f"轮 {ev.round}: 未知上游 {ev.upstream}")
            continue
        if ev.kind == "assign":
            key = (ev.tenant, ev.prefix)
            if key in assigned:
                errors.append(f"轮 {ev.round}: 租户 {ev.tenant} 重复委派 {ev.prefix}")
            assigned[key] = ev
            if not ups_map[ev.upstream].prefix.contains(ev.prefix):
                errors.append(
                    f"轮 {ev.round}: {ev.prefix} 不位于上游 {ev.upstream} {ups_map[ev.upstream].prefix} 内"
                )
            if ev.prefix.length != tenant_map[ev.tenant].prefix_length:
                errors.append(
                    f"轮 {ev.round}: {ev.tenant} 的前缀长度 {ev.prefix.length} 与要求不符"
                )

    # ---- 重建每段委派的存活窗口 ----------------------------------------
    # 旧前缀在第 1 轮前就已持有（无 assign 事件），先播种其窗口。
    windows: list[dict] = []
    for tnt in problem.tenants:
        if tnt.old_prefix is None:
            continue
        wd = next(
            (
                e for e in events
                if e.kind == "withdraw" and e.tenant == tnt.name and e.prefix == tnt.old_prefix
            ),
            None,
        )
        windows.append(
            {
                "tenant": tnt.name,
                "prefix": tnt.old_prefix,
                "upstream": tnt.old_upstream,
                "start": 1,
                "end": wd.round if wd is not None else 10**9,
                "temporary": False,
            }
        )

    for (tenant, pfx), ev in assigned.items():
        start, end = _alive_window(events, ev)
        withdraws = [
            e for e in events
            if e.kind == "withdraw" and e.tenant == tenant and e.prefix == pfx
        ]
        if len(withdraws) > 1:
            errors.append(f"{tenant} 的 {pfx} 存在多次撤告")
        windows.append(
            {
                "tenant": tenant,
                "prefix": pfx,
                "upstream": ev.upstream,
                "start": start,
                "end": end,
                "temporary": ev.temporary,
            }
        )

    # ---- 每租户生命周期：固定点、hold、最终前缀、换号 ------------------
    for tnt in problem.tenants:
        mine = [w for w in windows if w["tenant"] == tnt.name]
        if not mine:
            errors.append(f"租户 {tnt.name} 没有任何委派事件")
            continue
        finals = [
            w for w in mine
            if not w["temporary"] and (tnt.old_prefix is None or w["prefix"] != tnt.old_prefix)
        ]
        if not finals:
            errors.append(f"租户 {tnt.name} 从未获得最终新前缀")
            continue
        if len(finals) > 1:
            errors.append(f"租户 {tnt.name} 获得了多个最终前缀")
        final = finals[0]
        for w in mine:
            for fixed in tnt.fixed:
                if not w["prefix"].contains(fixed):
                    errors.append(
                        f"租户 {tnt.name} 的持有前缀 {w['prefix']} 未包含固定子前缀 {fixed}"
                    )
            if w["upstream"] not in (tnt.allowed_upstreams or tuple(ups_map)) and not w["temporary"]:
                if w is final:
                    errors.append(f"租户 {tnt.name} 的最终前缀位于未授权上游 {w['upstream']}")
        if tnt.old_prefix is not None:
            if final["prefix"] == tnt.old_prefix:
                errors.append(f"租户 {tnt.name} 的新前缀与旧前缀相同，未真正换号")
            old_windows = [
                w for w in mine
                if w["prefix"] == tnt.old_prefix and not w["temporary"]
            ]
            if not old_windows:
                errors.append(f"租户 {tnt.name} 的旧前缀缺少撤告记录")
            else:
                old = old_windows[0]
                if old["end"] == 10**9:
                    errors.append(f"租户 {tnt.name} 的旧前缀从未撤告")
                else:
                    dual_rounds = old["end"] - final["start"] + 1
                    if dual_rounds != tnt.hold_rounds:
                        errors.append(
                            f"租户 {tnt.name} 双地址轮数 {dual_rounds} != hold_rounds {tnt.hold_rounds}"
                        )
        # 临时前缀必须来自输入清单且在最终前缀到达后按 hold 撤告。
        temps = [w for w in mine if w["temporary"]]
        temp_names = {t.name: t for t in problem.temp_prefixes}
        for w in temps:
            matched = [
                t for t in problem.temp_prefixes
                if t.prefix.contains(w["prefix"]) and t.upstream == w["upstream"]
            ]
            if not matched:
                errors.append(f"租户 {tnt.name} 使用了未授权的临时前缀 {w['prefix']}")
            if w["end"] == 10**9:
                errors.append(f"租户 {tnt.name} 的临时前缀 {w['prefix']} 未撤告")
            else:
                dual = w["end"] - final["start"] + 1
                if dual != tnt.hold_rounds:
                    errors.append(
                        f"租户 {tnt.name} 临时/最终双地址轮数 {dual} != {tnt.hold_rounds}"
                    )

    # ---- 跨租户重叠：撤告轮结束前不得转给别人 --------------------------
    for i, wa in enumerate(windows):
        for wb in windows[i + 1 :]:
            if wa["tenant"] == wb["tenant"]:
                continue
            if intervals_disjoint(wa["prefix"].interval, wb["prefix"].interval):
                continue
            overlap_start = max(wa["start"], wb["start"])
            overlap_end = min(wa["end"], wb["end"])
            if overlap_start <= overlap_end:
                errors.append(
                    f"前缀 {wa['prefix']} 在轮 {overlap_round_text(overlap_start, overlap_end)} "
                    f"被 {wa['tenant']} 与 {wb['tenant']} 同时持有"
                )

    # ---- 每轮每上游：最小通告集与上限 ----------------------------------
    makespan = schedule.makespan
    rebuilt_ads = set()
    for r in range(1, makespan + 1):
        per_up: dict[str, list[tuple[Prefix, str, str]]] = {}
        for w in windows:
            if w["start"] <= r <= w["end"]:
                tnt = tenant_map[w["tenant"]]
                per_up.setdefault(w["upstream"], []).append(
                    (w["prefix"], w["tenant"], tnt.policy)
                )
        for ups_name, entries in per_up.items():
            minimal = minimal_advertisements(entries)
            if len(minimal) > ups_map[ups_name].max_advertisements:
                errors.append(
                    f"轮 {r} 上游 {ups_name} 通告 {len(minimal)} 条，"
                    f"超过上限 {ups_map[ups_name].max_advertisements}"
                )
            for pfx, tenant, pol in minimal:
                rebuilt_ads.add((r, ups_name, pfx.addr, pfx.length))
    given_ads = {
        (a.round, a.upstream, a.prefix.addr, a.prefix.length) for a in schedule.advertisements
    }
    if rebuilt_ads != given_ads:
        missing = sorted(rebuilt_ads - given_ads)
        extra = sorted(given_ads - rebuilt_ads)
        if missing:
            errors.append(f"通告集缺少 {len(missing)} 条，例如 {missing[:3]}")
        if extra:
            errors.append(f"通告集多出 {len(extra)} 条（含空洞/错误汇总），例如 {extra[:3]}")

    # ---- 双地址轮数统计一致 --------------------------------------------
    if schedule.dual_address_rounds != _count_dual_rounds(problem, windows):
        errors.append("Schedule 记录的双地址轮数与事件重建结果不一致")

    return VerificationReport(ok=not errors, errors=tuple(errors))


def overlap_round_text(start: int, end: int) -> str:
    return f"{start}..{end}" if start != end else str(start)


def _count_dual_rounds(problem: Problem, windows: list[dict]) -> int:
    total = 0
    for tnt in problem.tenants:
        mine = [w for w in windows if w["tenant"] == tnt.name]
        final = next(
            (w for w in mine if not w["temporary"] and (
                tnt.old_prefix is None or w["prefix"] != tnt.old_prefix
            )),
            None,
        )
        if final is None:
            continue
        for w in mine:
            if w is final:
                continue
            # 只有被撤告的旧持有（旧前缀/临时前缀）才产生双地址轮。
            if w["end"] == 10**9 or w["end"] < final["start"]:
                continue
            total += w["end"] - final["start"] + 1
    return total


# ---------------------------------------------------------------------------
# 小地址投影：逐地址验证
# ---------------------------------------------------------------------------
def _to_projection(pfx: Prefix, width: int) -> tuple[int, int]:
    """把真实前缀换算回投影区间 ``[lo, hi)``（端点右移 128-width）。"""
    shift = BITS - width
    lo = pfx.addr >> shift
    hi = pfx.end >> shift
    return lo, hi


def verify_small_projection(
    problem: Problem,
    schedule: Schedule,
    projection_width: int,
) -> VerificationReport:
    """在 ``[0, 2**projection_width)`` 投影上逐地址验证。

    要求所有输入前缀都落在该投影内。逐地址检查：

    * 每个地址每轮至多一个租户持有（无重叠）；
    * 每个被持有地址都出现在该上游的最小通告集中（全程可达）；
    * 固定子前缀的每个地址在每一轮都由同一租户持有（固定点不动）。
    """
    errors: list[str] = []
    size = 1 << projection_width
    all_prefixes = [u.prefix for u in problem.upstreams]
    all_prefixes += [t.old_prefix for t in problem.tenants if t.old_prefix is not None]
    all_prefixes += [t.prefix for t in problem.temp_prefixes]
    for tnt in problem.tenants:
        all_prefixes += list(tnt.fixed)
    block = 1 << (BITS - projection_width)
    for pfx in all_prefixes:
        lo, hi = _to_projection(pfx, projection_width)
        if hi > size or (pfx.addr % block != 0):
            errors.append(f"前缀 {pfx} 超出或不对齐于投影宽度 {projection_width}")
    if errors:
        return VerificationReport(False, tuple(errors))

    windows: list[dict] = []
    # 旧前缀从第 1 轮起持有，先播种窗口。
    for tnt in problem.tenants:
        if tnt.old_prefix is None:
            continue
        wd = next(
            (
                e for e in schedule.events
                if e.kind == "withdraw" and e.tenant == tnt.name and e.prefix == tnt.old_prefix
            ),
            None,
        )
        windows.append(
            {
                "tenant": tnt.name,
                "prefix": tnt.old_prefix,
                "upstream": tnt.old_upstream,
                "start": 1,
                "end": min(wd.round if wd is not None else 10**9, schedule.makespan),
            }
        )
    for ev in schedule.events:
        if ev.kind != "assign":
            continue
        start, end = _alive_window(schedule.events, ev)
        windows.append(
            {
                "tenant": ev.tenant,
                "prefix": ev.prefix,
                "upstream": ev.upstream,
                "start": start,
                "end": min(end, schedule.makespan),
            }
        )

    fixed_owner: dict[int, str] = {}
    for tnt in problem.tenants:
        for fpfx in tnt.fixed:
            lo, hi = _to_projection(fpfx, projection_width)
            for addr in range(lo, hi):
                fixed_owner[addr] = tnt.name

    for r in range(1, schedule.makespan + 1):
        owner: dict[int, str] = {}
        advertised: dict[int, set[str]] = {}
        for w in windows:
            if not (w["start"] <= r <= w["end"]):
                continue
            lo, hi = _to_projection(w["prefix"], projection_width)
            for addr in range(lo, hi):
                if addr in owner and owner[addr] != w["tenant"]:
                    errors.append(
                        f"轮 {r} 地址 {addr} 同时归属 {owner[addr]} 与 {w['tenant']}（重叠）"
                    )
                owner[addr] = w["tenant"]
        for ad in schedule.advertisements:
            if ad.round != r:
                continue
            lo, hi = _to_projection(ad.prefix, projection_width)
            for addr in range(lo, hi):
                advertised.setdefault(addr, set()).add(ad.upstream)
        for addr, tenant in owner.items():
            if addr not in advertised:
                errors.append(f"轮 {r} 地址 {addr}（租户 {tenant}）未被任何通告覆盖（不可达/空洞）")
        for addr in advertised:
            if addr not in owner:
                errors.append(f"轮 {r} 地址 {addr} 被通告但无租户持有（宣告了空洞）")
        for addr, tenant in fixed_owner.items():
            if owner.get(addr) != tenant:
                errors.append(f"轮 {r} 固定地址 {addr} 归属变为 {owner.get(addr)}，固定点被移动")

    return VerificationReport(ok=not errors, errors=tuple(errors))
