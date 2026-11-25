"""读调度核心：SCAN 首选 + 截止期保护 + 降级择优。

调度在每个“决策点”（仿真开始或上一个请求读取完成、且已吸收该时刻
全部到达请求后）运行，候选集为当前队列中所有已到达请求。三层规则：

1. 普通时刻沿当前方向挑最近柱面（SCAN），同柱面先读较早能转到的
   扇区，构成普通首选 ``normal``。
2. 若按普通首选执行会使“已到请求”失去全部按时完成的顺序，则在所有
   仍能保住全部截止期的首选（safe first moves）里挑磁头移动最少者。
3. 若已不存在全保方案，则先最大化按时完成数，同分再按总寻道距离、
   再按编号序列（rid 字典序）择优。

读操作不可中断：一旦选定请求，寻道、旋转等待与读取必须一次走完，
期间到达的新请求只能在下一个决策点参与调度。
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import (
    MODE_BEST_EFFORT,
    MODE_NORMAL,
    MODE_SAFE,
    DiskConfig,
    Request,
    ServiceRecord,
    SimulationResult,
    TimelineEvent,
)


@dataclass(frozen=True)
class ServicePlan:
    """单次服务的时间计划（不含请求本身）。"""

    seek_distance: int
    seek_end: int
    read_start: int
    complete: int
    rotational_latency: int
    just_missed: bool


def earliest_sector_time(config: DiskConfig, time: int, sector: int) -> int:
    """最早在 ``time`` 之后（含恰好在该时刻对齐）读到 ``sector`` 的时刻。

    扇区 ``s`` 占据旋转位置槽 ``s``，其起始边界出现在
    ``(k * sectors_per_track + s) * ticks_per_sector`` 时刻。
    """
    tps = config.ticks_per_sector
    spt = config.sectors_per_track
    slot = time // tps
    aligned = time % tps == 0
    if aligned and slot % spt <= sector:
        target_slot = slot - (slot % spt) + sector
    else:
        # 当前圈内该扇区的起始边界已过（或此刻落在扇区中间），等下一圈。
        target_slot = slot - (slot % spt) + spt + sector
    return target_slot * tps


def plan_service(
    config: DiskConfig, time: int, cylinder: int, request: Request
) -> ServicePlan:
    """给定磁头状态与请求，计算完整服务计划。

    寻道期间盘片继续转动：寻道耗时只与柱面距离成正比，寻道结束时的
    旋转位置由结束时刻本身决定。
    """
    seek_distance = abs(cylinder - request.cylinder)
    seek_end = time + seek_distance * config.seek_ticks_per_cylinder
    read_start = earliest_sector_time(config, seek_end, request.sector)
    complete = read_start + request.length * config.ticks_per_sector
    latency = read_start - seek_end
    # “刚好错过”：目标扇区在寻道结束前不久刚转过去，几乎要等一整圈。
    # 阈值为一圈少一个扇区槽；含恰好在扇区结束边界到达的情形。
    just_missed = latency >= config.rotation_period - config.ticks_per_sector
    return ServicePlan(
        seek_distance=seek_distance,
        seek_end=seek_end,
        read_start=read_start,
        complete=complete,
        rotational_latency=latency,
        just_missed=just_missed,
    )


@dataclass(frozen=True)
class OrderEvaluation:
    """一个固定服务顺序的评估结果。"""

    order: tuple[int, ...]
    completions: dict[int, int]
    on_time: tuple[int, ...]
    total_seek_distance: int
    feasible: bool


def evaluate_order(
    config: DiskConfig,
    start_time: int,
    start_cylinder: int,
    requests: list[Request] | tuple[Request, ...],
) -> OrderEvaluation:
    """按给定顺序模拟服务，评估每条请求的完成时刻与达标情况。"""
    time = start_time
    cylinder = start_cylinder
    total_seek = 0
    completions: dict[int, int] = {}
    on_time: list[int] = []
    feasible = True
    for request in requests:
        plan = plan_service(config, time, cylinder, request)
        time = plan.complete
        cylinder = request.cylinder
        total_seek += plan.seek_distance
        completions[request.rid] = time
        if time <= request.deadline:
            on_time.append(request.rid)
        else:
            feasible = False
    return OrderEvaluation(
        order=tuple(r.rid for r in requests),
        completions=completions,
        on_time=tuple(on_time),
        total_seek_distance=total_seek,
        feasible=feasible,
    )


class DiskScheduler:
    """磁盘读调度仿真器。"""

    def __init__(self, config: DiskConfig):
        self.config = config

    # ------------------------------------------------------------------
    # 普通首选：SCAN 方向 + 最近柱面 + 最早转到的扇区
    # ------------------------------------------------------------------
    def _normal_key(self, request: Request, time: int, cylinder: int, direction: int):
        plan = plan_service(self.config, time, cylinder, request)
        if direction >= 0:
            ahead = request.cylinder >= cylinder
        else:
            ahead = request.cylinder <= cylinder
        direction_group = 0 if ahead else 1
        return (
            direction_group,
            plan.seek_distance,
            plan.rotational_latency,
            plan.read_start,
            request.rid,
        )

    def _normal_ranked(
        self, queue: list[Request], time: int, cylinder: int, direction: int
    ) -> list[Request]:
        return sorted(
            queue,
            key=lambda r: self._normal_key(r, time, cylinder, direction),
        )

    # ------------------------------------------------------------------
    # 截止期可行性：枚举排列，带记忆化与截止期剪枝
    # ------------------------------------------------------------------
    def _feasible(
        self,
        remaining: frozenset[int],
        time: int,
        cylinder: int,
        by_id: dict[int, Request],
        memo: dict[tuple, bool],
    ) -> bool:
        """remaining 中的请求能否在各自截止期内全部完成。"""
        if not remaining:
            return True
        key = (time, cylinder, remaining)
        if key in memo:
            return memo[key]
        for rid in remaining:
            request = by_id[rid]
            plan = plan_service(self.config, time, cylinder, request)
            if plan.complete > request.deadline:
                continue  # 该首选已违约，剪枝
            if self._feasible(
                remaining - {rid}, plan.complete, request.cylinder, by_id, memo
            ):
                memo[key] = True
                return True
        memo[key] = False
        return False

    def _safe_first_moves(
        self, queue: list[Request], time: int, cylinder: int
    ) -> set[int]:
        """返回所有“作为首个请求仍能保住全队列截止期”的 rid。"""
        by_id = {r.rid: r for r in queue}
        memo: dict[tuple, bool] = {}
        safe: set[int] = set()
        remaining = frozenset(by_id)
        for request in queue:
            plan = plan_service(self.config, time, cylinder, request)
            if plan.complete > request.deadline:
                continue
            if self._feasible(
                remaining - {request.rid},
                plan.complete,
                request.cylinder,
                by_id,
                memo,
            ):
                safe.add(request.rid)
        return safe

    # ------------------------------------------------------------------
    # 降级模式：最大化按时数 → 最小总寻道 → rid 序列字典序
    # ------------------------------------------------------------------
    def _best_effort_first(
        self, queue: list[Request], time: int, cylinder: int
    ) -> tuple[int, tuple]:
        best_key: tuple | None = None
        best_rid: int | None = None
        n = len(queue)

        def search(
            remaining: list[Request],
            t: int,
            cyl: int,
            seek_sum: int,
            on_time: int,
            sequence: list[int],
        ) -> None:
            nonlocal best_key, best_rid
            if not remaining:
                key = (-on_time, seek_sum, tuple(sequence))
                if best_key is None or key < best_key:
                    best_key = key
                    best_rid = sequence[0]
                return
            # 即使剩余全部按时也追不上已知最好成绩，剪枝。
            if best_key is not None and on_time + len(remaining) < -best_key[0]:
                return
            for request in remaining:
                plan = plan_service(self.config, t, cyl, request)
                search(
                    [r for r in remaining if r.rid != request.rid],
                    plan.complete,
                    request.cylinder,
                    seek_sum + plan.seek_distance,
                    on_time + (1 if plan.complete <= request.deadline else 0),
                    sequence + [request.rid],
                )

        search(queue, time, cylinder, 0, 0, [])
        assert best_rid is not None and best_key is not None
        return best_rid, best_key

    def _choose(
        self, queue: list[Request], time: int, cylinder: int, direction: int
    ) -> tuple[Request, str, dict]:
        """执行三层决策，返回 (选中请求, 模式, 说明)。"""
        ranked = self._normal_ranked(queue, time, cylinder, direction)
        normal = ranked[0]
        safe_ids = self._safe_first_moves(queue, time, cylinder)
        detail: dict = {
            "candidates": [r.rid for r in ranked],
            "normal_first": normal.rid,
            "safe_first_moves": sorted(safe_ids),
        }
        if safe_ids:
            if normal.rid in safe_ids:
                return normal, MODE_NORMAL, detail
            # 普通首选不在安全集中：在安全首选里挑寻道最少者。
            safe_requests = [r for r in queue if r.rid in safe_ids]
            chosen = min(
                safe_requests,
                key=lambda r: (
                    plan_service(self.config, time, cylinder, r).seek_distance,
                    plan_service(self.config, time, cylinder, r).rotational_latency,
                    plan_service(self.config, time, cylinder, r).read_start,
                    r.rid,
                ),
            )
            detail["override_reason"] = "normal_choice_misses_some_deadline"
            return chosen, MODE_SAFE, detail
        rid, best_key = self._best_effort_first(queue, time, cylinder)
        detail["best_effort_key"] = {
            "on_time": -best_key[0],
            "total_seek": best_key[1],
            "sequence": list(best_key[2]),
        }
        chosen = next(r for r in queue if r.rid == rid)
        return chosen, MODE_BEST_EFFORT, detail

    # ------------------------------------------------------------------
    # 仿真主循环
    # ------------------------------------------------------------------
    def run(self, requests: list[Request]) -> SimulationResult:
        for request in requests:
            request.validate_geometry(self.config)
        rids = [r.rid for r in requests]
        if len(set(rids)) != len(rids):
            raise ValueError("请求 rid 必须唯一")
        arrivals = sorted(requests, key=lambda r: (r.arrival, r.rid))
        arrival_index = 0

        time = 0
        cylinder = self.config.head_cylinder
        direction = 1  # 初始方向：柱面号增大方向
        queue: list[Request] = []
        records: list[ServiceRecord] = []
        timeline: list[TimelineEvent] = []
        rejected: list[int] = []
        served_order: list[int] = []

        while True:
            # 1. 吸收当前时刻之前（含）的全部到达。
            while (
                arrival_index < len(arrivals)
                and arrivals[arrival_index].arrival <= time
            ):
                request = arrivals[arrival_index]
                arrival_index += 1
                if len(queue) < self.config.max_queue:
                    queue.append(request)
                    timeline.append(
                        TimelineEvent(
                            time=request.arrival,
                            kind="arrival",
                            rid=request.rid,
                            accepted=True,
                            detail=f"请求 {request.rid} 到达并进入队列",
                        )
                    )
                else:
                    rejected.append(request.rid)
                    timeline.append(
                        TimelineEvent(
                            time=request.arrival,
                            kind="arrival",
                            rid=request.rid,
                            accepted=False,
                            detail=(
                                f"请求 {request.rid} 到达时队列已满"
                                f"（上限 {self.config.max_queue}），被拒绝"
                            ),
                        )
                    )

            # 2. 队列空：空闲到下一到达时刻。
            if not queue:
                if arrival_index >= len(arrivals):
                    break
                next_arrival = arrivals[arrival_index].arrival
                timeline.append(
                    TimelineEvent(
                        time=time,
                        kind="idle",
                        detail=f"队列空，磁头空闲至 tick {next_arrival}",
                        extra={"resume_time": next_arrival},
                    )
                )
                time = next_arrival
                continue

            # 3. 三层调度决策。
            chosen, mode, detail = self._choose(queue, time, cylinder, direction)
            old_direction = direction
            if chosen.cylinder > cylinder:
                direction = 1
            elif chosen.cylinder < cylinder:
                direction = -1
            detail = dict(detail)
            detail["direction"] = direction
            if direction != old_direction and chosen.cylinder != cylinder:
                detail["direction_reversed"] = True
            timeline.append(
                TimelineEvent(
                    time=time,
                    kind="decision",
                    rid=chosen.rid,
                    mode=mode,
                    detail=f"按 {mode} 规则选择请求 {chosen.rid}",
                    extra=detail,
                )
            )

            # 4. 不可中断地完成寻道、旋转等待与读取。
            plan = plan_service(self.config, time, cylinder, chosen)
            if plan.seek_distance > 0:
                timeline.append(
                    TimelineEvent(
                        time=time,
                        kind="seek",
                        rid=chosen.rid,
                        detail=(
                            f"柱面 {cylinder} -> {chosen.cylinder}，"
                            f"寻道 {plan.seek_distance} 柱面，"
                            f"tick {time} -> {plan.seek_end}"
                        ),
                        extra={
                            "from_cylinder": cylinder,
                            "to_cylinder": chosen.cylinder,
                            "seek_end": plan.seek_end,
                        },
                    )
                )
            if plan.rotational_latency > 0:
                timeline.append(
                    TimelineEvent(
                        time=plan.seek_end,
                        kind="rotational_wait",
                        rid=chosen.rid,
                        detail=(
                            f"等待扇区 {chosen.sector} 转来，"
                            f"延迟 {plan.rotational_latency} tick，"
                            f"tick {plan.seek_end} -> {plan.read_start}"
                        ),
                        extra={
                            "head_sector": self.config.head_sector_at(plan.seek_end),
                            "target_sector": chosen.sector,
                            "read_start": plan.read_start,
                        },
                    )
                )
                if plan.just_missed:
                    timeline.append(
                        TimelineEvent(
                            time=plan.seek_end,
                            kind="sector_miss",
                            rid=chosen.rid,
                            detail=(
                                f"寻道期间盘片转动，磁头在 tick {plan.seek_end}"
                                f" 刚好错过扇区 {chosen.sector}，"
                                f"需再等 {plan.rotational_latency} tick"
                            ),
                            extra={
                                "head_sector": self.config.head_sector_at(
                                    plan.seek_end
                                ),
                                "target_sector": chosen.sector,
                                "latency": plan.rotational_latency,
                            },
                        )
                    )
            timeline.append(
                TimelineEvent(
                    time=plan.read_start,
                    kind="read",
                    rid=chosen.rid,
                    detail=(
                        f"读取 {chosen.length} 扇区，"
                        f"tick {plan.read_start} -> {plan.complete}"
                    ),
                    extra={"complete": plan.complete},
                )
            )

            records.append(
                ServiceRecord(
                    rid=chosen.rid,
                    arrival=chosen.arrival,
                    deadline=chosen.deadline,
                    cylinder=chosen.cylinder,
                    sector=chosen.sector,
                    length=chosen.length,
                    seek_start=time,
                    seek_end=plan.seek_end,
                    read_start=plan.read_start,
                    complete=plan.complete,
                    seek_distance=plan.seek_distance,
                    rotational_latency=plan.rotational_latency,
                    just_missed=plan.just_missed,
                    on_time=plan.complete <= chosen.deadline,
                )
            )
            served_order.append(chosen.rid)
            time = plan.complete
            cylinder = chosen.cylinder
            queue = [r for r in queue if r.rid != chosen.rid]

        return SimulationResult(
            records=tuple(records),
            timeline=tuple(timeline),
            rejected=tuple(rejected),
            served_order=tuple(served_order),
        )
