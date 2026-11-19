"""小计数表发布方案求解器。

问题模型
========
给定一张至多 4x4 的非负整数计数表、每格不超过 9 的取值上界、必须隐藏
的敏感格敏感格集合、允许额外隐藏的候选格及其隐藏成本。行合计与列合计始终
公开。观察者知道：行列合计、所有可见格的真实值、每格取值上界，以及
"隐藏格取值为区间内的非负整数" 这一结构信息。

模块职责
========
1. 对每个敏感格，求仅做必须隐藏时观察者能推出的最小值/最大值；
2. 寻找总成本最低的额外隐藏集合，使每个敏感格都至少存在两种可行
   取值（最小值 < 最大值）；同成本时按 ``(行, 列)`` 坐标序列的字典序
   取最小方案；
3. 若连全部候选都隐藏仍无解，指出哪些敏感格仍可被唯一推出以及推出值。

可行性判定委托给 :mod:`publication.flow` 的整数最大流运输模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .flow import feasible as _feasible

Pair = Tuple[int, int]


# --------------------------------------------------------------------------- #
# 输入校验与规整
# --------------------------------------------------------------------------- #

def _is_real_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _check_pair(p: object, rows: int, cols: int, name: str) -> Pair:
    if not isinstance(p, (tuple, list)) or len(p) != 2:
        raise ValueError(f"{name} 必须是 (行, 列) 二元组: {p!r}")
    r, c = p
    if not (_is_real_int(r) and _is_real_int(c)):
        raise ValueError(f"{name} 坐标必须是整数: {p!r}")
    if not (0 <= r < rows and 0 <= c < cols):
        raise ValueError(f"{name} 坐标越界: {p!r}")
    return (r, c)


def _validate_inputs(
    table: Sequence[Sequence[int]],
    upper_bounds: object,
    sensitive: Sequence[object],
    candidates: Sequence[object],
    costs: Mapping[object, int],
) -> Tuple[
    List[List[int]],
    List[List[int]],
    Tuple[Pair, ...],
    Tuple[Pair, ...],
    Dict[Pair, int],
]:
    if not isinstance(table, (tuple, list)) or len(table) == 0:
        raise ValueError("table 必须是非空二维序列")
    rows = len(table)
    if rows > 4:
        raise ValueError("表行数不超过 4")
    cols = None
    norm_table: List[List[int]] = []
    for row in table:
        if not isinstance(row, (tuple, list)):
            raise ValueError("table 必须是二维序列")
        if cols is None:
            cols = len(row)
            if cols == 0 or cols > 4:
                raise ValueError("表列数必须在 1..4 之间")
        elif len(row) != cols:
            raise ValueError("table 各行长度不一致")
        norm_row = []
        for v in row:
            if not _is_real_int(v) or v < 0:
                raise ValueError(f"表元素必须是非负整数: {v!r}")
            norm_row.append(v)
        norm_table.append(norm_row)
    assert cols is not None

    # 上界：单个整数（广播）或与表同形的二维序列。
    if _is_real_int(upper_bounds):
        if not (0 <= int(upper_bounds) <= 9):
            raise ValueError("统一上界必须在 0..9 之间")
        norm_upper = [[int(upper_bounds)] * cols for _ in range(rows)]
    elif isinstance(upper_bounds, (tuple, list)):
        if len(upper_bounds) != rows:
            raise ValueError("upper_bounds 行数与表不一致")
        norm_upper = []
        for row in upper_bounds:
            if not isinstance(row, (tuple, list)) or len(row) != cols:
                raise ValueError("upper_bounds 必须与表同形")
            buf = []
            for u in row:
                if not _is_real_int(u) or not (0 <= u <= 9):
                    raise ValueError(f"上界必须是 0..9 的整数: {u!r}")
                buf.append(int(u))
            norm_upper.append(buf)
    else:
        raise ValueError("upper_bounds 必须是整数或二维序列")

    for r in range(rows):
        for c in range(cols):
            if norm_table[r][c] > norm_upper[r][c]:
                raise ValueError(
                    f"表值 ({r},{c})={norm_table[r][c]} 超过上界 "
                    f"{norm_upper[r][c]}"
                )

    if not isinstance(sensitive, (tuple, list)) or len(sensitive) == 0:
        raise ValueError("sensitive 必须是非空坐标序列")
    sens_list: List[Pair] = []
    seen = set()
    for p in sensitive:
        pair = _check_pair(p, rows, cols, "sensitive")
        if pair in seen:
            raise ValueError(f"sensitive 中存在重复坐标: {pair}")
        seen.add(pair)
        sens_list.append(pair)

    if not isinstance(candidates, (tuple, list)):
        raise ValueError("candidates 必须是坐标序列")
    cand_list: List[Pair] = []
    seen_c = set()
    for p in candidates:
        pair = _check_pair(p, rows, cols, "candidates")
        if pair in seen:
            raise ValueError(f"候选格 {pair} 已是必须隐藏的敏感格")
        if pair in seen_c:
            raise ValueError(f"candidates 中存在重复坐标: {pair}")
        seen_c.add(pair)
        cand_list.append(pair)

    if not isinstance(costs, Mapping):
        raise ValueError("costs 必须是坐标到成本的映射")
    norm_costs: Dict[Pair, int] = {}
    for key, val in costs.items():
        pair = _check_pair(key, rows, cols, "costs 键")
        if pair not in seen_c:
            raise ValueError(f"成本键 {pair} 不在 candidates 中")
        if not _is_real_int(val) or val < 0:
            raise ValueError(f"隐藏成本必须是非负整数: {val!r}")
        norm_costs[pair] = int(val)
    missing = [p for p in cand_list if p not in norm_costs]
    if missing:
        raise ValueError(f"以下候选格缺少成本: {missing}")

    # 统一按行优先有序，保证"坐标序列字典序"规则不依赖输入排列。
    return (
        norm_table,
        norm_upper,
        tuple(sorted(sens_list)),
        tuple(sorted(cand_list)),
        norm_costs,
    )


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ReleasePlan:
    """发布方案结果。

    属性
    ----
    initial_ranges:
        仅隐藏敏感格时，每个敏感格可推出的 ``(最小值, 最大值)``。
    initial_forced:
        初始状态下被唯一推出的敏感格及其值。
    feasible:
        是否存在让全部敏感格都至少有两种取值的补隐藏方案。
    extra_hidden:
        最优方案额外隐藏的坐标（行优先有序）；无解时为空元组。
    cost:
        最优方案总成本；无解时为 ``None``。
    final_ranges:
        最优方案下各敏感格的取值区间；无解时为空映射。
    full_hide_ranges:
        全部候选都隐藏时各敏感格的取值区间（诊断信息）。
    still_forced:
        无解时仍可被唯一推出的敏感格及其推出值。
    row_sums / col_sums:
        始终保留的行、列合计。
    """

    initial_ranges: Mapping[Pair, Tuple[int, int]]
    initial_forced: Mapping[Pair, int]
    feasible: bool
    extra_hidden: Tuple[Pair, ...]
    cost: Optional[int]
    final_ranges: Mapping[Pair, Tuple[int, int]]
    full_hide_ranges: Mapping[Pair, Tuple[int, int]]
    still_forced: Mapping[Pair, int]
    row_sums: Tuple[int, ...]
    col_sums: Tuple[int, ...]

    def to_dict(self) -> dict:
        """转成可 JSON 序列化的普通字典（坐标转为字符串键）。"""
        def ranges(d: Mapping[Pair, Tuple[int, int]]) -> dict:
            return {f"{r},{c}": list(v) for (r, c), v in sorted(d.items())}

        def forced(d: Mapping[Pair, int]) -> dict:
            return {f"{r},{c}": v for (r, c), v in sorted(d.items())}

        return {
            "feasible": self.feasible,
            "initial_ranges": ranges(self.initial_ranges),
            "initial_forced": forced(self.initial_forced),
            "extra_hidden": [list(p) for p in self.extra_hidden],
            "cost": self.cost,
            "final_ranges": ranges(self.final_ranges),
            "full_hide_ranges": ranges(self.full_hide_ranges),
            "still_forced": forced(self.still_forced),
            "row_sums": list(self.row_sums),
            "col_sums": list(self.col_sums),
        }


# --------------------------------------------------------------------------- #
# 求解器
# --------------------------------------------------------------------------- #

class _Solver:
    def __init__(
        self,
        table: List[List[int]],
        upper: List[List[int]],
        sensitive: Tuple[Pair, ...],
        candidates: Tuple[Pair, ...],
        costs: Dict[Pair, int],
    ) -> None:
        self.table = table
        self.upper = upper
        self.rows = len(table)
        self.cols = len(table[0])
        self.sensitive = sensitive
        self.candidates = candidates
        self.costs = costs
        self.row_sums = tuple(sum(row) for row in table)
        self.col_sums = tuple(
            sum(table[r][c] for r in range(self.rows))
            for c in range(self.cols)
        )
        self._feas_cache: Dict[frozenset, bool] = {}
        self._range_cache: Dict[Tuple[frozenset, Pair], Tuple[int, int]] = {}

    # -- 基础查询 ---------------------------------------------------------- #

    def _visible_fixed(self, hidden: frozenset) -> List[Tuple[int, int, int]]:
        return [
            (r, c, self.table[r][c])
            for r in range(self.rows)
            for c in range(self.cols)
            if (r, c) not in hidden
        ]

    def _base_feasible(self, hidden: frozenset) -> bool:
        if hidden not in self._feas_cache:
            self._feas_cache[hidden] = _feasible(
                self.rows,
                self.cols,
                self.row_sums,
                self.col_sums,
                self.upper,
                fixed=self._visible_fixed(hidden),
            )
        return self._feas_cache[hidden]

    def _cell_range(self, hidden: frozenset, cell: Pair) -> Tuple[int, int]:
        """给定隐藏集合，求 cell 在所有可行表中的最小/最大值。"""
        key = (hidden, cell)
        cached = self._range_cache.get(key)
        if cached is not None:
            return cached

        r, c = cell
        fixed = self._visible_fixed(hidden)
        cap = self.upper[r][c]

        def can_lb(k: int) -> bool:
            # 存在一张可行表使该格取值 >= k（用于求最大值）。
            return _feasible(
                self.rows,
                self.cols,
                self.row_sums,
                self.col_sums,
                self.upper,
                fixed=fixed,
                clipped_lower=((r, c, k),) if k > 0 else None,
            )

        def can_ub(k: int) -> bool:
            # 存在一张可行表使该格取值 <= k（用于求最小值）。
            return _feasible(
                self.rows,
                self.cols,
                self.row_sums,
                self.col_sums,
                self.upper,
                fixed=fixed,
                clipped_upper=((r, c, k),),
            )

        if not can_lb(0):  # 理论上不会发生：真实表本身就是可行解
            raise RuntimeError("内部错误：隐藏集合无可行完成")

        # 最小值：can_ub(k) 随 k 增大由假变真，取第一个可行 k。
        lo, hi = 0, cap
        while lo < hi:
            mid = (lo + hi) // 2
            if can_ub(mid):
                hi = mid
            else:
                lo = mid + 1
        low = lo

        # 最大值：can_lb(k) 随 k 增大由真变假，取最后一个可行 k。
        lo, hi = 0, cap
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if can_lb(mid):
                lo = mid
            else:
                hi = mid - 1
        high = lo

        result = (low, high)
        self._range_cache[key] = result
        return result

    def _all_ambiguous(self, extra: frozenset) -> bool:
        hidden = frozenset(self.sensitive) | extra
        if not self._base_feasible(hidden):
            return False
        for cell in self.sensitive:
            low, high = self._cell_range(hidden, cell)
            if low >= high:  # 仅一种可行取值 => 仍可被推出
                return False
        return True

    # -- 子集搜索 ---------------------------------------------------------- #

    def search(self) -> Tuple[
        Optional[Tuple[Pair, ...]],
        Optional[int],
        Dict[Pair, Tuple[int, int]],
        Dict[Pair, Tuple[int, int]],
    ]:
        n = len(self.candidates)
        base = frozenset(self.sensitive)
        full_extra = frozenset(self.candidates)

        full_hidden = base | full_extra
        full_ranges = {
            cell: self._cell_range(full_hidden, cell)
            for cell in self.sensitive
        }

        best: Optional[Tuple[int, Tuple[Pair, ...]]] = None
        chosen: List[Pair] = []

        def dfs(i: int, cost: int, extra: frozenset) -> None:
            nonlocal best
            if best is not None and cost > best[0]:
                return
            # 单调性：当前集合已满足时，其超集只会更贵，无需继续。
            if self._all_ambiguous(extra):
                key = (cost, tuple(chosen))
                if best is None or key < best:
                    best = key
                return
            if i == n:
                return
            cell = self.candidates[i]
            # 先不选（倾向坐标更少/字典序更小的集合），再选。
            dfs(i + 1, cost, extra)
            chosen.append(cell)
            dfs(i + 1, cost + self.costs[cell], extra | frozenset((cell,)))
            chosen.pop()

        dfs(0, 0, frozenset())

        if best is None:
            return None, None, {}, full_ranges

        best_cost, best_tuple = best
        hidden = base | frozenset(best_tuple)
        final_ranges = {
            cell: self._cell_range(hidden, cell) for cell in self.sensitive
        }
        return best_tuple, best_cost, final_ranges, full_ranges


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #

def plan_release(
    table: Sequence[Sequence[int]],
    upper_bounds: object,
    sensitive: Sequence[object],
    candidates: Sequence[object],
    costs: Mapping[object, int],
) -> ReleasePlan:
    """计算发布方案。

    参数
    ----
    table:
        至多 4x4 的非负整数表（真实计数）。
    upper_bounds:
        每格取值上界（0..9）；可传单个整数广播，或与表同形的二维序列。
    sensitive:
        必须隐藏的敏感格 ``(行, 列)`` 序列，坐标从 0 开始。
    candidates:
        允许额外隐藏的格子坐标序列（不得与 sensitive 重叠）。
    costs:
        每个候选格到非负整数隐藏成本的映射，键为 ``(行, 列)``。

    返回
    ----
    :class:`ReleasePlan`，包含初始推断区间、最优补隐藏方案（成本与字典序
    双重最优），或无解时仍可推出的敏感格值。
    """

    norm_table, norm_upper, sens, cands, norm_costs = _validate_inputs(
        table, upper_bounds, sensitive, candidates, costs
    )
    solver = _Solver(norm_table, norm_upper, sens, cands, norm_costs)

    base_hidden = frozenset(sens)
    initial_ranges = {
        cell: solver._cell_range(base_hidden, cell) for cell in sens
    }
    initial_forced = {
        cell: low
        for cell, (low, high) in initial_ranges.items()
        if low == high
    }

    extra, best_cost, final_ranges, full_ranges = solver.search()

    if extra is None:
        still_forced = {
            cell: low
            for cell, (low, high) in full_ranges.items()
            if low == high
        }
        return ReleasePlan(
            initial_ranges=initial_ranges,
            initial_forced=initial_forced,
            feasible=False,
            extra_hidden=(),
            cost=None,
            final_ranges={},
            full_hide_ranges=full_ranges,
            still_forced=still_forced,
            row_sums=solver.row_sums,
            col_sums=solver.col_sums,
        )

    return ReleasePlan(
        initial_ranges=initial_ranges,
        initial_forced=initial_forced,
        feasible=True,
        extra_hidden=extra,
        cost=best_cost,
        final_ranges=final_ranges,
        full_hide_ranges=full_ranges,
        still_forced={},
        row_sums=solver.row_sums,
        col_sums=solver.col_sums,
    )
