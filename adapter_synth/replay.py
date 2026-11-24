"""Replay and exhaustive verification support.

These utilities turn the synthesized artefacts into concrete runs against
real game semantics (never precomputed outputs):

* :func:`simulate_policy` walks *every* admissible environment choice
  sequence against one synthesized policy and reports success/failure
  traces and the worst observed round count.
* :func:`enumerate_counter_plays` expands a counter-strategy tree into the
  full list of replayable root-to-leaf plays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .counter import CounterLeaf, CounterNode
from .game import (
    GameState,
    PASS,
    admissible_env_choices,
    apply_env,
    play_round,
)
from .model import BufferConfig, ProtocolPair
from .serialize import describe_adapter_action, describe_env_action, state_to_dict
from .synthesize import AdapterStrategy


@dataclass
class SimulationReport:
    """Aggregate result of exhaustively replaying one policy."""

    all_succeed: bool
    total_plays: int
    success_plays: int
    failure_plays: List[dict] = field(default_factory=list)
    worst_rounds: int = 0
    best_rounds: Optional[int] = None
    traces: List[dict] = field(default_factory=list)


def simulate_policy(
    proto: ProtocolPair,
    config: BufferConfig,
    strategy: AdapterStrategy,
    round_budget: int,
    collect_traces: bool = True,
) -> SimulationReport:
    """Replay the policy against every environment choice sequence.

    Every trace uses :func:`play_round`, so buffer legality, forced handoffs
    and the no-change-round failure rule are enforced on each step.
    """

    start = GameState.initial(proto)
    successes = 0
    failures: List[dict] = []
    traces: List[dict] = []
    worst = 0
    best: Optional[int] = None

    def walk(state: GameState, rounds: int, trace: List[dict], on_path: set) -> None:
        nonlocal successes, worst, best

        if state.is_goal(proto):
            successes += 1
            worst = max(worst, rounds)
            best = rounds if best is None else min(best, rounds)
            if collect_traces:
                traces.append(
                    {"outcome": "goal", "rounds": rounds, "steps": list(trace)}
                )
            return

        if rounds >= round_budget:
            failures.append(
                {
                    "outcome": "round_budget",
                    "rounds": rounds,
                    "state": state_to_dict(state),
                    "steps": list(trace),
                }
            )
            return

        if state in on_path:
            failures.append(
                {
                    "outcome": "repeated_state",
                    "rounds": rounds,
                    "state": state_to_dict(state),
                    "steps": list(trace),
                }
            )
            return

        on_path.add(state)
        choices = admissible_env_choices(proto, config, state)
        for choice in choices:
            if choice is PASS:
                mid = state
                adapter_action = strategy.choose(mid)
            else:
                mid = apply_env(state, choice)
                if mid.is_goal(proto):
                    # The environment move alone reaches the goal; the
                    # adapter's vacuous wait terminates the round.
                    trace.append(
                        {
                            "round": rounds + 1,
                            "env_action": describe_env_action(choice),
                            "adapter_action": "wait(terminal)",
                            "end_state": state_to_dict(mid),
                        }
                    )
                    walk(mid, rounds + 1, trace, on_path)
                    trace.pop()
                    continue
                adapter_action = strategy.choose(mid)

            outcome = play_round(
                proto, config, state, choice, adapter_action
            )
            step = {
                "round": rounds + 1,
                "env_action": describe_env_action(choice),
                "adapter_action": describe_adapter_action(adapter_action),
                "end_state": state_to_dict(outcome.end_state),
            }
            trace.append(step)
            if outcome.deadlock:
                failures.append(
                    {
                        "outcome": "deadlock",
                        "rounds": rounds + 1,
                        "state": state_to_dict(outcome.end_state),
                        "steps": list(trace),
                    }
                )
            else:
                walk(outcome.end_state, rounds + 1, trace, on_path)
            trace.pop()
        on_path.discard(state)

    walk(start, 0, [], set())
    total = successes + len(failures)
    return SimulationReport(
        all_succeed=not failures,
        total_plays=total,
        success_plays=successes,
        failure_plays=failures,
        worst_rounds=worst,
        best_rounds=best,
        traces=traces,
    )


def enumerate_counter_plays(root: object) -> List[dict]:
    """Expand a counter-strategy tree into replayable root-to-leaf plays."""

    plays: List[dict] = []

    def walk(node: object, steps: List[dict]) -> None:
        if isinstance(node, CounterLeaf):
            plays.append(
                {
                    "leaf_reason": node.reason,
                    "leaf_state": state_to_dict(node.state),
                    "rounds": len(steps),
                    "steps": list(steps),
                }
            )
            return
        assert isinstance(node, CounterNode)
        for adapter_label, child in node.branches:
            steps.append(
                {
                    "state": state_to_dict(node.state),
                    "env_action": describe_env_action(node.env_action),
                    "adapter_action": adapter_label,
                }
            )
            walk(child, steps)
            steps.pop()

    walk(root, [])
    return plays
