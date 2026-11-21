"""流水段 / 微批整步最短时间规划。

约束建模
========
* 最多 3 个流水段，最多 4 个微批；每个微批先依次前向 F0..F(S-1)，
  再倒序反向 B(S-1)..B0。
* 每段一个设备，同一时刻只能跑一项操作；操作时长为固定整数。
* 跨段移交瞬时：同一时刻允许先完成再开工（半开区间 [start, end)）。
* 微批 m 在段 s 的前向一开始即占用一个激活槽，直到该微批在段 s 的
  反向结束才释放（占用区间 [F.start, B.end)；B 在时刻 t 结束即释放，
  新前向可在同一时刻 t 开始）。

状态不变量
==========
* ``f_done[m]``：微批 m 已完成前向的段数。初始 0；段 s 前向完成后变为
  ``s+1``；全部前向完成时为 ``S``。段 s 前向可开工 iff ``f_done[m] == s``。
* ``b_next[m]``：微批 m 下一个待做反向的段号。初始 ``S-1``；段 s 反向
  完成后变为 ``s-1``；全部反向完成时为 ``-1``。段 s 反向可开工 iff
  ``b_next[m] == s`` 且该段前向已完成（``f_done[m] > s``）。

算法
====
离散时间事件驱动 DFS + 记忆化 + 分支限界。每次只在设备刚空闲的事件时刻
决策：对每个当前空闲段选择一个合法操作，或在所有空闲段均无工可开时整体
空转到下一事件时刻。该枚举覆盖所有可行调度，因此返回全局最短 makespan。
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_STAGES = 3
MAX_MICROBATCHES = 4
_INF = 10**18


@dataclass(frozen=True)
class StageConfig:
    """单个流水段的配置。"""

    forward_duration: int
    backward_duration: int
    activation_capacity: int


@dataclass(frozen=True)
class Operation:
    """单个微批在单段上的一次操作（半开区间 [start, end)）。"""

    stage: int
    microbatch: int
    kind: str  # "forward" 或 "backward"
    start: int
    end: int


@dataclass(frozen=True)
class Schedule:
    makespan: int
    operations: tuple[Operation, ...]
    activation_peak: tuple[int, ...]


def _validate(stages: list[StageConfig], num_microbatches: int) -> None:
    if not isinstance(num_microbatches, int) or isinstance(num_microbatches, bool):
        raise TypeError("num_microbatches 必须为整数")
    if not 1 <= num_microbatches <= MAX_MICROBATCHES:
        raise ValueError(
            f"num_microbatches 需在 1..{MAX_MICROBATCHES} 之间，收到 {num_microbatches}"
        )
    if not isinstance(stages, (list, tuple)) or not stages:
        raise ValueError("stages 必须为非空序列")
    if len(stages) > MAX_STAGES:
        raise ValueError(f"流水段数量最多 {MAX_STAGES}，收到 {len(stages)}")
    for i, st in enumerate(stages):
        if not isinstance(st, StageConfig):
            raise TypeError(f"stages[{i}] 必须为 StageConfig")
        for name in ("forward_duration", "backward_duration", "activation_capacity"):
            v = getattr(st, name)
            if not isinstance(v, int) or isinstance(v, bool):
                raise TypeError(f"stages[{i}].{name} 必须为整数")
            if v <= 0:
                raise ValueError(f"stages[{i}].{name} 必须为正整数，收到 {v}")


def plan(stages: list[StageConfig], num_microbatches: int) -> Schedule:
    """求最早完成时刻的整步调度。"""
    _validate(stages, num_microbatches)
    S = len(stages)
    M = num_microbatches

    def purge(active_s: tuple, t: int) -> tuple:
        """移除在时刻 t 已经释放（反向结束时刻 <= t）的激活槽记录。"""
        return tuple(a for a in active_s if a[1] is None or a[1] > t)

    def eligibles(
        s: int,
        t: int,
        f_done: tuple,
        b_next: tuple,
        active_s: tuple,
        intervals: dict[tuple, tuple[int, int]],
    ) -> list[tuple[str, int]]:
        """段 s 在时刻 t 可启动的合法操作（调用方保证设备在 t 空闲）。

        除计数不变量外还强制时间偏序：
        * 段 s 前向开始 >= 段 s-1 前向结束（瞬时移交，等号合法）；
        * 段 s 反向开始 >= 段 s+1 反向结束（倒序，等号合法）。
        前向排在反向之前，贪心与分支枚举都依赖这一顺序。
        """
        active_s = purge(active_s, t)
        out: list[tuple[str, int]] = []
        # 前向：段 0..s-1 已前向（f_done == s）、本段未前向、激活槽未满。
        if len(active_s) < stages[s].activation_capacity:
            for m in range(M):
                if f_done[m] != s:
                    continue
                if s > 0:
                    prev_end = intervals[("forward", s - 1, m)][1]
                    if t < prev_end:
                        continue
                out.append(("forward", m))
        # 反向：本段前向已完成（f_done > s），且下一个待做反向正是本段；
        # 非末段还需后一段反向已结束。
        for m in range(M):
            if f_done[m] > s and b_next[m] == s:
                if s + 1 < S:
                    next_b_end = intervals[("backward", s + 1, m)][1]
                    if t < next_b_end:
                        continue
                out.append(("backward", m))
        return out

    def simulate_greedy() -> tuple[int, dict[tuple, tuple[int, int]]]:
        """确定性贪心可行解（分支限界上界）：每个空闲段取 eligibles 首项，
        无工可开时整体空转到最近的设备空闲事件。"""
        free = [0] * S
        f_done = tuple(0 for _ in range(M))
        b_next = tuple(S - 1 for _ in range(M))
        active = tuple(() for _ in range(S))
        iv: dict[tuple, tuple[int, int]] = {}
        total = 2 * S * M
        done = 0
        while done < total:
            t = min(free)
            free_stages = [s for s in range(S) if free[s] == t]
            per_stage = {
                s: eligibles(s, t, f_done, b_next, active[s], iv)
                for s in free_stages
            }
            if all(len(e) == 0 for e in per_stage.values()):
                busy = [free[s] for s in range(S) if free[s] > t]
                if not busy:  # 剩余工作存在却全员无工可开 -> 理论不可达
                    raise RuntimeError("贪心仿真进入死锁状态")
                t2 = min(busy)
                free = [t2 if x == t else x for x in free]
                continue
            fd = list(f_done)
            bn = list(b_next)
            ac = list(active)
            for s in free_stages:
                es = per_stage[s]
                if not es:
                    continue
                kind, m = es[0]
                if kind == "forward":
                    end = t + stages[s].forward_duration
                    iv[("forward", s, m)] = (t, end)
                    ac[s] = purge(ac[s], t) + ((m, None),)
                    fd[m] = s + 1
                else:
                    end = t + stages[s].backward_duration
                    iv[("backward", s, m)] = (t, end)
                    ac[s] = tuple(
                        (mm, end) if mm == m and rel is None else (mm, rel)
                        for (mm, rel) in purge(ac[s], t)
                    )
                    bn[m] = s - 1
                free[s] = end
                done += 1
            f_done = tuple(fd)
            b_next = tuple(bn)
            active = tuple(ac)
        return max(free), iv

    best_makespan, greedy_iv = simulate_greedy()

    def lower_bound(t: int, free: tuple, f_done: tuple, b_next: tuple) -> int:
        """设备工作量下界 + 单微批关键链下界，取最大。"""
        lb = t
        for s in range(S):
            remaining = 0
            for m in range(M):
                if f_done[m] <= s:  # 段 s 前向尚未完成
                    remaining += stages[s].forward_duration
                if b_next[m] >= s:  # 段 s 反向尚未完成
                    remaining += stages[s].backward_duration
            lb = max(lb, free[s] + remaining)
        for m in range(M):
            chain = 0
            for s in range(f_done[m], S):
                chain += stages[s].forward_duration
            for s in range(S - 1, -1, -1):
                if b_next[m] >= s:
                    chain += stages[s].backward_duration
            # 该微批下一步在段 f_done[m] 开工，该设备当前空闲时刻为 free[...]。
            ready = free[f_done[m]] if f_done[m] < S else t
            lb = max(lb, ready + chain)
        return lb

    memo: dict[tuple, int] = {}
    best_intervals: dict[tuple, tuple[int, int]] = dict(greedy_iv)

    def dfs(
        t: int,
        free: tuple,
        f_done: tuple,
        b_next: tuple,
        active: tuple,
        intervals: dict[tuple, tuple[int, int]],
    ) -> int:
        nonlocal best_makespan, best_intervals

        if all(f_done[m] == S and b_next[m] == -1 for m in range(M)):
            ms = max(free)
            if ms < best_makespan:
                best_makespan = ms
                best_intervals = dict(intervals)
            return ms

        if lower_bound(t, free, f_done, b_next) >= best_makespan:
            return _INF

        key = (free, f_done, b_next, active)
        cached = memo.get(key)
        if cached is not None:
            return cached

        free_stages = [s for s in range(S) if free[s] == t]
        per_stage = {
            s: eligibles(s, t, f_done, b_next, active[s], intervals)
            for s in free_stages
        }
        busy_times = [free[s] for s in range(S) if free[s] > t]

        # 全部空闲段都无工可开：整体空转到下一设备事件（或判定死锁）。
        if all(len(e) == 0 for e in per_stage.values()):
            if not busy_times:
                memo[key] = _INF  # 真死锁，与上界无关，可缓存
                return _INF
            t2 = min(busy_times)
            result = dfs(
                t2,
                tuple(t2 if x == t else x for x in free),
                f_done,
                b_next,
                active,
                intervals,
            )
            if result < _INF:
                # INF 可能只是被当前 best_makespan 剪枝，不缓存
                memo[key] = result
            return result

        # 有活段：在它们的合法操作上做笛卡尔积；无活段本拍不动，
        # 其 free 时刻在分支内推进到“该分支下一事件时刻”。
        # 此外每个有活段也允许主动空转（选项 None），以覆盖先做别的
        # 段、稍后再开工的交错最优解。
        options: list[dict] = [{}]
        for s in free_stages:
            es = per_stage[s]
            if not es:
                continue  # 无活段：分支内统一空转，不参与笛卡尔积
            choices_here = list(es) + [None]
            new_options = []
            for partial in options:
                for op in choices_here:
                    p = dict(partial)
                    p[s] = op
                    new_options.append(p)
            options = new_options

        results = []
        for choices in options:
            nfree = list(free)
            fd = list(f_done)
            bn = list(b_next)
            ac = list(active)
            iv = dict(intervals)
            # 先落地选定的操作，得到本分支的最早结束事件
            branch_ends = list(busy_times)
            active_choice = False
            for s, op in choices.items():
                if op is None:
                    continue
                active_choice = True
                kind, m = op
                if kind == "forward":
                    end = t + stages[s].forward_duration
                    iv[("forward", s, m)] = (t, end)
                    ac[s] = purge(ac[s], t) + ((m, None),)
                    fd[m] = s + 1
                else:
                    end = t + stages[s].backward_duration
                    iv[("backward", s, m)] = (t, end)
                    ac[s] = tuple(
                        (mm, end) if mm == m and rel is None else (mm, rel)
                        for (mm, rel) in purge(ac[s], t)
                    )
                    bn[m] = s - 1
                nfree[s] = end
                branch_ends.append(end)
            if not active_choice:
                # 所有有活段都选择等待：跳到最近的忙段事件
                if not busy_times:
                    continue
                t2 = min(busy_times)
                res = dfs(
                    t2,
                    tuple(t2 if x == t else x for x in free),
                    f_done,
                    b_next,
                    active,
                    intervals,
                )
                results.append(res)
                continue
            next_t = min(branch_ends)
            # 本拍无活（或主动空转）的空闲段推进到下一事件时刻
            for s in free_stages:
                if nfree[s] == t:
                    nfree[s] = next_t
            res = dfs(next_t, tuple(nfree), tuple(fd), tuple(bn),
                      tuple(ac), iv)
            results.append(res)
            if res < best_makespan:
                best_makespan = res

        result = min(results) if results else _INF
        if result < _INF:
            memo[key] = result
        return result

    dfs(
        0,
        tuple(0 for _ in range(S)),
        tuple(0 for _ in range(M)),
        tuple(S - 1 for _ in range(M)),
        tuple(() for _ in range(S)),
        {},
    )

    if best_makespan >= _INF - 1:
        raise RuntimeError("不存在可行调度（不应发生：所有合法配置均存在可行调度）")

    operations: list[Operation] = []
    for (kind, s, m), (start, end) in best_intervals.items():
        operations.append(Operation(stage=s, microbatch=m, kind=kind,
                                    start=start, end=end))
    operations.sort(key=lambda o: (o.start, o.stage, o.microbatch, o.kind))

    peaks = _activation_peaks(S, M, best_intervals)
    return Schedule(
        makespan=best_makespan,
        operations=tuple(operations),
        activation_peak=tuple(peaks),
    )


def _activation_peaks(
    S: int, M: int, intervals: dict[tuple, tuple[int, int]]
) -> list[int]:
    """按所有事件时刻重放占用计数，求每段激活峰值。"""
    peaks = [0] * S
    for s in range(S):
        events = []
        for m in range(M):
            f = intervals.get(("forward", s, m))
            b = intervals.get(("backward", s, m))
            if f is not None and b is not None:
                # 半开占用 [F.start, B.end)：B 结束时刻即释放。
                events.append((f[0], +1))
                events.append((b[1], -1))
        events.sort(key=lambda e: (e[0], e[1]))  # 同一时刻先释放后占用
        cur = 0
        for _, delta in events:
            cur += delta
            peaks[s] = max(peaks[s], cur)
    return peaks


def verify_schedule(
    stages: list[StageConfig],
    num_microbatches: int,
    schedule: Schedule,
) -> dict:
    """独立重放校验调度是否满足全部交叉约束，返回统计报告。

    任一约束被违反时抛出 :class:`ValueError`。
    """
    _validate(stages, num_microbatches)
    S = len(stages)
    M = num_microbatches
    ops = list(schedule.operations)

    # 1) 操作齐全且时长/编号合法。
    seen: set[tuple] = set()
    for op in ops:
        key = (op.kind, op.stage, op.microbatch)
        if key in seen:
            raise ValueError(f"重复操作：{key}")
        seen.add(key)
        if op.kind not in ("forward", "backward"):
            raise ValueError(f"未知操作类型：{op.kind}")
        if not (0 <= op.stage < S and 0 <= op.microbatch < M):
            raise ValueError(f"操作越界：{op}")
        if op.end <= op.start:
            raise ValueError(f"操作时长必须为正：{op}")
        expected = (
            stages[op.stage].forward_duration
            if op.kind == "forward"
            else stages[op.stage].backward_duration
        )
        if op.end - op.start != expected:
            raise ValueError(
                f"{op.kind}({op.stage},{op.microbatch}) 时长 "
                f"{op.end - op.start} != 配置 {expected}"
            )
    for s in range(S):
        for m in range(M):
            for kind in ("forward", "backward"):
                if (kind, s, m) not in seen:
                    raise ValueError(f"缺少操作：{kind} 段{s} 微批{m}")

    iv = {
        (op.kind, op.stage, op.microbatch): (op.start, op.end) for op in ops
    }

    # 2) 设备互斥：同段任意两个操作半开区间不重叠（首尾相接允许）。
    for s in range(S):
        seg = sorted(
            [(op.start, op.end) for op in ops if op.stage == s],
            key=lambda x: (x[0], x[1]),
        )
        for (a0, a1), (b0, b1) in zip(seg, seg[1:]):
            if b0 < a1:
                raise ValueError(
                    f"段{s}设备时间重叠：[{a0},{a1}) 与 [{b0},{b1})"
                )

    # 3) 微批内偏序：前向逐段推进、反向倒序推进、同段 F 先于 B。
    for m in range(M):
        for s in range(S):
            fs, fe = iv[("forward", s, m)]
            bs, be = iv[("backward", s, m)]
            if fe > bs:
                raise ValueError(f"微批{m} 段{s} 反向早于前向结束")
            if s + 1 < S:
                nfs, _ = iv[("forward", s + 1, m)]
                if nfs < fe:
                    raise ValueError(f"微批{m} 前向跨段早于上段完成")
                _, nbe = iv[("backward", s + 1, m)]
                pbs, _ = iv[("backward", s, m)]
                if pbs < nbe:
                    raise ValueError(f"微批{m} 反向未按倒序执行")

    # 4) 激活槽：占用 [F.start, B.end)，任意时刻不超容量。
    peaks = [0] * S
    for s in range(S):
        events = []
        for m in range(M):
            fs, _ = iv[("forward", s, m)]
            _, be = iv[("backward", s, m)]
            events.append((fs, +1))
            events.append((be, -1))
        events.sort(key=lambda e: (e[0], e[1]))  # 同时刻先释放
        cur = 0
        for _, delta in events:
            cur += delta
            if cur > stages[s].activation_capacity:
                raise ValueError(
                    f"段{s}激活占用 {cur} 超过容量 "
                    f"{stages[s].activation_capacity}"
                )
            peaks[s] = max(peaks[s], cur)

    # 5) makespan / 峰值与返回值一致。
    real_makespan = max(op.end for op in ops)
    if real_makespan != schedule.makespan:
        raise ValueError(
            f"makespan 不一致：{schedule.makespan} != {real_makespan}"
        )
    if tuple(peaks) != tuple(schedule.activation_peak):
        raise ValueError(
            f"激活峰值不一致：{schedule.activation_peak} != {tuple(peaks)}"
        )

    return {
        "stages": S,
        "microbatches": M,
        "operations": len(ops),
        "makespan": real_makespan,
        "activation_peak": tuple(peaks),
    }
