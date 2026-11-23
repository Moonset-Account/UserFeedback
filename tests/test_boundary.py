"""边界条件与输入校验测试。"""

from __future__ import annotations

import unittest

from remediation_coordinator import (
    ActionSpec,
    ActionState,
    AllOf,
    ChainStatus,
    Evidence,
    EvidenceActive,
    EvidenceCleared,
    ResourceVersion,
    WorldState,
)
from remediation_coordinator.model import (
    Action,
    Precondition,
)


class BoundaryValidationTest(unittest.TestCase):
    def test_world_version_must_be_monotonic(self) -> None:
        world = WorldState(resources={"r": 3})
        with self.assertRaises(ValueError):
            world.update_resource("r", 3)
        with self.assertRaises(ValueError):
            world.update_resource("r", 2)
        world.update_resource("r", 4)
        self.assertEqual(world.resource_version("r"), 4)
        self.assertIsNone(world.resource_version("missing"))

    def test_action_spec_validation(self) -> None:
        common = dict(
            action_id="a",
            chain_id="c",
            step=0,
            reads=(),
            writes=("r",),
            preconditions=(),
        )
        with self.assertRaises(ValueError):
            ActionSpec(**common, phases=())
        with self.assertRaises(ValueError):
            ActionSpec(**common, phases=("p", "p"))
        with self.assertRaises(ValueError):
            ActionSpec(**common, phases=("p",), irrevocable_phase="nope")
        ActionSpec(**common, phases=("p",), irrevocable_phase=None)

    def test_submit_chain_validates_steps_and_ownership(self) -> None:
        from remediation_coordinator import RemediationCoordinator

        coordinator = RemediationCoordinator(WorldState(resources={"r": 1}))
        good = ActionSpec(
            action_id="a0", chain_id="c", step=0,
            reads=(), writes=("r",), preconditions=(), phases=("p",),
        )
        bad_step = ActionSpec(
            action_id="a1", chain_id="c", step=2,
            reads=(), writes=("r",), preconditions=(), phases=("p",),
        )
        with self.assertRaises(ValueError):
            coordinator.submit_chain("c", [good, bad_step])
        other_owner = ActionSpec(
            action_id="a2", chain_id="other", step=0,
            reads=(), writes=("r",), preconditions=(), phases=("p",),
        )
        with self.assertRaises(ValueError):
            coordinator.submit_chain("c", [other_owner])
        with self.assertRaises(ValueError):
            coordinator.submit_chain("c", [])
        coordinator.submit_chain("c", [good])
        with self.assertRaises(ValueError):
            coordinator.submit_chain("c", [good])

    def test_unknown_action_and_illegal_state_reports(self) -> None:
        from tests.fixtures import build_chain_a, build_coordinator

        coordinator, _ = build_coordinator()
        coordinator.submit_chain("chain-a", build_chain_a())
        with self.assertRaises(KeyError):
            coordinator.confirm("nope", 1)
        gen = coordinator.actions["a-reserve"].generation
        # 尚未生效的动作不能确认
        with self.assertRaises(ValueError):
            coordinator.confirm("a-reserve", gen)
        # 未知资源版本更新同样拒绝
        with self.assertRaises(ValueError):
            coordinator.report_effective("a-reserve", gen, {"hot-standby": 1})

    def test_allof_and_evidence_precondition_predicates(self) -> None:
        world = WorldState(
            resources={"r": 1},
            evidence={
                "e-on": Evidence("e-on", active=True),
                "e-off": Evidence("e-off", active=False),
            },
        )
        self.assertTrue(ResourceVersion("r", 1).evaluate(world))
        self.assertFalse(ResourceVersion("r", 2).evaluate(world))
        self.assertFalse(ResourceVersion("missing", 1).evaluate(world))
        self.assertTrue(EvidenceActive("e-on").evaluate(world))
        self.assertFalse(EvidenceActive("e-off").evaluate(world))
        self.assertFalse(EvidenceActive("missing").evaluate(world))
        self.assertTrue(EvidenceCleared("e-off").evaluate(world))
        self.assertFalse(EvidenceCleared("e-on").evaluate(world))

        allof = AllOf([ResourceVersion("r", 1), EvidenceActive("e-on")])
        self.assertTrue(allof.evaluate(world))
        self.assertEqual(
            allof.references(),
            frozenset({("resource", "r"), ("evidence", "e-on")}),
        )
        # 序列化往返
        restored = Precondition.from_dict(allof.to_dict())
        self.assertEqual(restored, allof)

    def test_model_serialization_roundtrip(self) -> None:
        from tests.fixtures import build_chain_a

        spec = build_chain_a()[1]
        action = Action(spec=spec, state=ActionState.SCHEDULED, generation=7)
        restored = Action.from_dict(action.to_dict())
        self.assertEqual(restored.spec, spec)
        self.assertEqual(restored.state, ActionState.SCHEDULED)
        self.assertEqual(restored.generation, 7)

    def test_scheduled_cancel_aborts_chain_without_executor(self) -> None:
        from remediation_coordinator import RemediationCoordinator
        from tests.fixtures import build_chain_a, build_world

        coordinator = RemediationCoordinator(build_world(), executor=None)
        coordinator.submit_chain("chain-a", build_chain_a())
        head = coordinator.actions["a-reserve"]
        self.assertIs(head.state, ActionState.SCHEDULED)
        receipt = coordinator.cancel("a-reserve")
        self.assertEqual(receipt.event, "cancelled")
        self.assertIs(head.state, ActionState.CANCELLED)
        self.assertIs(coordinator.chains["chain-a"].status, ChainStatus.ABORTED)

    def test_repeat_cancel_request_on_cancelling_is_deduped(self) -> None:
        from tests.fixtures import build_chain_a, build_coordinator

        coordinator, executor = build_coordinator()
        coordinator.submit_chain("chain-a", build_chain_a())
        first = coordinator.cancel("a-reserve")
        second = coordinator.cancel("a-reserve")
        self.assertEqual(first.event, "cancel_requested")
        self.assertEqual(second.event, "cancel_rejected")
        # 执行器只收到一次取消请求
        self.assertEqual(len(executor.cancel_requests), 1)

    def test_terminal_action_cancel_is_rejected_with_receipt(self) -> None:
        from tests.fixtures import build_chain_a, build_coordinator

        coordinator, _ = build_coordinator()
        coordinator.submit_chain("chain-a", build_chain_a())
        gen = coordinator.actions["a-reserve"].generation
        coordinator.report_effective("a-reserve", gen, {"hot-standby": 2})
        coordinator.confirm("a-reserve", gen)
        receipt = coordinator.cancel("a-reserve")
        self.assertEqual(receipt.event, "cancel_rejected")
        self.assertIs(
            coordinator.actions["a-reserve"].state, ActionState.CONFIRMED
        )

    def test_receipt_helpers_and_snapshot_format_guard(self) -> None:
        from tests.fixtures import build_chain_a, build_coordinator

        coordinator, _ = build_coordinator()
        coordinator.submit_chain("chain-a", build_chain_a())
        action_receipts = coordinator.receipts_for("a-reserve")
        self.assertTrue(action_receipts)
        chain_receipts = coordinator.receipts_for_chain("chain-a")
        self.assertGreaterEqual(len(chain_receipts), len(action_receipts))
        # 回执序号与逻辑时钟严格递增
        seqs = [r.seq for r in coordinator.receipts]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        bad_snapshot = dict(coordinator.to_dict(), format=999)
        with self.assertRaises(ValueError):
            type(coordinator).restore(bad_snapshot)


if __name__ == "__main__":
    unittest.main()
