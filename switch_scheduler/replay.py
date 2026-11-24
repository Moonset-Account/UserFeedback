"""设备指令回放与读后重排。

replay：把“发给设备的连线序列 + 每拍发数”在问题实例上逐拍回放，
    校验合法性（部分匹配、不可用交叉点、变更拍不发数、窗口与超量发送），
    产出实际完成矩阵，并在不可行窗口中指出首个已错过的需求。

replan：读到某拍后的实际完成矩阵时，冻结此前拍序，从当时真实连线与
    剩余业务重新求最优后续拍序。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from .matching import active_edges, changed_ports, matching_is_valid
from .model import (
    BeatRecord,
    IllegalPlanError,
    MissedDemand,
    Problem,
    Schedule,
)
from .scheduler import first_missed, solve


@dataclass(frozen=True)
class ReplayResult:
    num_beats: int
    records: tuple[BeatRecord, ...]
    completed: tuple[tuple[int, ...], ...]
    remaining: tuple[tuple[int, ...], ...]
    cleared: bool
    first_missed: Optional[MissedDemand]


def replay(
    problem: Problem,
    matchings: Sequence[tuple[int, ...]],
    declared_sends: Optional[Sequence[Iterable[tuple[int, int]]]] = None,
    actual_completion: Optional[Sequence[Sequence[int]]] = None,
) -> ReplayResult:
    """逐拍回放设备连线与发数。

    参数：
        matchings: 每拍发给设备的目标匹配（长度 n_in，-1 悬空）。
        declared_sends: 可选，每拍声明实际发数的边。回放引擎按硬件规则
            独立计算“本拍可发的边”，若声明包含不可发的边即判指令非法；
            缺省时认为可发的边全部发数（与求解器确定性策略一致）。
        actual_completion: 可选，设备回报的截至当前已完成矩阵。给出时以它
            为准重算剩余需求（用于 replan 与真实环境对齐）。
    """
    n_in, n_out = problem.n_in, problem.n_out
    if declared_sends is not None and len(declared_sends) != len(matchings):
        raise IllegalPlanError("declared_sends 与 matchings 拍数不一致")

    remaining = [list(row) for row in problem.demand]
    completed = [[0] * n_out for _ in range(n_in)]
    prev = problem.previous_matching
    records: list[BeatRecord] = []
    first_miss: Optional[MissedDemand] = None

    for beat, m_raw in enumerate(matchings):
        m = tuple(m_raw)
        if len(m) != n_in:
            raise IllegalPlanError(f"第 {beat} 拍匹配长度应为 {n_in}")
        if not matching_is_valid(m, n_out, problem.forbidden):
            raise IllegalPlanError(f"第 {beat} 拍匹配非法（重复输出或不可用交叉点）: {m}")

        changed = changed_ports(prev, m)
        active = active_edges(prev, m)

        # 硬件规则下本拍允许发数的边：未改连线 + 有剩余 + 窗口开放
        engine_sends: list[tuple[int, int]] = []
        idle: list[tuple[int, int]] = []
        for i, j in active:
            if remaining[i][j] <= 0:
                continue
            l, r = problem.window_of(i, j)
            if l <= beat <= r:
                engine_sends.append((i, j))
            else:
                idle.append((i, j))

        if declared_sends is not None:
            declared = tuple(sorted((int(i), int(j)) for i, j in declared_sends[beat]))
            allowed_set = set(engine_sends)
            for edge in declared:
                if edge not in allowed_set:
                    raise IllegalPlanError(
                        f"第 {beat} 拍声明在边 {edge} 发数，但该边本拍不可发"
                        f"（已改连线/无剩余/窗口外）"
                    )
            sends = declared
        else:
            sends = tuple(engine_sends)

        for i, j in sends:
            if remaining[i][j] <= 0:
                raise IllegalPlanError(f"第 {beat} 拍边 ({i},{j}) 超量发送")
            remaining[i][j] -= 1
            completed[i][j] += 1

        # 留空的可发边
        idle_final = tuple(sorted(set(engine_sends) - set(sends)))
        rem_tuple = tuple(tuple(row) for row in remaining)
        miss = first_missed(problem, rem_tuple, beat)
        if miss is not None and first_miss is None:
            first_miss = miss

        records.append(
            BeatRecord(
                beat=beat,
                matching=m,
                changed_ports=changed,
                active_edges=active,
                sends=tuple(sorted(sends)),
                idle_edges=idle_final + tuple(sorted(idle)),
            )
        )
        prev = m

    if actual_completion is not None:
        comp = [list(row) for row in actual_completion]
        if len(comp) != n_in or any(len(row) != n_out for row in comp):
            raise IllegalPlanError("actual_completion 矩阵形状不匹配")
        for i in range(n_in):
            for j in range(n_out):
                v = comp[i][j]
                if v < 0 or v > problem.demand[i][j]:
                    raise IllegalPlanError(
                        f"actual_completion[{i}][{j}]={v} 超出原始需求"
                    )
        completed = comp
        remaining = [
            [problem.demand[i][j] - comp[i][j] for j in range(n_out)]
            for i in range(n_in)
        ]
        rem_tuple = tuple(tuple(row) for row in remaining)
        miss = first_missed(problem, rem_tuple, len(matchings) - 1 if matchings else 0)
        if miss is not None and first_miss is None:
            first_miss = miss

    rem_final = tuple(tuple(row) for row in remaining)
    comp_final = tuple(tuple(row) for row in completed)
    cleared = all(x == 0 for row in rem_final for x in row)
    return ReplayResult(
        num_beats=len(matchings),
        records=tuple(records),
        completed=comp_final,
        remaining=rem_final,
        cleared=cleared,
        first_missed=first_miss,
    )


def replan(
    problem: Problem,
    frozen_matchings: Sequence[tuple[int, ...]],
    actual_completion: Sequence[Sequence[int]],
) -> tuple[Schedule, Schedule]:
    """冻结前缀并从真实连线/剩余业务重排。

    返回 (frozen, suffix)：
        frozen: 冻结前缀的回放 Schedule（记录真实执行结果）；
        suffix: 从当前真实连线出发、针对剩余业务的最优后续 Schedule。
    后续窗口拍号按“全帧绝对拍号”对齐：子问题窗口整体平移 frozen 拍数。
    """
    n_in, n_out = problem.n_in, problem.n_out
    k = len(frozen_matchings)
    replay_result = replay(
        problem,
        frozen_matchings,
        actual_completion=actual_completion,
    )
    current_matching = (
        replay_result.records[-1].matching
        if replay_result.records
        else problem.previous_matching
    )

    shifted_windows: dict[tuple[int, int], tuple[int, int]] = {}
    for cell, (l, r) in problem.windows.items():
        shifted_windows[cell] = (max(0, l - k), max(0, r - k))
    default_l = max(0, problem.default_window[0] - k)
    default_r = max(0, problem.default_window[1] - k)

    sub_problem = Problem.build(
        demand=replay_result.remaining,
        forbidden=problem.forbidden,
        windows=shifted_windows,
        previous_matching=current_matching,
        default_window=(default_l, default_r),
    )
    suffix = solve(sub_problem)

    # 把后缀记录的拍号还原为全帧绝对拍号
    suffix_records = tuple(
        BeatRecord(
            beat=rec.beat + k,
            matching=rec.matching,
            changed_ports=rec.changed_ports,
            active_edges=rec.active_edges,
            sends=rec.sends,
            idle_edges=rec.idle_edges,
        )
        for rec in suffix.records
    )
    suffix_global = Schedule(
        records=suffix_records,
        total_changes=suffix.total_changes,
        demand=replay_result.remaining,
        forbidden=problem.forbidden,
        windows=dict(shifted_windows),
        initial_matching=current_matching,
    )
    frozen = Schedule(
        records=replay_result.records,
        total_changes=sum(len(r.changed_ports) for r in replay_result.records),
        demand=problem.demand,
        forbidden=problem.forbidden,
        windows=dict(problem.windows),
        initial_matching=problem.previous_matching,
    )
    return frozen, suffix_global
