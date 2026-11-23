"""重启恢复测试。

协调器在任意时刻快照落盘后，新进程用快照恢复，必须能区分：
  * 待开始（planned / scheduled）；
  * 已生效但未确认（effective）；
  * 尚可取消（dispatched / cancelling）；
  * 另附已过不可撤销点的在途动作桶（只能等结果）。

恢复后旧代次回执仍按代次规则处理，新执行器派发的代次继续单调递增。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from remediation_coordinator import (
    ActionSpec,
    ActionState,
    ChainStatus,
    EventType,
    RemediationCoordinator,
    ResourceVersion,
    ScriptedExecutor,
    WorldState,
    load_snapshot,
    save_snapshot,
)


def _one_shot(action_id: str, chain_id: str, resource_id: str, version: int = 1) -> ActionSpec:
    return ActionSpec(
        action_id=action_id,
        chain_id=chain_id,
        step=0,
        reads=(),
        writes=(resource_id,),
        preconditions=(ResourceVersion(resource_id, version),),
        phases=("prepare", "commit"),
        irrevocable_phase="commit",
    )


def _mixed_world() -> WorldState:
    return WorldState(
        resources={
            "r-effective": 1,
            "r-cancel": 1,
            "r-pending": 1,
            "r-irrev": 1,
            "r-fresh": 1,
        }
    )


class RestartRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.executor = ScriptedExecutor()
        self.coordinator = RemediationCoordinator(_mixed_world(), executor=self.executor)
        # 三条互不争用资源的独立链，分别停在不同在途状态；
        # "待开始"样本由 _drive_pending_to_planned 用被冲突挡住的链单独构造
        self.coordinator.submit_chain(
            "chain-effective", [_one_shot("act-effective", "chain-effective", "r-effective")]
        )
        self.coordinator.submit_chain(
            "chain-cancel", [_one_shot("act-cancel", "chain-cancel", "r-cancel")]
        )
        self.coordinator.submit_chain(
            "chain-irrev", [_one_shot("act-irrev", "chain-irrev", "r-irrev")]
        )

        self.gen_effective = self.coordinator.actions["act-effective"].generation
        self.gen_cancel = self.coordinator.actions["act-cancel"].generation
        self.gen_irrev = self.coordinator.actions["act-irrev"].generation

        # 生效未确认
        self.coordinator.report_effective(
            "act-effective", self.gen_effective, {"r-effective": 2}
        )
        # 尚可取消：停在 dispatched（尚未进入 commit 不可撤销点）
        self.assertEqual(
            self.coordinator.actions["act-cancel"].state, ActionState.DISPATCHED
        )
        # 不可撤销在途
        self.coordinator.report_phase("act-irrev", self.gen_irrev, "commit")

    def _drive_pending_to_planned(self) -> None:
        """构造一个被冲突挡住的 planned 头部作为"待开始"样本。"""
        # r-pending 目前无人占用；先放一个占位者，再让 waiter 争用同一资源
        self.coordinator.submit_chain(
            "chain-holder",
            [_one_shot("act-holder", "chain-holder", "r-pending")],
        )
        self.coordinator.submit_chain(
            "chain-waiter",
            [_one_shot("act-waiter", "chain-waiter", "r-pending")],
        )
        self.assertIs(
            self.coordinator.actions["act-waiter"].state, ActionState.PLANNED
        )

    def test_recovery_buckets_classify_in_flight_actions(self) -> None:
        self._drive_pending_to_planned()
        buckets = self.coordinator.recovery_buckets()
        # waiter 被冲突挡住，停在 planned（待开始）；holder 已派发（尚可取消）
        self.assertEqual(buckets.pending_start, ("act-waiter",))
        self.assertEqual(buckets.effective_unconfirmed, ("act-effective",))
        self.assertEqual(
            buckets.cancellable, ("act-cancel", "act-holder")
        )
        self.assertEqual(buckets.irrevocable_in_flight, ("act-irrev",))

    def test_scheduled_action_also_belongs_to_pending_bucket(self) -> None:
        # 无执行器引擎的动作停在 SCHEDULED，同样属于"待开始"
        planner = RemediationCoordinator(
            WorldState(resources={"r": 1}), executor=None
        )
        planner.submit_chain("c", [_one_shot("a", "c", "r")])
        self.assertIs(planner.actions["a"].state, ActionState.SCHEDULED)
        self.assertEqual(planner.recovery_buckets().pending_start, ("a",))

    def test_snapshot_roundtrip_preserves_state_and_generations(self) -> None:
        self._drive_pending_to_planned()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            save_snapshot(self.coordinator, path)
            new_executor = ScriptedExecutor()
            restored = load_snapshot(path, executor=new_executor)

        expected = self.coordinator.recovery_buckets()
        self.assertEqual(restored.recovery_buckets(), expected)

        self.assertIs(
            restored.actions["act-effective"].state, ActionState.EFFECTIVE
        )
        self.assertEqual(
            restored.actions["act-effective"].generation, self.gen_effective
        )
        self.assertIs(restored.actions["act-cancel"].state, ActionState.DISPATCHED)
        self.assertEqual(
            restored.actions["act-cancel"].generation, self.gen_cancel
        )
        self.assertIs(restored.actions["act-irrev"].state, ActionState.IRREVOCABLE)
        self.assertEqual(restored.world.resource_version("r-effective"), 2)

        recovered = [
            r for r in restored.receipts if r.event == EventType.RECOVERED.value
        ]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].details["buckets"], expected.to_dict())

        pre_recovery = [
            r for r in restored.receipts if r.event != EventType.RECOVERED.value
        ]
        self.assertEqual(
            [r.to_dict() for r in pre_recovery],
            [r.to_dict() for r in self.coordinator.receipts],
        )

    def test_stale_generation_after_restart_is_rejected(self) -> None:
        snapshot = self.coordinator.to_dict()
        restored = RemediationCoordinator.restore(snapshot, executor=ScriptedExecutor())
        # 重启前的旧代次回执（模拟执行器重放）必须被当作过期结果
        stale = restored.confirm("act-effective", self.gen_effective - 1)
        self.assertEqual(stale.event, EventType.STALE_RESULT.value)
        self.assertIs(
            restored.actions["act-effective"].state, ActionState.EFFECTIVE
        )
        # 当前代次的确认仍然有效
        ok = restored.confirm("act-effective", self.gen_effective)
        self.assertEqual(ok.event, EventType.CONFIRMED.value)
        self.assertIs(
            restored.actions["act-effective"].state, ActionState.CONFIRMED
        )

    def test_cancelling_action_restored_in_cancellable_bucket(self) -> None:
        self.coordinator.cancel("act-cancel")
        self.assertIs(
            self.coordinator.actions["act-cancel"].state, ActionState.CANCELLING
        )
        restored = RemediationCoordinator.restore(
            self.coordinator.to_dict(), executor=ScriptedExecutor()
        )
        self.assertIn("act-cancel", restored.recovery_buckets().cancellable)
        # 恢复后执行器的取消回执仍按原代次结算
        receipt = restored.report_cancelled("act-cancel", self.gen_cancel)
        self.assertEqual(receipt.event, EventType.CANCELLED.value)
        self.assertIs(
            restored.actions["act-cancel"].state, ActionState.CANCELLED
        )

    def test_post_restart_dispatch_generation_continues_monotonic(self) -> None:
        max_gen_before = max(
            action.generation for action in self.coordinator.actions.values()
        )
        snapshot = self.coordinator.to_dict()
        new_executor = ScriptedExecutor()
        restored = RemediationCoordinator.restore(snapshot, executor=new_executor)

        # 不可撤销在途动作：重启后继续等结果，收到当前代次结果后正常结算
        restored.report_effective("act-irrev", self.gen_irrev, {"r-irrev": 2})
        self.assertIs(
            restored.actions["act-irrev"].state, ActionState.EFFECTIVE
        )
        restored.confirm("act-irrev", self.gen_irrev)
        self.assertIs(
            restored.actions["act-irrev"].state, ActionState.CONFIRMED
        )
        self.assertIs(
            restored.chains["chain-irrev"].status, ChainStatus.COMPLETED
        )

        # 新链派发的代次必须大于重启前的最大代次
        restored.submit_chain(
            "fresh-chain", [_one_shot("fresh-act", "fresh-chain", "r-fresh")]
        )
        self.assertEqual(len(new_executor.dispatches), 1)
        self.assertGreater(
            new_executor.dispatches[0].generation, max_gen_before
        )

    def test_recovery_then_blocked_head_revalidated_against_latest_facts(self) -> None:
        # 待开始动作恢复后，外部推进它钉住的资源版本 -> 重新核对即拒绝
        self._drive_pending_to_planned()
        restored = RemediationCoordinator.restore(
            self.coordinator.to_dict(), executor=ScriptedExecutor()
        )
        self.assertIs(
            restored.actions["act-waiter"].state, ActionState.PLANNED
        )
        restored.resource_changed("r-pending", 9)
        self.assertIs(
            restored.actions["act-waiter"].state, ActionState.REJECTED
        )
        self.assertIs(
            restored.chains["chain-waiter"].status, ChainStatus.ABORTED
        )


if __name__ == "__main__":
    unittest.main()
