"""Synthesis library for a bounded-buffer adapter between two RPC peers.

The package implements an adversarial reachability game between an
environment, which controls the two peer finite state machines, and an
adapter, which moves messages through bounded FIFO buffers according to an
accepted message mapping.  It synthesizes an adapter policy that forces both
peers into final states with every buffer empty, reports the minimum buffer
capacities required, and, when no such policy exists, emits a replayable
environment counter-strategy tree.
"""

from .model import (
    Act,
    Direction,
    PeerFSM,
    PeerTransition,
    MessageMapping,
    BufferConfig,
    ProtocolPair,
)
from .game import (
    GameState,
    PeerMove,
    EnvAction,
    AdapterAction,
    MoveOutcome,
    enabled_moves,
    available_conversions,
    admissible_env_choices,
    apply_env,
    apply_adapter,
    play_round,
    enumerate_reachable,
    compute_ranks,
)
from .synthesize import (
    AdapterStrategy,
    SynthesisResult,
    synthesize,
    minimum_buffer_capacity,
)
from .counter import (
    CounterNode,
    CounterLeaf,
    build_counter_strategy,
    counter_to_dict,
    counter_branch_count,
)
from .replay import (
    simulate_policy,
    enumerate_counter_plays,
)

__all__ = [
    "Act",
    "Direction",
    "PeerFSM",
    "PeerTransition",
    "MessageMapping",
    "BufferConfig",
    "ProtocolPair",
    "GameState",
    "PeerMove",
    "EnvAction",
    "AdapterAction",
    "MoveOutcome",
    "enabled_moves",
    "available_conversions",
    "admissible_env_choices",
    "apply_env",
    "apply_adapter",
    "play_round",
    "enumerate_reachable",
    "compute_ranks",
    "AdapterStrategy",
    "SynthesisResult",
    "synthesize",
    "minimum_buffer_capacity",
    "CounterNode",
    "CounterLeaf",
    "build_counter_strategy",
    "counter_to_dict",
    "counter_branch_count",
    "simulate_policy",
    "enumerate_counter_plays",
]
