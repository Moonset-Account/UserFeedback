"""Specification validation, table loading and dependency resolution."""

from __future__ import annotations

from .physical import ROWNO_COLUMN
from .spec import FUNCTION_POLICY, MAX_WINDOWS, WindowSpec


class SpecError(ValueError):
    """Raised when window declarations or input rows are malformed."""


class DependencyError(ValueError):
    """Raised on unknown references, self-dependency or a dependency cycle."""


def load_rows(raw_rows: list[dict]) -> list[dict]:
    """Validate input rows and inject each row's original row number.

    The reserved :data:`~window_planning.spec.ROWNO_COLUMN` column holds the
    0-based original index; it is the key used to restore output ordering.
    """
    if not isinstance(raw_rows, list):
        raise SpecError("rows must be a list of dictionaries")
    if not raw_rows:
        return []
    base_columns: set[str] | None = None
    rows: list[dict] = []
    for position, raw in enumerate(raw_rows):
        if not isinstance(raw, dict):
            raise SpecError(f"row {position} is not a dictionary")
        columns = set(raw)
        if ROWNO_COLUMN in columns:
            raise SpecError(
                f"row {position} uses reserved column {ROWNO_COLUMN!r}"
            )
        if base_columns is None:
            base_columns = columns
        elif columns != base_columns:
            raise SpecError(
                f"row {position} has columns {sorted(columns)}, expected "
                f"{sorted(base_columns)}: all rows must share one schema"
            )
        row = dict(raw)
        row[ROWNO_COLUMN] = position
        rows.append(row)
    return rows


def validate_specs(
    specs: list[WindowSpec], base_columns: frozenset[str]
) -> dict[str, list[str]]:
    """Validate window declarations and resolve their dependency graph.

    Returns a mapping ``window name -> dependency window names``. A dependency
    exists whenever a partition or ordering column is the output column of
    another window. Self-dependencies and cycles are rejected.

    An empty table carries no schema, so column existence is skipped in that
    edge case; the planner and executor both no-op on zero rows anyway.
    """
    if not specs:
        raise SpecError("at least one window is required")
    if len(specs) > MAX_WINDOWS:
        raise SpecError(f"at most {MAX_WINDOWS} windows are supported")
    check_columns = bool(base_columns)

    names = [spec.name for spec in specs]
    if len(set(names)) != len(names):
        raise SpecError("window names must be unique")
    output_columns = frozenset(names)
    available = base_columns | output_columns

    dependencies: dict[str, list[str]] = {}
    for spec in specs:
        if not spec.name or not isinstance(spec.name, str):
            raise SpecError("each window needs a non-empty string name")
        expected_policy = FUNCTION_POLICY[spec.function]
        if spec.tie_policy is not expected_policy:
            raise SpecError(
                f"window {spec.name!r} uses function "
                f"{spec.function.value} which requires tie policy "
                f"{expected_policy.value}, got {spec.tie_policy.value}"
            )
        p_cols = list(spec.partition_by)
        o_cols = list(spec.order_columns)
        if len(set(p_cols)) != len(p_cols):
            raise SpecError(
                f"window {spec.name!r} repeats a partition column"
            )
        if len(set(o_cols)) != len(o_cols):
            raise SpecError(
                f"window {spec.name!r} repeats an ordering column"
            )
        if set(p_cols) & set(o_cols):
            raise SpecError(
                f"window {spec.name!r} uses the same column for partition "
                "and ordering"
            )
        unknown = (
            spec.referenced_columns - available if check_columns else frozenset()
        )
        if unknown:
            raise DependencyError(
                f"window {spec.name!r} references unknown columns "
                f"{sorted(unknown)}"
            )
        deps = [
            column
            for column in spec.referenced_columns
            if column in output_columns and column != spec.name
        ]
        if spec.name in spec.referenced_columns:
            raise DependencyError(
                f"window {spec.name!r} cannot depend on itself"
            )
        dependencies[spec.name] = deps

    _reject_cycles(specs, dependencies)
    return dependencies


def _reject_cycles(
    specs: list[WindowSpec], dependencies: dict[str, list[str]]
) -> None:
    """Kahn's algorithm; anything left over belongs to a cycle."""
    remaining = dict(dependencies)
    resolved: set[str] = set()
    while True:
        ready = sorted(
            name
            for name, deps in remaining.items()
            if all(dep in resolved for dep in deps)
        )
        if not ready:
            break
        for name in ready:
            resolved.add(name)
            del remaining[name]
    if remaining:
        raise DependencyError(
            "dependency cycle detected among windows "
            f"{sorted(remaining)}"
        )


def topo_order(
    specs: list[WindowSpec], dependencies: dict[str, list[str]]
) -> list[WindowSpec]:
    """Deterministic topological ordering (ties broken by declared order)."""
    position = {spec.name: i for i, spec in enumerate(specs)}
    done: set[str] = set()
    order: list[WindowSpec] = []
    while len(order) < len(specs):
        candidates = [
            name
            for name in position
            if name not in done
            and all(dep in done for dep in dependencies[name])
        ]
        chosen = min(candidates, key=lambda name: position[name])
        order.append(next(spec for spec in specs if spec.name == chosen))
        done.add(chosen)
    return order
