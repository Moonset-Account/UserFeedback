"""Immutable MVCC snapshots and rank-space cursors."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from . import treap
from .groups import KeyGroup, build_group
from .model import Copy, InvalidRankError
from .tables import TableSet


@dataclass(slots=True)
class Cursor:
    """A position pinned to one immutable :class:`Snapshot`.

    The cursor keeps the snapshot alive even after the database commits a
    newer rank space.
    """

    snapshot: "Snapshot"
    rank: int = 0

    def __post_init__(self) -> None:
        self.snapshot._check_rank(self.rank, allow_end=True)

    @property
    def total(self) -> int:
        return self.snapshot.total

    @property
    def remaining(self) -> int:
        return self.snapshot.total - self.rank

    def seek(self, rank: int) -> "Cursor":
        self.snapshot._check_rank(rank, allow_end=True)
        self.rank = rank
        return self

    def advance(self, delta: int = 1) -> Copy:
        """Move by ``delta`` and return the copy at the new position."""
        if isinstance(delta, bool) or not isinstance(delta, int):
            raise TypeError("delta must be an integer")
        new_rank = self.rank + delta
        self.snapshot._check_rank(new_rank, allow_end=True)
        self.rank = new_rank
        return self.current()

    def current(self) -> Copy:
        return self.snapshot.at(self.rank)

    def get(self) -> Copy:
        return self.current()

    def fetch_many(self, limit: int) -> list[Copy]:
        """Read up to ``limit`` copies and advance the cursor."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        out: list[Copy] = []
        for _ in range(limit):
            if self.rank >= self.snapshot.total:
                break
            out.append(self.snapshot.at(self.rank))
            self.rank += 1
        return out

    def __iter__(self) -> Iterator[Copy]:
        while self.rank < self.snapshot.total:
            yield self.snapshot.at(self.rank)
            self.rank += 1

    def __next__(self) -> Copy:
        if self.rank >= self.snapshot.total:
            raise StopIteration
        copy = self.snapshot.at(self.rank)
        self.rank += 1
        return copy


@dataclass(slots=True)
class Snapshot:
    """An immutable compressed rank space."""

    version: int
    tables: TableSet
    root: treap.Node | None
    total: int

    @classmethod
    def from_tables(cls, tables: TableSet, version: int = 0) -> "Snapshot":
        root: treap.Node | None = None
        for key in sorted(tables.closed_keys()):
            group = build_group(key, tables)
            if group is not None:
                root = treap.upsert(root, group)
        return cls(version=version, tables=tables, root=root, total=treap.total(root))

    def with_tables(
        self,
        new_tables: TableSet,
        affected_keys: set[tuple[Any, Any, Any]],
        version: int,
    ) -> "Snapshot":
        """Create a new version, rebuilding only ``affected_keys`` groups."""
        root = self.root
        for key in sorted(affected_keys):
            group = build_group(key, new_tables)
            if group is None:
                root = treap.erase(root, key)
            else:
                root = treap.upsert(root, group)
        return Snapshot(
            version=version,
            tables=new_tables,
            root=root,
            total=treap.total(root),
        )

    @property
    def keys(self) -> tuple[tuple[Any, Any, Any], ...]:
        """All closed join keys; primarily for assertions and debugging."""
        return tuple(treap.iter_keys(self.root))

    def group(self, key: tuple[Any, Any, Any]) -> KeyGroup | None:
        return treap.get(self.root, key)

    def _check_rank(self, rank: int, *, allow_end: bool = False) -> None:
        if isinstance(rank, bool) or not isinstance(rank, int):
            raise InvalidRankError("rank must be an integer")
        upper = self.total if allow_end else self.total - 1
        if rank < 0 or rank > upper:
            suffix = " (end allowed)" if allow_end else "-1"
            raise InvalidRankError(f"rank {rank} is outside [0, {self.total}{suffix}]")

    def group_for_rank(self, rank: int) -> tuple[KeyGroup, int, int]:
        self._check_rank(rank)
        return treap.find_by_rank(self.root, rank)

    def rank_before_key(self, key: tuple[Any, Any, Any]) -> int:
        return treap.rank_before(self.root, key)

    def group_total(self, key: tuple[Any, Any, Any]) -> int:
        group = self.group(key)
        return group.total if group is not None else 0

    def at(self, rank: int) -> Copy:
        group, local_rank, _ = self.group_for_rank(rank)
        return group.copy_at(local_rank)

    def get(self, rank: int) -> Copy:
        return self.at(rank)

    def rank_of(self, copy: Copy) -> int:
        group = self.group(copy.key)
        if group is None:
            from .model import CopyNotFoundError

            raise CopyNotFoundError(f"no closed join group for key {copy.key!r}")
        start = self.rank_before_key(copy.key)
        return start + group.rank_of_copy(copy)

    def locate(self, rank: int) -> tuple[Copy, int]:
        """Return ``(copy, reverse_rank)``; useful in tests and REPL."""
        copy = self.at(rank)
        return copy, self.rank_of(copy)

    def cursor(self, rank: int = 0) -> Cursor:
        return Cursor(self, rank)

    def __getitem__(self, rank: int | slice) -> Copy:
        if isinstance(rank, slice):
            raise TypeError(
                "slicing a trillion-row result is intentionally unsupported; use a Cursor"
            )
        return self.at(rank)
