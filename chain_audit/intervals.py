"""整数区间约束引擎。

只依赖标准库。约束统一表示为线性整数方程::

    sum(coeff[var] * var) = rhs

变量域为非负整数闭区间。先用 AC-3 式的区间传播做不动点收窄，
需要精确可行/精确界时再在收窄后的小值域上做 DFS 与分支定界；
若预算耗尽仍无定论，返回 ``unknown``，绝不把近似当成结论。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

from .model import GLOBAL_CAP, InputError


@dataclass(frozen=True)
class Equation:
    coeffs: tuple[tuple[str, int], ...]
    rhs: int
    origin: Optional[tuple[str, ...]] = None  # 可查单据编号；结构方程为 None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "coeffs",
            tuple((v, int(c)) for v, c in self.coeffs if c != 0),
        )


@dataclass
class PropagationResult:
    domains: dict[str, tuple[int, int]]
    failed: Optional[str] = None  # 变空的变量名（传播层面定位矛盾）


def initial_domains(var_names, cap: int = GLOBAL_CAP) -> dict[str, tuple[int, int]]:
    return {v: (0, cap) for v in var_names}


def _ceil_div(num: int, den: int) -> int:
    # ceil(num/den)，纯整数实现，den 可为负。
    return -((-num) // den)


def _floor_div(num: int, den: int) -> int:
    # floor(num/den)，Python // 即向下取整，且对负分母同样成立。
    return num // den


def propagate(
    domains: dict[str, tuple[int, int]], equations: list[Equation]
) -> PropagationResult:
    """对全部方程做不动点区间传播，返回收窄后的域（不修改入参）。"""
    cur = dict(domains)
    queue: list[int] = list(range(len(equations)))
    queued = set(queue)
    while queue:
        idx = queue.pop()
        queued.discard(idx)
        eq = equations[idx]
        lo_sum = 0
        hi_sum = 0
        for var, coeff in eq.coeffs:
            lo, hi = cur[var]
            if coeff > 0:
                lo_sum += coeff * lo
                hi_sum += coeff * hi
            else:
                lo_sum += coeff * hi
                hi_sum += coeff * lo
        if lo_sum > eq.rhs or hi_sum < eq.rhs:
            failed = eq.coeffs[0][0] if eq.coeffs else None
            return PropagationResult(cur, failed=failed)
        for var, coeff in eq.coeffs:
            lo, hi = cur[var]
            # rem = rhs - 其它变量之和
            other_lo = lo_sum - (coeff * lo if coeff > 0 else coeff * hi)
            other_hi = hi_sum - (coeff * hi if coeff > 0 else coeff * lo)
            rem_lo = eq.rhs - other_hi
            rem_hi = eq.rhs - other_lo
            if coeff > 0:
                nlo = max(lo, _ceil_div(rem_lo, coeff))
                nhi = min(hi, _floor_div(rem_hi, coeff))
            else:
                nlo = max(lo, _ceil_div(rem_hi, coeff))
                nhi = min(hi, _floor_div(rem_lo, coeff))
            if nlo > nhi:
                return PropagationResult(cur, failed=var)
            if (nlo, nhi) != (lo, hi):
                cur[var] = (nlo, nhi)
                for j, other in enumerate(equations):
                    if j != idx and any(v == var for v, _ in other.coeffs):
                        if j not in queued:
                            queue.append(j)
                            queued.add(j)
    return PropagationResult(cur, failed=None)


def solve(
    domains: dict[str, tuple[int, int]],
    equations: list[Equation],
    node_budget: int = 200_000,
) -> tuple[str, Optional[dict[str, int]]]:
    """可行性求解：返回 ("sat"|"unsat"|"unknown", 见证赋值或 None)。"""
    first = propagate(domains, equations)
    if first.failed is not None:
        return "unsat", None
    used = 0

    def dfs(dom: dict[str, tuple[int, int]]) -> Optional[dict[str, int]]:
        nonlocal used
        used += 1
        if used > node_budget:
            return None
        branched = [
            (v, b) for v, b in dom.items() if b[0] != b[1]
        ]
        if not branched:
            return {v: b[0] for v, b in dom.items()}
        # 值域最小的变量优先分支，尽快发现矛盾。
        var, (lo, hi) = min(branched, key=lambda item: item[1][1] - item[1][0])
        if hi - lo > 32:
            # 大域不枚举点，先试二分，仍不收敛则报 unknown。
            mid = (lo + hi) // 2
            for nlo, nhi in ((lo, mid), (mid + 1, hi)):
                trial = dict(dom)
                trial[var] = (nlo, nhi)
                res = propagate(trial, equations)
                if res.failed is not None:
                    continue
                found = dfs(res.domains)
                if found is not None:
                    return found
                if used > node_budget:
                    return None
            return None
        for value in range(lo, hi + 1):
            trial = dict(dom)
            trial[var] = (value, value)
            res = propagate(trial, equations)
            if res.failed is not None:
                continue
            found = dfs(res.domains)
            if found is not None:
                return found
            if used > node_budget:
                return None
        return None

    witness = dfs(first.domains)
    if witness is not None:
        return "sat", witness
    if used > node_budget:
        return "unknown", None
    return "unsat", None


def extremize(
    domains: dict[str, tuple[int, int]],
    equations: list[Equation],
    target: str,
    maximize: bool,
    node_budget: int = 200_000,
) -> tuple[Optional[int], bool]:
    """求 target 变量的精确最小/最大值；预算耗尽返回 (当前最优, False)。"""
    first = propagate(domains, equations)
    if first.failed is not None:
        return None, True
    used = 0
    best: Optional[int] = None

    def optimistic(dom) -> int:
        return dom[target][1] if maximize else dom[target][0]

    def dfs(dom: dict[str, tuple[int, int]]) -> bool:
        nonlocal used, best
        used += 1
        if used > node_budget:
            return False
        bound = optimistic(dom)
        if best is not None:
            if (maximize and bound <= best) or (not maximize and bound >= best):
                return True
        branched = [
            (v, b) for v, b in dom.items() if b[0] != b[1]
        ]
        if not branched:
            value = dom[target][0]
            if best is None or (maximize and value > best) or (
                not maximize and value < best
            ):
                best = value
            return True
        var, (lo, hi) = min(
            branched,
            key=lambda item: (
                item[1][1] - item[1][0],
                0 if item[0] == target else 1,
            ),
        )

        def branch(nlo: int, nhi: int) -> bool:
            trial = dict(dom)
            trial[var] = (nlo, nhi)
            res = propagate(trial, equations)
            if res.failed is not None:
                return True
            return dfs(res.domains)

        if hi - lo > 32:
            mid = (lo + hi) // 2
            if maximize:
                ok1 = branch(mid + 1, hi)
                ok2 = branch(lo, mid)
            else:
                ok1 = branch(lo, mid)
                ok2 = branch(mid + 1, hi)
            return ok1 and ok2
        completed = True
        order = range(lo, hi + 1)
        if maximize:
            order = range(hi, lo - 1, -1)
        for value in order:
            if not branch(value, value):
                completed = False
        return completed

    completed = dfs(first.domains)
    return best, completed


def ratio_extremize(
    domains: dict[str, tuple[int, int]],
    equations: list[Equation],
    numerator: str,
    denominator: str,
    maximize: bool,
    node_budget: int = 200_000,
) -> tuple[Optional[Fraction], bool]:
    """求 num/den 的精确极值（只统计 den>0 的完整赋值）。

    乐观界用 max(1, den 界) 规避除零，是保守正确的；
    若所有可行赋值 den 都为 0，返回 (None, True)。
    同一份根域/方程集合下，按 (num, den, 方向) 记忆化，避免对
    多商品流里大量结构相同的份额重复做分支定界。
    """
    first = propagate(domains, equations)
    if first.failed is not None:
        return None, True
    cache = _ratio_cache.setdefault(id(equations), {})
    sig = (numerator, denominator, maximize)
    if sig in cache:
        return cache[sig]
    used = 0
    # best 以 (num, den) 整数对保存，比较时交叉相乘，避免热路径上反复
    # 构造 Fraction。
    best_pair: Optional[tuple[int, int]] = None
    saw_positive_den = False

    def optimistic_num_den(dom) -> tuple[int, int]:
        nlo, nhi = dom[numerator]
        dlo, dhi = dom[denominator]
        if maximize:
            return nhi, max(1, dlo)
        return nlo, max(1, dhi)

    def beats(opt_n: int, opt_d: int) -> bool:
        if best_pair is None:
            return False
        bn, bd = best_pair
        lhs = opt_n * bd
        rhs = bn * opt_d
        return lhs <= rhs if maximize else lhs >= rhs

    def dfs(dom) -> bool:
        nonlocal used, best_pair, saw_positive_den
        used += 1
        if used > node_budget:
            return False
        if best_pair is not None:
            on, od = optimistic_num_den(dom)
            if beats(on, od):
                return True
        branched = [(v, b) for v, b in dom.items() if b[0] != b[1]]
        if not branched:
            dval = dom[denominator][0]
            if dval <= 0:
                return True
            saw_positive_den = True
            nval = dom[numerator][0]
            if best_pair is None:
                best_pair = (nval, dval)
            else:
                bn, bd = best_pair
                lhs, rhs = nval * bd, bn * dval
                if (maximize and lhs > rhs) or (not maximize and lhs < rhs):
                    best_pair = (nval, dval)
            return True
        # 值域最窄的变量优先分支，尽快把传播约束推到不动点。
        var, (lo, hi) = min(branched, key=lambda item: item[1][1] - item[1][0])

        def branch(nlo: int, nhi: int) -> bool:
            trial = dict(dom)
            trial[var] = (nlo, nhi)
            res = propagate(trial, equations)
            if res.failed is not None:
                return True
            return dfs(res.domains)

        if hi - lo > 32:
            mid = (lo + hi) // 2
            if maximize:
                ok = branch(mid + 1, hi) and branch(lo, mid)
            else:
                ok = branch(lo, mid) and branch(mid + 1, hi)
            return ok
        completed = True
        order = range(lo, hi + 1)
        if maximize:
            order = range(hi, lo - 1, -1)
        for value in order:
            if not branch(value, value):
                completed = False
        return completed

    completed = dfs(first.domains)
    if not saw_positive_den and best_pair is None:
        result = (None, completed)
    else:
        result = (
            Fraction(best_pair[0], best_pair[1]) if best_pair else None,
            completed,
        )
    cache[sig] = result
    return result


# 结果记忆化缓存：键为方程列表对象 id。审计器在重建模型时会显式清空，
# 避免方程被追加后命中陈旧结果。
_ratio_cache: dict[int, dict] = {}


def clear_caches() -> None:
    _ratio_cache.clear()


__all__ = [
    "Equation",
    "PropagationResult",
    "clear_caches",
    "initial_domains",
    "propagate",
    "solve",
    "extremize",
    "ratio_extremize",
]
