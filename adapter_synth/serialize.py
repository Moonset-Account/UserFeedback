"""Stable, JSON-friendly serialization of game actions and states."""

from __future__ import annotations

from .game import Convert, GameState, PASS, PeerMove, WAIT


def describe_env_action(action) -> str:
    if action is PASS:
        return "pass"
    if isinstance(action, PeerMove):
        return (
            f"{action.peer}:{action.act.value}:{action.message}:"
            f"{action.source}->{action.target}"
        )
    return repr(action)


def describe_adapter_action(action) -> str:
    if action is WAIT:
        return "wait"
    if isinstance(action, Convert):
        return (
            f"convert:{action.direction.value}:"
            f"{action.source_message}->{action.destination_message}"
        )
    return repr(action)


def state_to_dict(state: GameState) -> dict:
    return {
        "state_a": state.state_a,
        "state_b": state.state_b,
        "pending_a2b": list(state.pending_a2b),
        "converted_a2b": list(state.converted_a2b),
        "pending_b2a": list(state.pending_b2a),
        "converted_b2a": list(state.converted_b2a),
    }
