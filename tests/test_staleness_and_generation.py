"""过期核对与带代次回执的测试。

覆盖：
  * 规划后、开始前（planned/scheduled）资源版本或证据变化 -> 拒绝过期动作；
  * 已派发动作为在途动作，外部变化不撤销，继续等待执行器结果；
  * 在途动作结算后，其后续动作按最新事实重新核对并被拒绝/重排；
  * 无关资源变化不影响其他链；
  * 过期代次的执行器回执只记 STALE_RESULT，不改变动作状态；
  * 不可撤销阶段不接受取消；取消窗口内取消成功/取消迟到（已生效）的交错。
"""

from __future__ import annotations

import unittest

from remediation_coordinator import (
    ActionSpec,
    ActionState,
    ChainStatus,
    EventType,
    RemediationCoordinator,
    ResourceVersion,
    WorldState,
    Evidence,
)

from tests.fixtures import (
    ALERT_A,
    HOT_STANDBY,
    build_chain_a,
    build_coordinator,
    build_world,
)


def single_action_world() -> WorldState:
    return WorldState(
        resources={HOT_STANDBY: 1, "cluster-x": 1},
        evidence={
            ALERT_A: Evidence(
                evidence_id=ALERT_A,
                active=True,
                observed_versions={HOT_STANDBY: 1},
            )
        },
    )


class StalenessAndGenerationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinator, self.executor = build_coordinator()

    def test_scheduled_action_rejected_when_resource_changes_before_start(self) -> None:
        # 无执行器：动作只推导到 SCHEDULED（待开始），尚未真正开始
        coordinator = RemediationCoordinator(build_world(), executor=None)
        coordinator.submit_chain("chain-a", build_chain_a())
        head = coordinator.actions["a-reserve"]
        self.assertIs(head.state, ActionState.SCHEDULED)
        coordinator.resource_changed(HOT_STANDBY, 8)
        self.assertIs(head.state, ActionState.REJECTED)
        self.assertTrue(head.reject_reason)
        self.assertIs(coordinator.chains["chain-a"].status, ChainStatus.ABORTED)
        for later in ("a-rebuild", "a-release"):
            self.assertIs(
                coordinator.actions[later].state, ActionState.REJECTED
            )

    def test_in_flight_action_not_revoked_by_external_change(self) -> None:
        # 已派发=在途：外部资源变化不能撤销它，只能等执行器带代次的结果
        self.coordinator.submit_chain("chain-a", build_chain_a())
        head = self.coordinator.actions["a-reserve"]
        gen = head.generation
        self.coordinator.resource_changed(HOT_STANDBY, 8)
        self.assertIs(head.state, ActionState.DISPATCHED)
        # 执行器结果仍按原代次正常结算
        receipt = self.coordinator.report_effective("a-reserve", gen, {"other": 2})
        self.assertEqual(receipt.event, EventType.EFFECTIVE.value)

    def test_unrelated_resource_change_does_not_touch_chain(self) -> None:
        self.coordinator.submit_chain("chain-a", build_chain_a())
        before = len(self.coordinator.receipts)
        self.coordinator.resource_changed("totally-unrelated", 99)
        new_events = [r.event for r in self.coordinator.receipts[before:]]
        self.assertEqual(new_events, [EventType.WORLD_CHANGED.value])
        self.assertIs(
            self.coordinator.actions["a-reserve"].state, ActionState.DISPATCHED
        )

    def test_evidence_cleared_mid_flight_settles_in_flight_then_rejects_successors(self) -> None:
        self.coordinator.submit_chain("chain-a", build_chain_a())
        action = self.coordinator.actions["a-reserve"]
        gen = action.generation
        # 告警在执行中途解除：在途动作不撤销，继续接收结果
        self.coordinator.evidence_changed(ALERT_A, active=False)
        self.assertIs(action.state, ActionState.DISPATCHED)
        self.coordinator.report_phase("a-reserve", gen, "bound")
        self.coordinator.report_effective("a-reserve", gen, {HOT_STANDBY: 2})
        self.coordinator.confirm("a-reserve", gen)
        self.assertIs(action.state, ActionState.CONFIRMED)
        # 在途动作结算后，后续动作按最新事实重新核对 -> 证据已解除，拒绝并重排
        self.assertIs(
            self.coordinator.actions["a-rebuild"].state, ActionState.REJECTED
        )
        self.assertIs(
            self.coordinator.actions["a-release"].state, ActionState.REJECTED
        )
        self.assertIs(self.coordinator.chains["chain-a"].status, ChainStatus.ABORTED)

    def test_stale_generation_result_is_ignored(self) -> None:
        self.coordinator.submit_chain("chain-a", build_chain_a())
        action = self.coordinator.actions["a-reserve"]
        current_gen = action.generation
        stale_receipt = self.coordinator.report_effective(
            "a-reserve", current_gen - 1
        )
        self.assertEqual(stale_receipt.event, EventType.STALE_RESULT.value)
        self.assertEqual(
            stale_receipt.details["received_generation"], current_gen - 1
        )
        self.assertEqual(
            stale_receipt.details["current_generation"], current_gen
        )
        # 状态与世界版本都没有被过期回执改变
        self.assertIs(action.state, ActionState.DISPATCHED)
        self.assertEqual(self.coordinator.world.resource_version(HOT_STANDBY), 1)

        stale_phase = self.coordinator.report_phase(
            "a-reserve", current_gen - 1, "bound"
        )
        self.assertEqual(stale_phase.event, EventType.STALE_RESULT.value)
        self.assertIs(action.state, ActionState.DISPATCHED)

        stale_confirm = self.coordinator.confirm("a-reserve", current_gen - 1)
        self.assertEqual(stale_confirm.event, EventType.STALE_RESULT.value)

    def test_irrevocable_action_rejects_cancel_but_settles_result(self) -> None:
        self.coordinator.submit_chain("chain-a", build_chain_a())
        action = self.coordinator.actions["a-reserve"]
        gen = action.generation
        self.coordinator.report_phase("a-reserve", gen, "bound")
        self.assertIs(action.state, ActionState.IRREVOCABLE)
        reject = self.coordinator.cancel("a-reserve")
        self.assertEqual(reject.event, EventType.CANCEL_REJECTED.value)
        self.assertIs(action.state, ActionState.IRREVOCABLE)
        # 结果照常结算
        self.coordinator.report_effective("a-reserve", gen, {HOT_STANDBY: 2})
        self.assertIs(action.state, ActionState.EFFECTIVE)
        self.coordinator.confirm("a-reserve", gen)
        self.assertIs(action.state, ActionState.CONFIRMED)

    def test_cancel_in_revocable_window_succeeds(self) -> None:
        spec = ActionSpec(
            action_id="x",
            chain_id="c",
            step=0,
            reads=(HOT_STANDBY,),
            writes=(),
            preconditions=(ResourceVersion(HOT_STANDBY, 1),),
            phases=("prepare", "commit"),
            irrevocable_phase="commit",
        )
        coordinator, executor = build_coordinator(single_action_world())
        coordinator.submit_chain("c", [spec])
        self.assertIs(coordinator.actions["x"].state, ActionState.DISPATCHED)
        receipt = coordinator.cancel("x")
        self.assertEqual(receipt.event, EventType.CANCEL_REQUESTED.value)
        self.assertIs(coordinator.actions["x"].state, ActionState.CANCELLING)
        self.assertEqual(executor.cancel_requests, [("x", receipt.generation)])
        # 执行器确认取消
        cancelled = coordinator.report_cancelled("x", receipt.generation)
        self.assertEqual(cancelled.event, EventType.CANCELLED.value)
        self.assertIs(coordinator.actions["x"].state, ActionState.CANCELLED)
        self.assertIs(coordinator.chains["c"].status, ChainStatus.ABORTED)

    def test_effective_report_after_cancel_request_wins(self) -> None:
        spec = ActionSpec(
            action_id="x",
            chain_id="c",
            step=0,
            reads=(),
            writes=(HOT_STANDBY,),
            preconditions=(ResourceVersion(HOT_STANDBY, 1),),
            phases=("prepare",),
        )
        coordinator, _ = build_coordinator(single_action_world())
        coordinator.submit_chain("c", [spec])
        gen = coordinator.actions["x"].generation
        coordinator.cancel("x")
        # 执行器来不及取消，动作已经生效
        receipt = coordinator.report_effective("x", gen, {HOT_STANDBY: 2})
        self.assertEqual(receipt.event, EventType.EFFECTIVE.value)
        self.assertTrue(receipt.details["despite_cancel_request"])
        self.assertIs(coordinator.actions["x"].state, ActionState.EFFECTIVE)
        # 此时再取消也只能被拒绝
        again = coordinator.cancel("x")
        self.assertEqual(again.event, EventType.CANCEL_REJECTED.value)

    def test_phase_must_advance_and_match_known_phases(self) -> None:
        self.coordinator.submit_chain("chain-a", build_chain_a())
        gen = self.coordinator.actions["a-reserve"].generation
        with self.assertRaises(ValueError):
            self.coordinator.report_phase("a-reserve", gen, "no-such-phase")
        self.coordinator.report_phase("a-reserve", gen, "bound")
        with self.assertRaises(ValueError):
            self.coordinator.report_phase("a-reserve", gen, "reserve")

    def test_version_update_must_be_monotonic(self) -> None:
        self.coordinator.submit_chain("chain-a", build_chain_a())
        gen = self.coordinator.actions["a-reserve"].generation
        with self.assertRaises(ValueError):
            self.coordinator.report_effective("a-reserve", gen, {HOT_STANDBY: 1})
        self.assertIs(
            self.coordinator.actions["a-reserve"].state, ActionState.DISPATCHED
        )

    def test_failure_aborts_chain_with_receipt(self) -> None:
        self.coordinator.submit_chain("chain-a", build_chain_a())
        gen = self.coordinator.actions["a-reserve"].generation
        receipt = self.coordinator.report_failed("a-reserve", gen, "备机无响应")
        self.assertEqual(receipt.event, EventType.FAILED.value)
        self.assertEqual(receipt.details["reason"], "备机无响应")
        self.assertIs(
            self.coordinator.actions["a-reserve"].state, ActionState.FAILED
        )
        self.assertIs(self.coordinator.chains["chain-a"].status, ChainStatus.ABORTED)
        for action_id in ("a-rebuild", "a-release"):
            self.assertIs(
                self.coordinator.actions[action_id].state, ActionState.REJECTED
            )


if __name__ == "__main__":
    unittest.main()
