"""Data model for the left-deep equi-join planner.

The planner works on byte-sized relations and a *partitioning property*:
a relation is either unpartitioned (``None``) or hash-partitioned on an
equivalence class of columns (a ``frozenset`` of ``Table.column`` names).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


class InputError(ValueError):
    """Raised when a query description is structurally invalid."""


@dataclass(frozen=True)
class Table:
    """A base table.

    ``partition_key`` is the local column name the table is initially
    hash-partitioned on, or ``None`` when the table has no partitioning
    property (e.g. it is round-robin distributed or not distributed).
    """

    name: str
    nbytes: int
    partition_key: str | None = None

    def qualified_partition_key(self) -> str | None:
        if self.partition_key is None:
            return None
        return f"{self.name}.{self.partition_key}"


@dataclass(frozen=True)
class JoinEdge:
    """An equi-join edge, each endpoint written as ``Table.column``."""

    left: str
    right: str


@dataclass(frozen=True)
class Query:
    """A fully validated planner input."""

    nodes: int
    tables: tuple[Table, ...]
    edges: tuple[JoinEdge, ...]
    output_bytes: Mapping[frozenset[str], int]
    broadcast_limit: int | None = None

    @property
    def table_names(self) -> frozenset[str]:
        return frozenset(table.name for table in self.tables)

    def table(self, name: str) -> Table:
        for table in self.tables:
            if table.name == name:
                return table
        raise KeyError(name)

    def result_bytes(self, tables: frozenset[str]) -> int:
        """Output size for a joined set of tables."""
        if len(tables) == 1:
            return self.table(next(iter(tables))).nbytes
        try:
            return self.output_bytes[tables]
        except KeyError as exc:  # pragma: no cover - guarded by validation
            raise InputError(
                "missing output byte estimate for joinable set: "
                + ",".join(sorted(tables))
            ) from exc


@dataclass(frozen=True)
class PlanStep:
    """One left-deep join step and its exact transfer cost."""

    index: int
    method: str  # "broadcast" or "repartition"
    left_tables: frozenset[str]
    right_table: str
    left_key: str
    right_key: str
    left_transfer: int
    right_transfer: int
    cost: int
    cumulative_cost: int
    result_tables: frozenset[str]
    result_bytes: int
    result_partition_columns: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "method": self.method,
            "left_tables": sorted(self.left_tables),
            "right_table": self.right_table,
            "left_key": self.left_key,
            "right_key": self.right_key,
            "left_transfer": self.left_transfer,
            "right_transfer": self.right_transfer,
            "cost": self.cost,
            "cumulative_cost": self.cumulative_cost,
            "result_tables": sorted(self.result_tables),
            "result_bytes": self.result_bytes,
            "result_partition_columns": list(self.result_partition_columns),
        }


@dataclass(frozen=True)
class Plan:
    """A complete left-deep execution plan."""

    steps: tuple[PlanStep, ...]
    total_cost: int
    result_tables: frozenset[str]
    result_bytes: int
    partition_columns: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_cost": self.total_cost,
            "result_tables": sorted(self.result_tables),
            "result_bytes": self.result_bytes,
            "partition_columns": list(self.partition_columns),
            "steps": [step.to_dict() for step in self.steps],
        }
