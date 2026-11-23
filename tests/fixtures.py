"""测试共享夹具：两条修复链共用热备的标准世界与动作规格。"""

from __future__ import annotations

from remediation_coordinator import (
    ActionSpec,
    AllOf,
    Evidence,
    EvidenceActive,
    ResourceVersion,
    ScriptedExecutor,
    WorldState,
    RemediationCoordinator,
)

HOT_STANDBY = "hot-standby"
CLUSTER_A = "cluster-a"
CLUSTER_B = "cluster-b"
ALERT_A = "alert-a"
ALERT_B = "alert-b"

PHASES_SIMPLE = ("prepare", "commit")


def build_world() -> WorldState:
    return WorldState(
        resources={HOT_STANDBY: 1, CLUSTER_A: 1, CLUSTER_B: 1},
        evidence={
            ALERT_A: Evidence(
                evidence_id=ALERT_A,
                active=True,
                observed_versions={HOT_STANDBY: 1, CLUSTER_A: 1},
            ),
            ALERT_B: Evidence(
                evidence_id=ALERT_B,
                active=True,
                observed_versions={HOT_STANDBY: 1, CLUSTER_B: 1},
            ),
        },
    )


def build_chain_a() -> list[ActionSpec]:
    return [
        ActionSpec(
            action_id="a-reserve",
            chain_id="chain-a",
            step=0,
            reads=(),
            writes=(HOT_STANDBY,),
            preconditions=(
                ResourceVersion(HOT_STANDBY, 1),
                EvidenceActive(ALERT_A),
            ),
            phases=("reserve", "bound"),
            irrevocable_phase="bound",
        ),
        ActionSpec(
            action_id="a-rebuild",
            chain_id="chain-a",
            step=1,
            reads=(HOT_STANDBY,),
            writes=(CLUSTER_A,),
            preconditions=(
                ResourceVersion(CLUSTER_A, 1),
                EvidenceActive(ALERT_A),
                AllOf([ResourceVersion(HOT_STANDBY, 2)]),
            ),
            phases=PHASES_SIMPLE,
            irrevocable_phase="commit",
        ),
        ActionSpec(
            action_id="a-release",
            chain_id="chain-a",
            step=2,
            reads=(HOT_STANDBY,),
            writes=(HOT_STANDBY,),
            preconditions=(
                ResourceVersion(HOT_STANDBY, 2),
                EvidenceActive(ALERT_A),
            ),
            phases=("release",),
        ),
    ]


def build_chain_b() -> list[ActionSpec]:
    return [
        ActionSpec(
            action_id="b-reserve",
            chain_id="chain-b",
            step=0,
            reads=(),
            writes=(HOT_STANDBY,),
            preconditions=(
                ResourceVersion(HOT_STANDBY, 1),
                EvidenceActive(ALERT_B),
            ),
            phases=("reserve", "bound"),
            irrevocable_phase="bound",
        ),
        ActionSpec(
            action_id="b-rebuild",
            chain_id="chain-b",
            step=1,
            reads=(HOT_STANDBY,),
            writes=(CLUSTER_B,),
            preconditions=(
                ResourceVersion(CLUSTER_B, 1),
                EvidenceActive(ALERT_B),
                ResourceVersion(HOT_STANDBY, 2),
            ),
            phases=PHASES_SIMPLE,
            irrevocable_phase="commit",
        ),
    ]


def build_coordinator(
    world: WorldState | None = None,
) -> tuple[RemediationCoordinator, ScriptedExecutor]:
    executor = ScriptedExecutor()
    coordinator = RemediationCoordinator(world or build_world(), executor=executor)
    return coordinator, executor
