"""临时写盘额度调度。

一次暂停写盘在半开区间 ``[write_start, write_finish)`` 内持续占用
``write_amount`` 个额度槽位；``write_finish`` 时刻额度立即释放，同一时刻
GPU 也立即释放，因此完成时刻可以立刻被新的写盘或新作业使用。

调度策略为事件驱动贪心：在每个“检查点到达 / 写盘完成”事件时刻，按
``(最早可开始时间, 作业ID)`` 顺序启动所有当前能放入剩余额度的写盘；
放不下的等待下一个写盘完成事件。该策略在所有写盘可串行等待的前提下
最小化最大写盘完成时间，且结果完全确定。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WriteRequest:
    """一次待调度的暂停写盘请求。"""

    job_id: str
    earliest_start: int
    duration: int
    amount: int


@dataclass(frozen=True)
class ScheduledWrite:
    """一次已落定的暂停写盘安排。"""

    job_id: str
    checkpoint_time: int
    write_amount: int
    write_start: int
    write_finish: int

    @property
    def delayed(self) -> bool:
        """写盘是否因为额度争用而晚于检查点时间启动。"""
        return self.write_start > self.checkpoint_time


def schedule_writes(
    requests: tuple[WriteRequest, ...] | list[WriteRequest],
    quota: int,
) -> tuple[ScheduledWrite, ...] | None:
    """在固定 ``quota`` 额度下调度全部写盘。

    返回按作业 ID 排序的调度结果；若某个写盘的 ``amount`` 本身超过总额度，
    则该组合不可能可行，返回 ``None``。
    """
    pending = sorted(requests, key=lambda r: (r.earliest_start, r.job_id))
    if any(r.amount > quota for r in pending):
        return None
    if not pending:
        return ()

    active: list[tuple[int, int]] = []  # (finish_time, amount)
    started: list[ScheduledWrite] = []
    t = pending[0].earliest_start

    while pending or active:
        # 释放 t 时刻已完成的写盘（半开区间：finish == t 即释放）
        active = [(finish, amount) for finish, amount in active if finish > t]
        active_total = sum(amount for _, amount in active)

        # 尽量按顺序启动已到达且放得下的写盘
        changed = True
        while changed:
            changed = False
            for idx, req in enumerate(pending):
                if req.earliest_start <= t and active_total + req.amount <= quota:
                    if req.duration == 0:
                        # 零时长写盘在 t 瞬间完成：额度当刻释放，不进入 active，
                        # 否则同一事件时刻无法再推进（半开区间的退化情形）。
                        started.append(
                            ScheduledWrite(
                                job_id=req.job_id,
                                checkpoint_time=req.earliest_start,
                                write_amount=req.amount,
                                write_start=t,
                                write_finish=t,
                            )
                        )
                        pending.pop(idx)
                        changed = True
                        break
                    finish = t + req.duration
                    active.append((finish, req.amount))
                    active_total += req.amount
                    started.append(
                        ScheduledWrite(
                            job_id=req.job_id,
                            checkpoint_time=req.earliest_start,
                            write_amount=req.amount,
                            write_start=t,
                            write_finish=finish,
                        )
                    )
                    pending.pop(idx)
                    changed = True
                    break

        if not pending and not active:
            break

        next_events = [finish for finish, _ in active]
        if pending:
            next_events.append(pending[0].earliest_start)
        future = [event for event in next_events if event > t]
        if not future:  # pragma: no cover - amount>quota 已在上面拦截
            return None
        t = min(future)

    return tuple(sorted(started, key=lambda w: w.job_id))
