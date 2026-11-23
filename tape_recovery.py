"""归档库恢复调度器(tape archive recovery scheduler)。

场景:归档库恢复多个对象时,同一物理记录可能给两个对象各提供一段切片。
输入至多四盘虚拟磁带、各记录的位置与长度、对象所需的记录切片及消费顺序,
在单一驱动器与暂存上限约束下,求最低总费用恢复计划。

费用模型(均由 ``Costs`` 给定):
  * 换盘:卸载当前盘并装入另一盘(首次装盤同价),固定费用;
  * 前进:带头每前进一个单位长度计费;
  * 回绕:回绕到带首的固定费用(回绕后再前进到目标位置另计前进费);
  * 读记录:按记录长度计费。

规则:
  * 只有一个驱动器,一次装一盘;每次卸载后重装从带首(位置 0)开始;
  * 整条记录读完才可消费其任一切片;暂存占用按记录原长计;
  * 一条记录的所有待用切片耗尽后免费自动释放;也可提前驱逐,以后重读;
  * 下游只按每个对象自身的片序接收,对象之间可以交错;
  * 任何时刻暂存占用不得超过给定上限。

公开 API:
  Costs / Record / ObjectSpec / Problem  —— 输入模型
  Step / Plan                            —— 输出计划
  solve(problem, capacity)               —— 求最低费用计划(不可行返回 None)
  replan(problem, plan, executed_steps, new_capacity)
                                         —— 执行中途更换暂存上限,保留已消费
                                            前缀,按新上限重求余程
  validate_plan(problem, plan, capacity) —— 逐步校验计划(供测试/验收)
  format_plan(problem, plan)             —— 逐动作文本表(带头位置、已消费片、
                                            暂存占用、峰值)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from heapq import heappop, heappush
from itertools import count
from typing import Optional, Sequence, Union

MAX_TAPES = 4


# ---------------------------------------------------------------------------
# 输入模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Costs:
    swap: int              # 换盘费用(卸载当前盘并装入一盘;首次装盤同价)
    forward_per_unit: int  # 带头前进每单位长度费用
    rewind: int            # 回绕到带首的固定费用
    read_per_unit: int     # 读记录每单位长度费用


@dataclass(frozen=True)
class Record:
    rid: str     # 记录 id(全局唯一)
    tape: int    # 所在磁带编号 [0, n_tapes)
    pos: int     # 记录在带上的起始位置
    length: int  # 记录长度(暂存占用按此原长计)


@dataclass(frozen=True)
class ObjectSpec:
    oid: str
    slices: tuple[str, ...]  # 所需记录切片的记录 id,按下游消费顺序


@dataclass(frozen=True)
class Problem:
    n_tapes: int                      # 磁带盘数(至多 MAX_TAPES)
    records: tuple[Record, ...]
    objects: tuple[ObjectSpec, ...]
    costs: Costs


# ---------------------------------------------------------------------------
# 输出模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    action: str                  # 'mount' | 'read' | 'evict' | 'consume' | 'release'
    cost: int                    # 本动作费用(consume/release/evict 为 0)
    head: int                    # 动作后的带头位置
    occupancy: int               # 动作后的暂存占用
    consumed: tuple[int, ...]    # 动作后各对象已消费片数(按 objects 顺序)
    tape: Optional[int] = None   # mount/read 涉及的磁带
    record: Optional[str] = None # read/evict/consume/release 涉及的记录
    obj: Optional[str] = None    # consume 涉及的对象


@dataclass(frozen=True)
class Plan:
    steps: tuple[Step, ...]
    total_cost: int
    peak_occupancy: int


@dataclass(frozen=True)
class _State:
    mounted: Optional[int]       # 当前装在驱动器上的磁带(None = 空驱动器)
    head: int                    # 带头位置
    staged: frozenset            # 暂存中的记录 id 集合
    consumed: tuple[int, ...]    # 各对象已消费片数


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

def _validate_problem(problem: Problem) -> None:
    if not 1 <= problem.n_tapes <= MAX_TAPES:
        raise ValueError(f"n_tapes 必须在 1..{MAX_TAPES},得到 {problem.n_tapes}")
    c = problem.costs
    if min(c.swap, c.forward_per_unit, c.rewind, c.read_per_unit) < 0:
        raise ValueError("费用参数不得为负")
    seen = set()
    for r in problem.records:
        if r.rid in seen:
            raise ValueError(f"记录 id 重复: {r.rid!r}")
        seen.add(r.rid)
        if not 0 <= r.tape < problem.n_tapes:
            raise ValueError(f"记录 {r.rid!r} 的磁带编号越界: {r.tape}")
        if r.pos < 0 or r.length <= 0:
            raise ValueError(f"记录 {r.rid!r} 的 pos/length 非法")
    for o in problem.objects:
        for rid in o.slices:
            if rid not in seen:
                raise ValueError(f"对象 {o.oid!r} 引用了未知记录 {rid!r}")


# ---------------------------------------------------------------------------
# 求解器(在状态空间上做 Dijkstra 最短路径,保证最低总费用)
# ---------------------------------------------------------------------------

class _Solver:
    def __init__(self, problem: Problem, capacity: int):
        self.p = problem
        self.capacity = capacity
        self.rec = {r.rid: r for r in problem.records}
        self.refs: dict[str, list[tuple[int, int]]] = {r.rid: [] for r in problem.records}
        for oi, o in enumerate(problem.objects):
            for k, rid in enumerate(o.slices):
                self.refs[rid].append((oi, k))
        self.by_tape: list[list[Record]] = [[] for _ in range(problem.n_tapes)]
        for r in problem.records:
            self.by_tape[r.tape].append(r)

    def pending(self, rid: str, consumed: tuple[int, ...]) -> bool:
        """该记录是否仍有未消费的待用切片。"""
        return any(consumed[oi] <= k for oi, k in self.refs[rid])

    def occupancy(self, staged: frozenset) -> int:
        return sum(self.rec[rid].length for rid in staged)

    def is_goal(self, s: _State) -> bool:
        return all(c == len(o.slices) for c, o in zip(s.consumed, self.p.objects))

    def close(self, staged, consumed, head):
        """贪婪消费:凡记录已在暂存中的可消费切片立即消费(消费免费且只会
        提前释放占用,不会使任何计划变差)。返回 (staged, consumed, steps)。"""
        staged = set(staged)
        consumed = list(consumed)
        steps: list[Step] = []
        for oi, o in enumerate(self.p.objects):
            while consumed[oi] < len(o.slices) and o.slices[consumed[oi]] in staged:
                rid = o.slices[consumed[oi]]
                consumed[oi] += 1
                steps.append(Step('consume', 0, head, self.occupancy(frozenset(staged)),
                                  tuple(consumed), record=rid, obj=o.oid))
                if not self.pending(rid, consumed):
                    staged.discard(rid)
                    steps.append(Step('release', 0, head, self.occupancy(frozenset(staged)),
                                      tuple(consumed), record=rid))
        return frozenset(staged), tuple(consumed), steps

    def search(self, start: _State) -> Optional[list[Step]]:
        p, c = self.p, self.p.costs
        staged, consumed, pre = self.close(start.staged, start.consumed, start.head)
        start = _State(start.mounted, start.head, staged, consumed)
        dist = {start: 0}
        parent: dict[_State, tuple[_State, list[Step]]] = {}
        tie = count()
        pq: list[tuple[int, int, _State]] = [(0, next(tie), start)]
        goal: Optional[_State] = None
        while pq:
            d, _, s = heappop(pq)
            if d > dist.get(s, math.inf):
                continue
            if self.is_goal(s):
                goal = s
                break
            occ = self.occupancy(s.staged)

            def relax(ns: _State, nd: int, steps: list[Step]) -> None:
                if nd < dist.get(ns, math.inf):
                    dist[ns] = nd
                    parent[ns] = (s, steps)
                    heappush(pq, (nd, next(tie), ns))

            # 1) 提前驱逐(免费,以后可重读)
            for rid in sorted(s.staged):
                if not self.pending(rid, s.consumed):
                    continue
                ns = _State(s.mounted, s.head, s.staged - {rid}, s.consumed)
                step = Step('evict', 0, s.head, occ - self.rec[rid].length,
                            s.consumed, record=rid)
                relax(ns, d, [step])

            # 2) 读当前盘上的一条待用记录(必要时先前进或回绕)
            if s.mounted is not None:
                for r in self.by_tape[s.mounted]:
                    if r.rid in s.staged or not self.pending(r.rid, s.consumed):
                        continue
                    nocc = occ + r.length
                    if nocc > self.capacity:
                        continue  # 暂存放不下,须先驱逐
                    if r.pos >= s.head:
                        seek = (r.pos - s.head) * c.forward_per_unit
                    else:
                        seek = c.rewind + r.pos * c.forward_per_unit
                    nhead = r.pos + r.length
                    rcost = seek + r.length * c.read_per_unit
                    read_step = Step('read', rcost, nhead, nocc, s.consumed,
                                     tape=s.mounted, record=r.rid)
                    st2, cons2, extra = self.close(s.staged | {r.rid}, s.consumed, nhead)
                    relax(_State(s.mounted, nhead, st2, cons2), d + rcost,
                          [read_step] + extra)

            # 3) 换盘(含重装同一盘以回到带首的情形);重装后带头在 0
            for t in range(p.n_tapes):
                if t == s.mounted and s.head == 0:
                    continue
                step = Step('mount', c.swap, 0, occ, s.consumed, tape=t)
                relax(_State(t, 0, s.staged, s.consumed), d + c.swap, [step])

        if goal is None:
            return None
        steps = list(pre)
        edges: list[list[Step]] = []
        s = goal
        while s in parent:
            prev, esteps = parent[s]
            edges.append(esteps)
            s = prev
        for esteps in reversed(edges):
            steps.extend(esteps)
        return steps


def _make_plan(steps: Sequence[Step]) -> Plan:
    steps = tuple(steps)
    return Plan(steps=steps,
                total_cost=sum(s.cost for s in steps),
                peak_occupancy=max((s.occupancy for s in steps), default=0))


def _initial_state(problem: Problem) -> _State:
    return _State(None, 0, frozenset(), tuple(0 for _ in problem.objects))


def solve(problem: Problem, capacity: int) -> Optional[Plan]:
    """求满足暂存上限 ``capacity`` 的最低总费用恢复计划;不可行返回 None。"""
    _validate_problem(problem)
    if capacity < 0:
        raise ValueError("capacity 不得为负")
    steps = _Solver(problem, capacity).search(_initial_state(problem))
    return None if steps is None else _make_plan(steps)


# ---------------------------------------------------------------------------
# 中途更换暂存上限:保留已消费前缀,按新上限重求余程
# ---------------------------------------------------------------------------

def _replay(problem: Problem, steps: Sequence[Step]) -> _State:
    obj_index = {o.oid: i for i, o in enumerate(problem.objects)}
    mounted: Optional[int] = None
    head = 0
    staged: set[str] = set()
    consumed = [0] * len(problem.objects)
    for st in steps:
        if st.action == 'mount':
            mounted, head = st.tape, 0
        elif st.action == 'read':
            staged.add(st.record)
            head = st.head
        elif st.action in ('evict', 'release'):
            staged.discard(st.record)
        elif st.action == 'consume':
            consumed[obj_index[st.obj]] += 1
        else:
            raise ValueError(f"未知动作: {st.action!r}")
    return _State(mounted, head, frozenset(staged), tuple(consumed))


def replan(problem: Problem, plan: Plan, executed_steps: int,
           new_capacity: int) -> Optional[Plan]:
    """已执行 ``plan`` 的前 ``executed_steps`` 个动作后,把暂存上限改为
    ``new_capacity`` 重新求余程。已消费前缀原样保留,返回拼接后的完整新计划;
    余程不可行时返回 None。"""
    _validate_problem(problem)
    if new_capacity < 0:
        raise ValueError("new_capacity 不得为负")
    if not 0 <= executed_steps <= len(plan.steps):
        raise ValueError("executed_steps 越界")
    prefix = list(plan.steps[:executed_steps])
    state = _replay(problem, prefix)
    suffix = _Solver(problem, new_capacity).search(state)
    if suffix is None:
        return None
    return _make_plan(prefix + suffix)


# ---------------------------------------------------------------------------
# 计划校验(测试与验收用):逐步重放并核对费用、带头、占用、消费顺序
# ---------------------------------------------------------------------------

def validate_plan(problem: Problem, plan: Union[Plan, Sequence[Step]],
                  capacity: int, start: Optional[_State] = None,
                  require_complete: bool = True) -> None:
    """校验计划合法且账目一致;任何违例抛出 ValueError。

    ``require_complete`` 为 False 时允许校验只执行了一半的前缀
    (不要求结束时所有切片都已消费)。"""
    _validate_problem(problem)
    steps = plan.steps if isinstance(plan, Plan) else tuple(plan)
    rec = {r.rid: r for r in problem.records}
    refs: dict[str, list[tuple[int, int]]] = {r.rid: [] for r in problem.records}
    for oi, o in enumerate(problem.objects):
        for k, rid in enumerate(o.slices):
            refs[rid].append((oi, k))
    obj_index = {o.oid: i for i, o in enumerate(problem.objects)}

    state = start if start is not None else _initial_state(problem)
    mounted, head = state.mounted, state.head
    staged = set(state.staged)
    consumed = list(state.consumed)
    total = 0
    peak = sum(rec[r].length for r in staged)

    def expect(cond: bool, msg: str) -> None:
        if not cond:
            raise ValueError(f"计划校验失败(第 {i} 步 {st.action}): {msg}")

    for i, st in enumerate(steps):
        occ = sum(rec[r].length for r in staged)
        if st.action == 'mount':
            expect(st.tape is not None and 0 <= st.tape < problem.n_tapes, "磁带编号非法")
            expect(st.cost == problem.costs.swap, "换盘费用不符")
            mounted, head = st.tape, 0
        elif st.action == 'read':
            expect(mounted is not None, "驱动器为空无法读带")
            r = rec[st.record]
            expect(r.tape == mounted, "记录不在当前磁带上")
            expect(r.rid not in staged, "重复读取未驱逐的记录")
            if r.pos >= head:
                seek = (r.pos - head) * problem.costs.forward_per_unit
            else:
                seek = problem.costs.rewind + r.pos * problem.costs.forward_per_unit
            expect(st.cost == seek + r.length * problem.costs.read_per_unit,
                   "读记录费用(含前进/回绕)不符")
            head = r.pos + r.length
            staged.add(r.rid)
            occ += r.length
            expect(occ <= capacity, f"暂存占用 {occ} 超过上限 {capacity}")
        elif st.action == 'evict':
            expect(st.record in staged, "驱逐的记录不在暂存中")
            expect(st.cost == 0, "驱逐应免费")
            staged.discard(st.record)
            occ -= rec[st.record].length
        elif st.action == 'consume':
            oi = obj_index[st.obj]
            o = problem.objects[oi]
            expect(consumed[oi] < len(o.slices), "对象切片已全部消费")
            expect(o.slices[consumed[oi]] == st.record, "违反对象内片序")
            expect(st.record in staged, "记录未读完(未入暂存)就消费切片")
            expect(st.cost == 0, "消费应免费")
            consumed[oi] += 1
        elif st.action == 'release':
            expect(st.record in staged, "释放的记录不在暂存中")
            expect(all(consumed[oi] > k for oi, k in refs[st.record]),
                   "记录尚有待用切片未耗尽就释放")
            expect(st.cost == 0, "释放应免费")
            staged.discard(st.record)
            occ -= rec[st.record].length
        else:
            raise ValueError(f"计划校验失败(第 {i} 步): 未知动作 {st.action!r}")
        expect(st.head == head, "带头位置账目不符")
        expect(st.occupancy == sum(rec[r].length for r in staged), "暂存占用账目不符")
        expect(st.consumed == tuple(consumed), "已消费片账目不符")
        total += st.cost
        peak = max(peak, st.occupancy)

    if isinstance(plan, Plan):
        if plan.total_cost != total:
            raise ValueError("total_cost 账目不符")
        if plan.peak_occupancy != peak:
            raise ValueError("peak_occupancy 账目不符")
    if require_complete and any(c != len(o.slices)
                                for c, o in zip(consumed, problem.objects)):
        raise ValueError("计划结束时仍有切片未消费")


# ---------------------------------------------------------------------------
# 逐动作文本输出:带头位置、已消费片、暂存占用与峰值
# ---------------------------------------------------------------------------

def format_plan(problem: Problem, plan: Plan) -> str:
    obj_ids = [o.oid for o in problem.objects]
    lines = [f"{'#':>3} {'action':<8} {'tape':>4} {'record':<7} {'obj':<5} "
             f"{'cost':>6} {'head':>6} {'occ':>5}  consumed{tuple(obj_ids)}"]
    for i, st in enumerate(plan.steps):
        lines.append(
            f"{i:>3} {st.action:<8} "
            f"{('-' if st.tape is None else st.tape)!s:>4} "
            f"{(st.record or '-'):<7} {(st.obj or '-'):<5} "
            f"{st.cost:>6} {st.head:>6} {st.occupancy:>5}  {st.consumed}")
    lines.append(f"total_cost={plan.total_cost} peak_occupancy={plan.peak_occupancy}")
    return "\n".join(lines)
