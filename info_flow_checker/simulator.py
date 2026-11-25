"""Scalar and bit-parallel Boolean circuit simulation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .circuit import AND, CONST, INPUT, MUX, NOT, OR, Circuit
from .errors import CircuitEvaluationError


def as_bool(value: Any, *, where: str = "assignment") -> bool:
    """Convert strict Boolean / 0-1 assignment values to :class:`bool`."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"0", "1", "false", "true"}:
        return value.strip().lower() in {"1", "true"}
    raise CircuitEvaluationError(f"{where} must be Boolean or 0/1, got {value!r}")


def evaluate_nodes(
    circuit: Circuit,
    assignment: Mapping[str, bool | int],
) -> dict[str, bool]:
    """Evaluate every node and return a mapping of node id to Boolean value."""

    missing = [name for name in circuit.inputs if name not in assignment]
    if missing:
        raise CircuitEvaluationError(f"missing input values: {', '.join(missing)}")

    values: dict[str, bool] = {}
    for node_id in circuit.order:
        node = circuit.nodes[node_id]
        if node.op == INPUT:
            values[node_id] = as_bool(assignment[node_id], where=f"input {node_id!r}")
        elif node.op == CONST:
            if node.value is None:  # Validated circuits never hit this.
                raise CircuitEvaluationError(f"constant {node_id!r} has no value")
            values[node_id] = node.value
        elif node.op == AND:
            values[node_id] = all(values[arg] for arg in node.args)
        elif node.op == OR:
            values[node_id] = any(values[arg] for arg in node.args)
        elif node.op == NOT:
            values[node_id] = not values[node.args[0]]
        elif node.op == MUX:
            condition = values[node.args[0]]
            values[node_id] = values[node.args[1]] if condition else values[node.args[2]]
        else:  # pragma: no cover - circuit validation prevents unknown ops
            raise CircuitEvaluationError(f"unknown op {node.op!r} on node {node_id!r}")
    return values


def evaluate(
    circuit: Circuit,
    assignment: Mapping[str, bool | int],
    output_names: list[str] | tuple[str, ...] | None = None,
) -> dict[str, bool]:
    """Evaluate named outputs under one complete input assignment."""

    node_values = evaluate_nodes(circuit, assignment)
    if output_names is None:
        names = circuit.output_names
    else:
        names = tuple(output_names)
    result: dict[str, bool] = {}
    for name in names:
        try:
            node_id = circuit.output_node(name)
        except KeyError as exc:
            raise CircuitEvaluationError(str(exc)) from exc
        result[name] = node_values[node_id]
    return result


def build_secret_masks(secret_names: tuple[str, ...]) -> dict[str, int]:
    """Build one bitset mask per secret input over all secret assignments."""

    secret_count = len(secret_names)
    assignment_count = 1 << secret_count
    positional_masks: list[int] = []
    for j in range(secret_count):
        mask = 0
        for assignment_index in range(assignment_count):
            if (assignment_index >> j) & 1:
                mask |= 1 << assignment_index
        positional_masks.append(mask)
    return {name: positional_masks[j] for j, name in enumerate(secret_names)}


def simulate_secret_bitsets(
    circuit: Circuit,
    *,
    public_positions: Mapping[str, int],
    secret_names: tuple[str, ...],
    public_index: int,
    secret_masks: Mapping[str, int] | None = None,
) -> tuple[dict[str, int], int, int]:
    """Simulate one public assignment for every secret assignment at once.

    Each node value is a Python integer used as a dense bitset.  Bit ``s`` is
    the node value under secret assignment index ``s``.
    """

    secret_count = len(secret_names)
    assignment_count = 1 << secret_count
    all_bits = (1 << assignment_count) - 1

    if secret_masks is None:
        secret_masks = build_secret_masks(secret_names)

    values: dict[str, int] = {}
    for node_id in circuit.order:
        node = circuit.nodes[node_id]
        if node.op == INPUT:
            if node_id in public_positions:
                bit = (public_index >> public_positions[node_id]) & 1
                values[node_id] = all_bits if bit else 0
            else:
                values[node_id] = secret_masks[node_id]
        elif node.op == CONST:
            values[node_id] = all_bits if node.value else 0
        elif node.op == AND:
            value = all_bits
            for arg in node.args:
                value &= values[arg]
            values[node_id] = value
        elif node.op == OR:
            value = 0
            for arg in node.args:
                value |= values[arg]
            values[node_id] = value
        elif node.op == NOT:
            values[node_id] = all_bits ^ values[node.args[0]]
        elif node.op == MUX:
            selector = values[node.args[0]]
            true_value = values[node.args[1]]
            false_value = values[node.args[2]]
            values[node_id] = (selector & true_value) | ((all_bits ^ selector) & false_value)
        else:  # pragma: no cover
            raise CircuitEvaluationError(f"unknown op {node.op!r} on node {node_id!r}")

    return values, assignment_count, all_bits
