"""主场景：两条修复链共用一个热备，其中一条告警在中途解除。

验收点（直接对应题面）：
  1. 两条链先后提交，只有一条能占位，另一条被全链路冲突挡住并留下 BLOCKED 回执；
  2. 先占链进入不可撤销阶段后，取消会被拒绝，动作继续等待执行器结果；
  3. 被挡链的告警中途解除，随后按最新事实重新核对并拒绝过期动作；
  4. 先占链完整走通后，另一条链不再发出任何动作，剩余动作全部拒绝；
  5. 处置记录（回执账本）完整覆盖规划/占位/派发/阶段/生效/确认/中止。
"""

from __future__ import annotations

import unittest

from remediation_coordinator import ActionState, ChainStatus, EventType

from tests.fixtures import (
    ALERT_A,
    ALERT_B,
    CLUSTER_A,
    CLUSTER_B,
    HOT_STANDBY,
    build_chain_a,
    build_chain_b,
    build_coordinator,
)


class HotStandbyScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinator, self.executor = build_coordinator()

    def _event_names(self, action_id: str) -> list[str]:
        return [r.event for r in self.coordinator.receipts_for(action_id)]

    def test_full_hot_standby_dual_chain_scenario(self) -> None:
        # --- 规划两条链 ---------------------------------------------------
        self.coordinator.submit_chain("chain-a", build_chain_a())
        self.coordinator.submit_chain("chain-b", build_chain_b())

        # chain-a 头部动作已占位并派发；chain-b 头部动作被挡住。
        # 两个链的非终态动作因热备争用同属一个全链路冲突分量。
        a_reserve = self.coordinator.actions["a-reserve"]
        b_reserve = self.coordinator.actions["b-reserve"]
        self.assertIs(a_reserve.state, ActionState.DISPATCHED)
        self.assertIs(b_reserve.state, ActionState.PLANNED)
        self.assertEqual(
            self.coordinator.conflict_components(),
            [["chain-a", "chain-b"]],
        )
        self.assertEqual(self._event_names("a-reserve")[:3],
                         ["planned", "scheduled", "dispatched"])
        blocked = self.coordinator.receipts_for("b-reserve")
        self.assertEqual(blocked[-1].event, EventType.BLOCKED.value)
        self.assertTrue(blocked[-1].details["reason"])
        self.assertEqual(blocked[-1].details["chains"], ["chain-a"])

        # 只有一条派发指令真正到达执行器
        self.assertEqual(len(self.executor.dispatches), 1)
        self.assertEqual(self.executor.dispatches[0].action_id, "a-reserve")
        gen_a = self.executor.dispatches[0].generation

        # --- a-reserve 进入不可撤销阶段 -----------------------------------
        self.coordinator.report_phase("a-reserve", gen_a, "bound")
        self.assertIs(a_reserve.state, ActionState.IRREVOCABLE)

        # 已经不可撤销：取消必须被拒绝，动作继续等结果
        cancel_reject = self.coordinator.cancel("a-reserve")
        self.assertEqual(cancel_reject.event, EventType.CANCEL_REJECTED.value)
        self.assertIs(a_reserve.state, ActionState.IRREVOCABLE)

        # a-reserve 生效（热备被占用，版本推进到 2），然后确认
        self.coordinator.report_effective(
            "a-reserve", gen_a, {HOT_STANDBY: 2}
        )
        self.assertIs(a_reserve.state, ActionState.EFFECTIVE)
        self.assertEqual(self.coordinator.world.resource_version(HOT_STANDBY), 2)
        self.coordinator.confirm("a-reserve", gen_a)
        self.assertIs(a_reserve.state, ActionState.CONFIRMED)

        # 头部确认后，链上的下一个动作 a-rebuild 被重新核对后派发
        a_rebuild = self.coordinator.actions["a-rebuild"]
        self.assertIs(a_rebuild.state, ActionState.DISPATCHED)
        gen_rebuild = a_rebuild.generation

        # --- 被挡链 B 的告警在 A 执行中途解除 ------------------------------
        self.coordinator.evidence_changed(ALERT_B, active=False)
        # 即使 B 仍被 A 占着热备而无法推进，重新核对发现证据已解除，
        # 过期动作立即拒绝并中止链 B（而不是继续排队占位）
        self.assertIs(b_reserve.state, ActionState.REJECTED)
        self.assertIs(
            self.coordinator.actions["b-rebuild"].state, ActionState.REJECTED
        )
        self.assertIs(
            self.coordinator.chains["chain-b"].status, ChainStatus.ABORTED
        )
        # 告警解除的当下，执行器从未收到过链 B 的任何派发
        self.assertEqual(
            {order.action_id for order in self.executor.dispatches},
            {"a-reserve", "a-rebuild"},
        )

        # a-rebuild 走完：不可撤销阶段 -> 生效（cluster-a 推进）-> 确认
        self.coordinator.report_phase("a-rebuild", gen_rebuild, "commit")
        self.assertIs(a_rebuild.state, ActionState.IRREVOCABLE)
        self.coordinator.report_effective(
            "a-rebuild", gen_rebuild, {CLUSTER_A: 2}
        )
        self.coordinator.confirm("a-rebuild", gen_rebuild)
        self.assertIs(a_rebuild.state, ActionState.CONFIRMED)

        # a-release 自动接棒并完成，释放热备（版本推进到 3）
        a_release = self.coordinator.actions["a-release"]
        self.assertIs(a_release.state, ActionState.DISPATCHED)
        gen_release = a_release.generation
        self.coordinator.report_effective(
            "a-release", gen_release, {HOT_STANDBY: 3}
        )
        self.coordinator.confirm("a-release", gen_release)
        self.assertIs(a_release.state, ActionState.CONFIRMED)
        self.assertEqual(self.coordinator.world.resource_version(HOT_STANDBY), 3)
        self.assertIs(
            self.coordinator.chains["chain-a"].status, ChainStatus.COMPLETED
        )

        # --- 链 B 已在告警解除时被拒绝，链 A 走完后不再有 B 的动作被发出 -----
        self.assertTrue(b_reserve.reject_reason)
        for action_id in ("b-reserve", "b-rebuild"):
            self.assertIs(
                self.coordinator.actions[action_id].state,
                ActionState.REJECTED,
            )
        self.assertIs(
            self.coordinator.chains["chain-b"].status, ChainStatus.ABORTED
        )

        # 链 B 从未向执行器发出过任何派发指令
        dispatched_ids = {order.action_id for order in self.executor.dispatches}
        self.assertNotIn("b-reserve", dispatched_ids)
        self.assertNotIn("b-rebuild", dispatched_ids)
        self.assertEqual(dispatched_ids,
                         {"a-reserve", "a-rebuild", "a-release"})

        # 处置记录中链 B 有规划、阻塞、拒绝、链中止四类回执
        b_events = self._event_names("b-reserve")
        self.assertIn("planned", b_events)
        self.assertIn("blocked", b_events)
        self.assertIn("rejected", b_events)
        chain_aborted = [
            r for r in self.coordinator.receipts_for_chain("chain-b")
            if r.event == EventType.CHAIN_ABORTED.value
        ]
        self.assertEqual(len(chain_aborted), 1)

        # 链 B 全部动作终态后，再次 pump 不会产生任何新回执
        before = len(self.coordinator.receipts)
        self.coordinator.pump()
        self.assertEqual(len(self.coordinator.receipts), before)

    def test_other_chain_not_affected_by_unrelated_change(self) -> None:
        # 另起一个完全不相关的资源变化，只动它，两条链的核对结果不应改变
        self.coordinator.submit_chain("chain-a", build_chain_a())
        self.coordinator.submit_chain("chain-b", build_chain_b())
        before = len(self.coordinator.receipts)
        self.coordinator.resource_changed("cluster-x-unrelated", 7)
        new_receipts = self.coordinator.receipts[before:]
        # 只有一条 world_changed 回执，没有任何拒绝/额外阻塞
        self.assertEqual(len(new_receipts), 1)
        self.assertEqual(new_receipts[0].event, EventType.WORLD_CHANGED.value)
        self.assertIs(
            self.coordinator.actions["a-reserve"].state, ActionState.DISPATCHED
        )
        self.assertIs(
            self.coordinator.actions["b-reserve"].state, ActionState.PLANNED
        )

    def test_alert_cleared_while_head_scheduled_rejects_before_dispatch(self) -> None:
        # 让 chain-a 只规划不派发：不注入执行器时先手工停在占位态不可行，
        # 因此改用证据在派发前一轮变化的构造——重新建一个无执行器引擎，
        # 规划后头部应停在 SCHEDULED，再解除告警，下一次核对必须拒绝。
        from remediation_coordinator import RemediationCoordinator, WorldState
        from tests.fixtures import build_world

        world = build_world()
        coordinator = RemediationCoordinator(world, executor=None)
        coordinator.submit_chain("chain-a", build_chain_a())
        self.assertIs(
            coordinator.actions["a-reserve"].state, ActionState.SCHEDULED
        )
        coordinator.evidence_changed(ALERT_A, active=False)
        self.assertIs(
            coordinator.actions["a-reserve"].state, ActionState.REJECTED
        )
        self.assertIs(coordinator.chains["chain-a"].status, ChainStatus.ABORTED)


if __name__ == "__main__":
    unittest.main()
