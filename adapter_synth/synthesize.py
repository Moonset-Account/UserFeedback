"""Strategy synthesis and minimum buffer capacity search."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .counter import build_counter_strategy, counter_branch_count
from .game import (
    INF,
    GameState,
    PASS,
    WAIT,
    admissible_env_choices,
    apply_adapter,
    apply_env,
    available_conversions,
    compute_ranks,
)
from .model import BufferConfig, ProtocolPair
from .serialize import describe_adapter_action, state_to_dict


@dataclass(frozen=True)
class AdapterStrategy:
    """Executable state-to-action policy plus a serialized action table."""

    config: BufferConfig
    rounds_required: int
    # Maps the state observed *after the environment move* to an adapter
    # action.  WAIT is stored explicitly.
    policy: Dict[GameState, object] = field(default_factory=dict)

    def choose(self, mid_state: GameState) -> object:
        """Return the adapter action prescribed for a post-environment state."""

        if mid_state not in self.policy:
            raise KeyError(
                f"no policy entry for state {state_to_dict(mid_state)}; "
                "the state must be reached through the documented game moves"
            )
        return self.policy[mid_state]

    def action_table(self) -> List[dict]:
        """Stable, JSON-friendly state-to-action table."""

        rows = []
        for state in sorted(self.policy, key=_state_sort_key):
            rows.append(
                {
                    "state": state_to_dict(state),
                    "adapter_action": describe_adapter_action(self.policy[state]),
                }
            )
        return rows


def _state_sort_key(state: GameState):
    return (
        state.state_a,
        state.state_b,
        state.pending_a2b,
        state.converted_a2b,
        state.pending_b2a,
        state.converted_b2a,
    )


@dataclass(frozen=True)
class SynthesisResult:
    """Outcome of one synthesis attempt for a fixed buffer configuration."""

    solvable: bool
    config: BufferConfig
    round_budget: int
    rounds_required: Optional[int]
    strategy: Optional[AdapterStrategy]
    counter_strategy: Optional[object]
    ranks: Dict[GameState, int]

    def counter_to_dict(self) -> Optional[dict]:
        if self.counter_strategy is None:
            return None
        return self.counter_strategy.to_dict()


def _build_strategy(
    proto: ProtocolPair,
    config: BufferConfig,
    ranks: Dict[GameState, int],
    rounds_required: int,
) -> AdapterStrategy:
    """Extract a deterministic memoryless winning policy from the ranks.

    For every reachable post-environment state that can be reached from a
    winning state, pick the legal adapter action minimizing the successor
    rank (ties broken by the stable action ordering).
    """

    policy: Dict[GameState, object] = {}
    for state, rank in ranks.items():
        if rank >= INF or rank == 0:
            continue
        choices = admissible_env_choices(proto, config, state)
        for choice in choices:
            if choice is PASS:
                mid = state
                options = available_conversions(proto, config, mid)
                # At a winning state a forced handoff always has an option.
            else:
                mid = apply_env(state, choice)
                if mid.is_goal(proto):
                    continue
                options = available_conversions(proto, config, mid) + [WAIT]
            if mid in policy:
                continue
            best = min(
                options,
                key=lambda opt: (
                    ranks[apply_adapter(mid, opt)],
                    describe_adapter_action(opt),
                ),
            )
            policy[mid] = best
    return AdapterStrategy(config, rounds_required, policy)


def synthesize(
    proto: ProtocolPair, config: BufferConfig, round_budget: int
) -> SynthesisResult:
    """Synthesize an adapter policy for one fixed buffer configuration.

    A winning policy must force both peers into final states with every
    buffer emptied within ``round_budget`` rounds, for *every* environment
    choice.  When no such policy exists, the result carries a replayable
    environment counter-strategy tree.
    """

    if not isinstance(round_budget, int) or isinstance(round_budget, bool):
        raise ValueError("round_budget must be an integer")
    if round_budget < 0:
        raise ValueError("round_budget must be non-negative")

    ranks = compute_ranks(proto, config)
    start = GameState.initial(proto)
    required = ranks[start]

    if required <= round_budget:
        strategy = _build_strategy(proto, config, ranks, required)
        return SynthesisResult(
            solvable=True,
            config=config,
            round_budget=round_budget,
            rounds_required=required,
            strategy=strategy,
            counter_strategy=None,
            ranks=ranks,
        )

    counter = build_counter_strategy(proto, config, ranks, start, round_budget)
    return SynthesisResult(
        solvable=False,
        config=config,
        round_budget=round_budget,
        rounds_required=None if required >= INF else required,
        strategy=None,
        counter_strategy=counter,
        ranks=ranks,
    )


def minimum_buffer_capacity(
    proto: ProtocolPair, round_budget: int
) -> dict:
    """Search all capacity vectors (each FIFO 0..2) for the minimum buffers.

    Configurations are examined in ascending total capacity, then by the
    canonical per-queue order, so the first solvable vector is the lexicographically
    smallest minimum.  Every minimum vector is reported.  When no vector works,
    the counter-strategy of the maximal (2,2,2,2) configuration is returned.
    """

    vectors: List[Tuple[int, int, int, int]] = [
        (p_a2b, c_a2b, p_b2a, c_b2a)
        for p_a2b in range(BufferConfig.MAX + 1)
        for c_a2b in range(BufferConfig.MAX + 1)
        for p_b2a in range(BufferConfig.MAX + 1)
        for c_b2a in range(BufferConfig.MAX + 1)
    ]
    vectors.sort(key=lambda v: (sum(v), v))

    winning: List[SynthesisResult] = []
    minimal_total: Optional[int] = None
    maximal = BufferConfig(
        BufferConfig.MAX, BufferConfig.MAX, BufferConfig.MAX, BufferConfig.MAX
    )
    maximal_result: Optional[SynthesisResult] = None

    for vector in vectors:
        config = BufferConfig(*vector)
        result = synthesize(proto, config, round_budget)
        if config == maximal:
            maximal_result = result
        if result.solvable:
            if minimal_total is None:
                minimal_total = sum(vector)
            if sum(vector) == minimal_total:
                winning.append(result)

    if minimal_total is None:
        return {
            "solvable": False,
            "minimal_total_capacity": None,
            "minimum_configs": [],
            "best_rounds_required": None,
            "counter_strategy": maximal_result.counter_to_dict(),
            "counter_config": maximal.as_tuple(),
            "counter_branch_count": counter_branch_count(
                maximal_result.counter_strategy
            ),
        }

    return {
        "solvable": True,
        "minimal_total_capacity": minimal_total,
        "minimum_configs": [
            {
                "capacities": r.config.as_tuple(),
                "rounds_required": r.rounds_required,
                "action_table": r.strategy.action_table(),
            }
            for r in winning
        ],
        "best_rounds_required": min(r.rounds_required for r in winning),
        "counter_strategy": None,
        "counter_config": None,
        "counter_branch_count": 0,
    }
