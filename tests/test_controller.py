"""可恢复迁移器：占位后切换、释放后切换、安全闸门。"""

from __future__ import annotations

import _bootstrap  # noqa: F401
import unittest

from safedrain.controller import DrainController
from safedrain.logs import (
    LogEvent,
    LogEventKind,
    MigrationTicket,
)
from safedrain.model import PlanningError
from safedrain.planner import plan_for_retirement
from safedrain.simulate import physical_usage, replay_plan
from safedrain.uncertainty import SafetyContext

from factories import build, node, pod


def _topo():
    return build(
        [
            node("n1", cpu=2, mem=2),
            node("n2", cpu=2, mem=2),
            node("n3", cpu=2, mem=2),
        ],
        [pod("p1", on="n1"), pod("p2", on="n1")],
    )


class ControllerExecutionTests(unittest.TestCase):
    def test_run_all_matches_replay_events(self):
        topo = _topo()
        plan = plan_for_retirement(topo, frozenset({"n1"}))
        controller = DrainController(topo, plan)
        history = controller.run_all()
        expected = replay_plan(topo, plan)
        self.assertEqual(
            [(e.kind, e.ticket, e.pod) for e in history],
            [(e.kind, e.ticket, e.pod) for e in expected[1:]],
        )
        self.assertTrue(controller.finished)

    def test_reserve_twice_without_release_rejected(self):
        topo = _topo()
        plan = plan_for_retirement(topo, frozenset({"n1"}))
        controller = DrainController(topo, plan)
        controller.reserve_next()
        with self.assertRaises(PlanningError):
            controller.reserve_next()

    def test_release_without_reserve_rejected(self):
        topo = _topo()
        plan = plan_for_retirement(topo, frozenset({"n1"}))
        controller = DrainController(topo, plan)
        with self.assertRaises(PlanningError):
            controller.confirm_release()


class RecoveryInterleaveTests(unittest.TestCase):
    def test_switch_after_reservation_resumes_without_double_booking(self):
        topo = _topo()
        plan = plan_for_retirement(topo, frozenset({"n1"}))
        controller = DrainController(topo, plan)
        reserved_events = controller.reserve_next()
        self.assertEqual(controller.state.phase, "reserved")
        # 切换点 1：占位成功、释放之前。
        payload = controller.to_json()
        successor = DrainController.from_json(topo, plan, payload)
        self.assertEqual(successor.state.phase, "reserved")
        self.assertEqual(successor.state.in_flight, ("p1",))
        # 恢复后不得重新占位，直接继续释放。
        release_events = successor.confirm_release()
        self.assertEqual([e.kind for e in release_events][:1], ["released"])
        self.assertEqual(successor.state.step, 1)
        # 资源对账：没有重复预留。
        snap = successor.current_snapshot
        self.assertEqual(physical_usage(topo, snap, "n2").cpu, 1)
        self.assertEqual(physical_usage(topo, snap, "n1").cpu, 1)
        successor.run_all()
        self.assertTrue(successor.finished)

    def test_switch_after_release_continues_next_ticket(self):
        topo = _topo()
        plan = plan_for_retirement(topo, frozenset({"n1"}))
        controller = DrainController(topo, plan)
        controller.reserve_next()
        controller.confirm_release()
        # 切换点 2：第一张票据完整结束、回执已写入。
        payload = controller.to_json()
        successor = DrainController.from_json(topo, plan, payload)
        self.assertEqual(successor.state.phase, "idle")
        self.assertEqual(successor.state.step, 1)
        successor.run_all()
        self.assertTrue(successor.finished)
        final = successor.current_snapshot
        self.assertNotIn("n1", final.placement)

    def test_round_trip_state_is_json_stable(self):
        topo = _topo()
        plan = plan_for_retirement(topo, frozenset({"n1"}))
        controller = DrainController(topo, plan)
        controller.reserve_next()
        payload = controller.to_json()
        successor = DrainController.from_json(topo, plan, payload)
        self.assertEqual(successor.to_json(), payload)


class SafetyGateTests(unittest.TestCase):
    def _tickets(self):
        t1 = MigrationTicket(
            "T1", "p1", "n1", ("n2", "n3"),
            (LogEvent("T1", LogEventKind.RESERVE_PENDING, "n1", ("n2", "n3")),),
        )
        return [t1]

    def test_gate_blocks_when_any_world_unsafe(self):
        topo = _topo()
        plan = plan_for_retirement(topo, frozenset({"n1"}))
        controller = DrainController(topo, plan)
        tickets = self._tickets()
        # 只有落在 n2 才安全，落在 n3 的现场不安全 -> 不得放行。
        ctx = SafetyContext(frozenset([("n2",)]))
        self.assertFalse(controller.can_proceed(tickets, ctx))

    def test_gate_opens_when_every_world_safe(self):
        topo = _topo()
        plan = plan_for_retirement(topo, frozenset({"n1"}))
        controller = DrainController(topo, plan)
        tickets = self._tickets()
        ctx = SafetyContext(frozenset([("n2",), ("n3",)]))
        self.assertTrue(controller.can_proceed(tickets, ctx))


if __name__ == "__main__":
    unittest.main()
