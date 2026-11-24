"""可恢复的迁移器策略。

迁移器把两阶段搬迁做成可恢复的状态机，状态可序列化（:meth:`to_json`），
控制器在占位成功、旧副本释放或回执写入日志之前切换后，新控制器用
:meth:`from_json` 接管即可从最后一个已确认阶段继续，不会重复占位或重复释放。

安全闸门 :meth:`can_proceed` 只有在当前**所有可能现场**都安全时才放行撤机。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .logs import MigrationTicket, possible_worlds
from .model import Plan, PlanningError, Topology
from .simulate import (
    ReplayEvent,
    Snapshot,
    replay_plan,
    release_action,
    reserve_action,
)
from .uncertainty import SafetyContext


@dataclass
class ControllerState:
    """迁移器的可持久化状态。"""

    step: int = 0
    phase: str = "idle"            # idle / reserved / done
    in_flight: tuple[str, ...] = ()
    destination: str | None = None
    history: tuple[ReplayEvent, ...] = ()
    last_error: str | None = None

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "phase": self.phase,
            "in_flight": list(self.in_flight),
            "destination": self.destination,
            "history": [
                {
                    "kind": e.kind,
                    "ticket": e.ticket,
                    "pod": e.pod,
                    "node": e.node,
                    "snapshot": {
                        "phase": e.snapshot.phase,
                        "placement": list(e.snapshot.placement),
                        "reserved": [list(r) for r in e.snapshot.reserved],
                    },
                }
                for e in self.history
            ],
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ControllerState":
        history = tuple(
            ReplayEvent(
                kind=e["kind"],
                ticket=e["ticket"],
                pod=e["pod"],
                node=e["node"],
                snapshot=Snapshot(
                    phase=e["snapshot"]["phase"],
                    placement=tuple(e["snapshot"]["placement"]),
                    reserved=tuple(tuple(r) for r in e["snapshot"]["reserved"]),
                ),
            )
            for e in data.get("history", [])
        )
        return cls(
            step=data["step"],
            phase=data["phase"],
            in_flight=tuple(data.get("in_flight", ())),
            destination=data.get("destination"),
            history=history,
            last_error=data.get("last_error"),
        )


class DrainController:
    """按计划逐步执行、可在任意阶段后恢复的迁移器。"""

    def __init__(self, topology: Topology, plan: Plan):
        self.topology = topology
        self.plan = plan
        # 完整预期事件序列，用于恢复后对账。
        self._expected = replay_plan(topology, plan)
        self.state = ControllerState()
        self._snapshot = self._expected[0].snapshot

    # ---- 持久化 -------------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(self.state.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, topology: Topology, plan: Plan, payload: str) -> "DrainController":
        controller = cls(topology, plan)
        controller.state = ControllerState.from_dict(json.loads(payload))
        controller._snapshot = (
            controller.state.history[-1].snapshot
            if controller.state.history
            else controller._expected[0].snapshot
        )
        return controller

    # ---- 执行 ---------------------------------------------------------
    def reserve_next(self) -> list[ReplayEvent]:
        """发起下一张票据的占位（同机闭包整体），返回占位事件。"""
        if self.state.phase == "reserved":
            raise PlanningError("上一张票据尚未确认释放，不能发起新占位")
        if self.state.phase == "done":
            raise PlanningError("计划已执行完毕")
        if self.state.step >= len(self.plan.actions):
            raise PlanningError("计划已执行完毕")
        action = self.plan.actions[self.state.step]
        events = reserve_action(self.topology, self._snapshot, action)
        self.state.phase = "reserved"
        self.state.in_flight = action.clique
        self.state.destination = action.destination
        self.state.history = self.state.history + tuple(events)
        self._snapshot = events[-1].snapshot
        return events

    def confirm_release(self) -> list[ReplayEvent]:
        """确认当前票据的源端副本全部删除并完成动作。"""
        if self.state.phase != "reserved":
            raise PlanningError("没有进行中的占位，无法确认释放")
        action = self.plan.actions[self.state.step]
        events = release_action(self.topology, self._snapshot, action)
        self.state.history = self.state.history + tuple(events)
        self._snapshot = events[-1].snapshot
        self.state.step += 1
        self.state.phase = "done" if self.state.step >= len(self.plan.actions) else "idle"
        self.state.in_flight = ()
        self.state.destination = None
        return events

    def run_all(self) -> tuple[ReplayEvent, ...]:
        """一次性跑完整张计划（无中断场景的便捷方法）。"""
        while self.state.phase != "done":
            self.reserve_next()
            self.confirm_release()
        return self.state.history

    # ---- 安全闸门 -----------------------------------------------------
    def can_proceed(self, tickets: list[MigrationTicket],
                    context: SafetyContext) -> bool:
        """当前所有可能现场都安全才允许继续撤机。"""
        worlds = possible_worlds(tickets)
        return all(context.is_safe(world, tuple(tickets)) for world in worlds)

    @property
    def current_snapshot(self) -> Snapshot:
        return self._snapshot

    @property
    def finished(self) -> bool:
        return self.state.phase == "done"
