"""Public data types and exceptions for the compressed rank index."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Hashable, Literal

Tabletour = Literal["R", "S", "T"]
TABLES = ("R", "S", "T")

# Physical column order for each named table.
COLUMNS: dict[str, tuple[str, str]] = {
    "R": ("a", "b"),
    "S": ("b", "c"),
    "T": ("c", "a"),
}


class RankIndexError(Exception):
    """Base class for errors raised by this library."""


class InvalidRowError(RankIndexError, ValueError):
    """A row identifier, key, or multiplicity is invalid."""


class DuplicateRowError(InvalidRowError):
    """An insert used an existing physical row number."""


class RowNotFoundError(InvalidRowError):
    """The referenced physical row does not exist."""


class InvalidRankError(RankIndexError, IndexError):
    """A rank is not an integer in a snapshot's rank space."""


class CopyNotFoundError(RankIndexError, KeyError):
    """A copy reference is not a legal copy in the requested snapshot."""


class DeletedRankError(RankIndexError, LookupError):
    """An old rank has no retained counterpart in the new rank space."""

    def __init__(self, rank: int, interval: tuple[int, int] | None = None):
        self.rank = rank
        self.interval = interval
        super().__init__(f"old rank {rank} was deleted" + (f" in interval {interval!r}" if interval else ""))


class AddedRankError(RankIndexError, LookupError):
    """A new rank has no old counterpart because it was added."""

    def __init__(self, rank: int, interval: tuple[int, int] | None = None):
        self.rank = rank
        self.interval = interval
        super().__init__(f"new rank {rank} was added" + (f" in interval {interval!r}" if interval else ""))


class ConcurrentUpdateError(RankIndexError):
    """An optimistic transaction commit lost a race with another writer."""


@dataclass(frozen=True, slots=True)
class Row:
    """A physical table row.

    ``values`` is in the table's physical column order:
    R=(a,b), S=(b,c), T=(c,a).
    """

    table: str
    row_id: Hashable
    values: tuple[Any, Any]
    multiplicity: int = 1

    def __post_init__(self) -> None:
        if self.table not in TABLES:
            raise InvalidRowError(f"unknown table {self.table!r}; expected R, S, or T")
        if not isinstance(self.row_id, Hashable):
            raise InvalidRowError("row_id must be hashable")
        if len(self.values) != 2:
            raise InvalidRowError(f"{self.table} row must have exactly two key values")
        if isinstance(self.multiplicity, bool) or not isinstance(self.multiplicity, int):
            raise InvalidRowError("multiplicity must be a positive integer")
        if self.multiplicity <= 0:
            raise InvalidRowError("multiplicity must be positive")

    @property
    def a(self) -> Any:
        cols = COLUMNS[self.table]
        return self.values[cols.index("a")]

    @property
    def b(self) -> Any:
        cols = COLUMNS[self.table]
        return self.values[cols.index("b")]

    @property
    def c(self) -> Any:
        cols = COLUMNS[self.table]
        return self.values[cols.index("c")]


def r_row(row_id: Hashable, a: Any, b: Any, multiplicity: int = 1) -> Row:
    return Row("R", row_id, (a, b), multiplicity)


def s_row(row_id: Hashable, b: Any, c: Any, multiplicity: int = 1) -> Row:
    return Row("S", row_id, (b, c), multiplicity)


def t_row(row_id: Hashable, c: Any, a: Any, multiplicity: int = 1) -> Row:
    return Row("T", row_id, (c, a), multiplicity)


# Readable aliases.
RRow = r_row
SRow = s_row
TRow = t_row


@dataclass(frozen=True, slots=True)
class Copy:
    """A legal physical replica in the three-table join result."""

    a: Any
    b: Any
    c: Any
    r_row: Hashable
    s_row: Hashable
    t_row: Hashable
    r_rep: int
    s_rep: int
    t_rep: int

    @property
    def key(self) -> tuple[Any, Any, Any]:
        return (self.a, self.b, self.c)

    @property
    def triple(self) -> tuple[Hashable, Hashable, Hashable]:
        return (self.r_row, self.s_row, self.t_row)

    @property
    def reps(self) -> tuple[int, int, int]:
        return (self.r_rep, self.s_rep, self.t_rep)
