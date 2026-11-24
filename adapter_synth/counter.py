"""Replayable environment counter-strategy for unsolvable instances.

A counter-strategy is a tree rooted at the initial game state.  Each node
records the environment move that keeps the instance unsolvable and one
branch per legal adapter response, so an operator can replay the
environment's answers against *different adapter choices* and continue
branching until a leaf is reached.  Leaves are either a forced no-change
round (global deadlock) or exhaustion of the round budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

from .game import (
    INF,
    GameState,
    PASS,
    WAIT,
    admissible_env_choices,
    apply_adapter,
    apply_env,
    available_conversions,
)
from .model import BufferConfig, ProtocolPair
from .serialize import describe_adapter_action, describe_env_action, state_to_dict


@dataclass(frozen=True)
class CounterLeaf:
    """Terminal failure of a counter-strategy play."""

    state: GameState
    reason: str  # "deadlock" or "round_budget"

    def to_dict(self) -> dict:
        return {
            "kind": "leaf",
            "reason": self.reason,
            "state": state_to_dict(self.state),
        }


@dataclass(frozen=True)
class CounterNode:
    """An environment counter-move together with every adapter reply.

    ``branches`` maps the textual description of an adapter action to the
    child node (or leaf) reached after that action.
    """

    state: GameState
    env_action: object
    branches: List[Tuple[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kind": "node",
            "state": state_to_dict(self.state),
            "env_action": describe_env_action(self.env_action),
            "branches": [
                {
                    "adapter_action": label,
                    "child": child.to_dict(),
                }
                for label, child in self.branches
            ],
        }


def _successor_losing(rank: int, remaining_budget: int) -> bool:
    if rank >= INF:
        return True
    return 1 + rank > remaining_budget


def build_counter_strategy(
    proto: ProtocolPair,
    config: BufferConfig,
    ranks: Dict[GameState, int],
    start: GameState,
    round_budget: int,
) -> Union[CounterNode, CounterLeaf]:
    """Build the replayable counter-strategy tree rooted at ``start``.

    ``ranks`` is the minimax rank table produced by :func:`compute_ranks`.
    The state at the root must be losing under ``round_budget``.
    """

    memo: Dict[Tuple[GameState, int], object] = {}

    def build(state: GameState, remaining: int) -> object:
        if state.is_goal(proto):
            raise ValueError("counter-strategy cannot be rooted at a goal state")
        if remaining <= 0:
            return CounterLeaf(state, "round_budget")
        key = (state, remaining)
        if key in memo:
            return memo[key]

        choices = admissible_env_choices(proto, config, state)
        losing_choice = None
        forced_leaf: Optional[CounterLeaf] = None

        for choice in choices:
            if choice is PASS:
                conversions = available_conversions(proto, config, state)
                if not conversions:
                    forced_leaf = CounterLeaf(state, "deadlock")
                    losing_choice = choice
                    break
                # Environment needs every adapter reply to stay losing.
                if all(
                    _successor_losing(ranks[apply_adapter(state, conv)], remaining)
                    for conv in conversions
                ):
                    losing_choice = choice
                    break
                continue

            mid = apply_env(state, choice)
            if mid.is_goal(proto):
                continue  # lets the adapter win immediately
            options = available_conversions(proto, config, mid) + [WAIT]
            if all(
                _successor_losing(ranks[apply_adapter(mid, opt)], remaining)
                for opt in options
            ):
                losing_choice = choice
                break

        if losing_choice is None:  # pragma: no cover - guarded by rank math
            raise RuntimeError(
                "losing state without a counter-move; ranks and budget disagree"
            )

        if forced_leaf is not None:
            node = CounterNode(state, losing_choice, [
                (describe_adapter_action(WAIT), forced_leaf)
            ])
            memo[key] = node
            return node

        if losing_choice is PASS:
            conversions = available_conversions(proto, config, state)
            branches = [
                (
                    describe_adapter_action(conv),
                    build(apply_adapter(state, conv), remaining - 1),
                )
                for conv in conversions
            ]
        else:
            mid = apply_env(state, losing_choice)
            options = available_conversions(proto, config, mid) + [WAIT]
            branches = [
                (
                    describe_adapter_action(opt),
                    build(apply_adapter(mid, opt), remaining - 1),
                )
                for opt in options
            ]

        node = CounterNode(state, losing_choice, branches)
        memo[key] = node
        return node

    return build(start, round_budget)


def counter_branch_count(root: object) -> int:
    """Number of distinct root-to-leaf plays in a counter-strategy tree."""

    if isinstance(root, CounterLeaf):
        return 1
    return sum(counter_branch_count(child) for _, child in root.branches)


def counter_to_dict(root: object) -> dict:
    """JSON-friendly form of a counter-strategy tree (or leaf)."""

    return root.to_dict()


def counter_max_depth(root: object) -> int:
    """Longest root-to-leaf depth (number of environment rounds)."""

    if isinstance(root, CounterLeaf):
        return 0
    return 1 + max(counter_max_depth(child) for _, child in root.branches)
