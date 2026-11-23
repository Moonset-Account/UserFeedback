"""全链路冲突测试。

构造一个 X-Y-Z 三链世界：
  * 链 X 与链 Y 直接争用资源 r-xy；
  * 链 Y 与链 Z 直接争用资源 r-yz；
  * 链 X 与链 Z 没有任何共同资源。

当 X、Y 都在占位时，Z 虽与 X 无直接交集，但经由 Y 的全链路依赖形成
间接冲突，必须被拒绝占位；Y 中止后，X 与 Z 才允许同时占位。
"""

from __future__ import annotations

import unittest

from remediation_coordinator import (
    ActionSpec,
    ActionState,
    ResourceVersion,
    WorldState,
)


def spec(action_id: str, chain_id: str, writes: tuple[str, ...], reads: tuple[str, ...] = ()) -> ActionSpec:
    preconditions = tuple(
        ResourceVersion(resource_id, 1) for resource_id in set(writes) | set(reads)
    )
    return ActionSpec(
        action_id=action_id,
        chain_id=chain_id,
        step=0,
        reads=reads,
        writes=writes,
        preconditions=preconditions,
        phases=("prepare", "commit"),
        irrevocable_phase="commit",
    )


class FullChainConflictTest(unittest.TestCase):
    def _world(self) -> WorldState:
        return WorldState(
            resources={f"r-xy": 1, f"r-yz": 1, f"r-x": 1, f"r-z": 1},
            evidence={},
        )

    def test_indirect_conflict_via_bridge_chain_is_blocked(self) -> None:
        from remediation_coordinator import RemediationCoordinator
        from remediation_coordinator.scripted_executor import ScriptedExecutor

        coordinator = RemediationCoordinator(self._world(), executor=ScriptedExecutor())

        coordinator.submit_chain(
            "chain-x", [spec("x1", "chain-x", writes=("r-xy",))]
        )
        coordinator.submit_chain(
            "chain-y", [spec("y1", "chain-y", writes=("r-xy", "r-yz"))]
        )
        coordinator.submit_chain(
            "chain-z",
            [
                ActionSpec(
                    action_id="z1",
                    chain_id="chain-z",
                    step=0,
                    reads=(),
                    writes=("r-yz", "r-z"),
                    preconditions=(ResourceVersion("r-yz", 1),),
                    phases=("prepare", "commit"),
                    irrevocable_phase="commit",
                )
            ],
        )

        # X 与 Y 直接争用 r-xy（Y 被挡在 planned）；Z 与 X 无共同资源，
        # 但 Z 与 Y 争用 r-yz，而 Y 又与 X 同属一个全链路分量 -> Z 被间接挡住
        self.assertIs(coordinator.actions["x1"].state, ActionState.DISPATCHED)
        self.assertIs(coordinator.actions["y1"].state, ActionState.PLANNED)
        self.assertIs(coordinator.actions["z1"].state, ActionState.PLANNED)

        blocked = coordinator.receipts_for("z1")[-1]
        self.assertEqual(blocked.event, "blocked")
        # 已占位的只有 X，而 z1 与 X 没有任何资源交集，因此是纯间接冲突；
        # 与 Y 的 r-yz 交集发生在未占位动作之间，只用于连通分量计算
        self.assertTrue(blocked.details["indirect"])
        self.assertEqual(blocked.details["chains"], ["chain-x", "chain-y"])
        # 三个链的非终态动作同属一个全链路冲突分量
        self.assertEqual(
            coordinator.conflict_components(),
            [["chain-x", "chain-y", "chain-z"]],
        )

    def test_bridge_chain_removed_breaks_indirect_component(self) -> None:
        from remediation_coordinator import RemediationCoordinator
        from remediation_coordinator.scripted_executor import ScriptedExecutor

        coordinator = RemediationCoordinator(self._world(), executor=ScriptedExecutor())
        coordinator.submit_chain(
            "chain-x", [spec("x1", "chain-x", writes=("r-xy",))]
        )
        coordinator.submit_chain(
            "chain-y", [spec("y1", "chain-y", writes=("r-xy", "r-yz"))]
        )
        coordinator.submit_chain(
            "chain-z", [spec("z1", "chain-z", writes=("r-yz", "r-z"))]
        )
        # 桥接链 Y 从未占位，取消其待开始头部后链 Y 退出
        coordinator.cancel("y1")
        self.assertIs(coordinator.actions["y1"].state, ActionState.CANCELLED)
        # X 与 Z 之间没有任何资源交集，分量拆开，Z 允许占位
        self.assertIs(coordinator.actions["x1"].state, ActionState.DISPATCHED)
        self.assertIs(coordinator.actions["z1"].state, ActionState.DISPATCHED)
        self.assertEqual(
            coordinator.conflict_components(), [["chain-x"], ["chain-z"]]
        )

    def test_direct_read_write_and_write_write_conflicts(self) -> None:
        from remediation_coordinator import RemediationCoordinator
        from remediation_coordinator.scripted_executor import ScriptedExecutor

        world = WorldState(resources={"r": 1}, evidence={})
        coordinator = RemediationCoordinator(world, executor=ScriptedExecutor())
        coordinator.submit_chain(
            "writer", [spec("w1", "writer", writes=("r",))]
        )
        coordinator.submit_chain(
            "reader", [spec("rd1", "reader", writes=(), reads=("r",))]
        )
        self.assertIs(coordinator.actions["w1"].state, ActionState.DISPATCHED)
        self.assertIs(coordinator.actions["rd1"].state, ActionState.PLANNED)
        blocked = coordinator.receipts_for("rd1")[-1]
        self.assertEqual(blocked.details["resources"], ["r"])
        self.assertFalse(blocked.details["indirect"])

    def test_read_read_is_compatible(self) -> None:
        from remediation_coordinator import RemediationCoordinator
        from remediation_coordinator.scripted_executor import ScriptedExecutor

        world = WorldState(resources={"r": 1}, evidence={})
        coordinator = RemediationCoordinator(world, executor=ScriptedExecutor())
        coordinator.submit_chain(
            "reader-1", [spec("rd1", "reader-1", writes=(), reads=("r",))]
        )
        coordinator.submit_chain(
            "reader-2", [spec("rd2", "reader-2", writes=(), reads=("r",))]
        )
        self.assertIs(coordinator.actions["rd1"].state, ActionState.DISPATCHED)
        self.assertIs(coordinator.actions["rd2"].state, ActionState.DISPATCHED)

    def test_blocked_receipt_deduped_until_state_changes(self) -> None:
        from remediation_coordinator import RemediationCoordinator
        from remediation_coordinator.scripted_executor import ScriptedExecutor

        world = WorldState(resources={"r": 1}, evidence={})
        coordinator = RemediationCoordinator(world, executor=ScriptedExecutor())
        coordinator.submit_chain("writer", [spec("w1", "writer", writes=("r",))])
        coordinator.submit_chain("writer-2", [spec("w2", "writer-2", writes=("r",))])
        blocked_before = [
            r for r in coordinator.receipts_for("w2") if r.event == "blocked"
        ]
        coordinator.pump()
        coordinator.pump()
        blocked_after = [
            r for r in coordinator.receipts_for("w2") if r.event == "blocked"
        ]
        self.assertEqual(len(blocked_before), 1)
        self.assertEqual(len(blocked_after), 1)


if __name__ == "__main__":
    unittest.main()
