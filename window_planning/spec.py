"""Logical window specifications.

A window declaration consists of:

* ``partition_by``: partition key columns (any physical permutation is allowed);
* ``order_by``: ordering keys, each with an explicit direction and NULL position;
* ``tie_policy``: how ties on the ordering keys are resolved -- by an explicit
  row-number tail (:class:`TiePolicy.ROW_NUMBER`) or by treating the whole
  equal-valued set as one logical group (:class:`TiePolicy.GROUP`);
* ``function``: one of the three supported window functions.

The reserved column name :data:`ROWNO_COLUMN` holds the original row number of
each input row. User data may not contain a column with that name.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: Reserved physical column carrying the original 0-based row number.
ROWNO_COLUMN = "__rowno__"


class Direction(Enum):
    ASC = "ASC"
    DESC = "DESC"


class Nulls(Enum):
    FIRST = "FIRST"
    LAST = "LAST"


class TiePolicy(Enum):
    """How rows tied on all ordering keys are treated."""

    #: A physical row-number tail breaks ties, yielding a total order.
    ROW_NUMBER = "ROW_NUMBER"
    #: Tied rows form one logical equivalence group.
    GROUP = "GROUP"


class WindowFunction(Enum):
    ROW_NUMBER = "ROW_NUMBER"
    DENSE_RANK = "DENSE_RANK"
    CUMULATIVE_COUNT = "CUMULATIVE_COUNT"


@dataclass(frozen=True)
class OrderKey:
    """One ordering key with explicit direction and NULL position."""

    column: str
    direction: Direction
    nulls: Nulls

    def describe(self) -> str:
        return f"{self.column} {self.direction.value} NULLS {self.nulls.value}"


@dataclass(frozen=True)
class WindowSpec:
    """A single window item.

    ``partition_by`` and ``order_by`` are tuples so that specs are hashable and
    safe to share between planner, executor and verification code.
    """

    name: str
    partition_by: tuple[str, ...]
    order_by: tuple[OrderKey, ...]
    tie_policy: TiePolicy
    function: WindowFunction

    @property
    def order_columns(self) -> tuple[str, ...]:
        return tuple(k.column for k in self.order_by)

    @property
    def referenced_columns(self) -> frozenset[str]:
        return frozenset(self.partition_by) | frozenset(self.order_columns)

    def describe(self) -> str:
        parts = [f"window {self.name}: {self.function.value}"]
        if self.partition_by:
            parts.append("PARTITION BY " + ", ".join(self.partition_by))
        if self.order_by:
            parts.append(
                "ORDER BY " + ", ".join(k.describe() for k in self.order_by)
            )
        parts.append(f"TIE {self.tie_policy.value}")
        return " ".join(parts)


#: Hard limit stated by the problem.
MAX_WINDOWS = 8

#: Function / tie-policy compatibility rules.
FUNCTION_POLICY: dict[WindowFunction, TiePolicy] = {
    WindowFunction.ROW_NUMBER: TiePolicy.ROW_NUMBER,
    WindowFunction.DENSE_RANK: TiePolicy.GROUP,
    WindowFunction.CUMULATIVE_COUNT: TiePolicy.GROUP,
}
