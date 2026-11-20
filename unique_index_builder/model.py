"""Immutable value objects used by the index builder.

The model is deliberately small and dependency free:

* :class:`SnapshotRow` is one base-table row read by the snapshot scanner.
* :class:`Change` is one insert/update/delete inside a transaction.
* :class:`Transaction` groups changes under one consecutive commit number.
* :class:`UniqueConflict` describes duplicate values at the watermark.
* :class:`PublishReport` carries every reason a publish was blocked.

Values and primary keys must be non-``None`` and hashable.  Requiring
hashability is what makes duplicate detection and index construction O(n);
requiring non-``None`` keeps ``None`` an unambiguous "not found" result from
the public lookup methods.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Hashable, Mapping, Optional, Sequence, Tuple

from .errors import (
    InvalidChangeError,
    InvalidSnapshotRowError,
    InvalidTransactionError,
)

Key = Hashable
Value = Hashable


def _check_key(value: Any, label: str) -> None:
    if value is None:
        raise ValueError("{} must not be None".format(label))
    try:
        hash(value)
    except TypeError as exc:
        raise ValueError("{} must be hashable: {!r}".format(label, value)) from exc


class ChangeKind(str, Enum):
    """The three row-level operations carried by a transaction."""

    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


@dataclass(frozen=True)
class Change:
    """One row-level operation.

    ``insert`` and ``update`` both mean "set this primary key to this value"
    at the transaction's position in commit order.  That is intentional: the
    builder only needs the post-image, and treating both operations the same
    way makes the result independent of whether a snapshot row for the key has
    already arrived.  ``delete`` removes the key.
    """

    kind: ChangeKind
    primary_key: Key
    value: Optional[Value] = None

    def __post_init__(self) -> None:
        kind = self.kind
        if not isinstance(kind, ChangeKind):
            try:
                kind = ChangeKind(kind)
            except (TypeError, ValueError) as exc:
                raise InvalidChangeError(
                    "unknown change kind: {!r}".format(self.kind)
                ) from exc
            object.__setattr__(self, "kind", kind)

        try:
            _check_key(self.primary_key, "primary_key")
        except ValueError as exc:
            raise InvalidChangeError(str(exc)) from exc

        if kind is ChangeKind.DELETE:
            if self.value is not None:
                raise InvalidChangeError(
                    "delete change for {!r} must not carry a value".format(
                        self.primary_key
                    )
                )
            return

        if self.value is None:
            raise InvalidChangeError(
                "{} change for {!r} requires a value".format(
                    kind.value, self.primary_key
                )
            )
        try:
            _check_key(self.value, "value")
        except ValueError as exc:
            raise InvalidChangeError(str(exc)) from exc

    @classmethod
    def insert(cls, primary_key: Key, value: Value) -> "Change":
        return cls(ChangeKind.INSERT, primary_key, value)

    @classmethod
    def update(cls, primary_key: Key, value: Value) -> "Change":
        return cls(ChangeKind.UPDATE, primary_key, value)

    @classmethod
    def delete(cls, primary_key: Key) -> "Change":
        return cls(ChangeKind.DELETE, primary_key)


@dataclass(frozen=True)
class Transaction:
    """A committed transaction with a consecutive commit sequence number."""

    commit_seq: int
    changes: Tuple[Change, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.commit_seq, bool) or not isinstance(self.commit_seq, int):
            raise InvalidTransactionError(
                "commit_seq must be an int, got {!r}".format(self.commit_seq)
            )
        if self.commit_seq < 0:
            raise InvalidTransactionError(
                "commit_seq must be non-negative, got {!r}".format(self.commit_seq)
            )

        try:
            normalized = tuple(self.changes)
        except TypeError as exc:
            raise InvalidTransactionError(
                "changes must be iterable, got {!r}".format(self.changes)
            ) from exc

        for change in normalized:
            if not isinstance(change, Change):
                raise InvalidTransactionError(
                    "changes must contain Change objects, got {!r}".format(change)
                )
        object.__setattr__(self, "changes", normalized)


@dataclass(frozen=True)
class SnapshotRow:
    """One row read from the base snapshot."""

    primary_key: Key
    value: Value

    def __post_init__(self) -> None:
        try:
            _check_key(self.primary_key, "primary_key")
            _check_key(self.value, "value")
        except ValueError as exc:
            raise InvalidSnapshotRowError(str(exc)) from exc


@dataclass(frozen=True)
class SnapshotSegment:
    """One deliverable unit produced by the snapshot scanner.

    A segment is just a batch id plus its rows, so a scanner can stream
    segments one at a time and the builder can ingest them in any order.
    """

    batch_id: Key
    rows: Tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        try:
            _check_key(self.batch_id, "batch_id")
        except ValueError as exc:
            raise InvalidSnapshotRowError(str(exc)) from exc
        try:
            object.__setattr__(self, "rows", tuple(self.rows))
        except TypeError as exc:
            raise InvalidSnapshotRowError(
                "rows must be iterable, got {!r}".format(self.rows)
            ) from exc


@dataclass(frozen=True)
class UniqueConflict:
    """A value that is shared by two or more primary keys at the watermark."""

    value: Value
    primary_keys: Tuple[Key, ...]

    def __str__(self) -> str:
        return "value {!r} is held by primary keys {!r}".format(
            self.value, list(self.primary_keys)
        )


class _OrderKey:
    """Deterministic ordering for hashable values that may be heterogeneous.

    Same-typed values are compared naturally (so integers sort numerically),
    and values of different types fall back to a stable type name / repr
    ordering.  Hashable values are not required to be mutually comparable, so
    this avoids the ``TypeError`` that a bare ``sorted`` could raise.
    """

    __slots__ = ("_value",)

    def __init__(self, value: Any) -> None:
        self._value = value

    @staticmethod
    def _rank(value: Any) -> Tuple[str, str]:
        value_type = type(value)
        return (
            getattr(value_type, "__module__", ""),
            getattr(value_type, "__qualname__", value_type.__name__),
        )

    def __lt__(self, other: "_OrderKey") -> bool:
        left, right = self._value, other._value
        if type(left) is type(right):
            try:
                return bool(left < right)
            except TypeError:
                pass
        left_rank, right_rank = self._rank(left), self._rank(right)
        if left_rank != right_rank:
            return left_rank < right_rank
        return repr(left) < repr(right)


def _sort_key(item: Any) -> _OrderKey:
    """A stable ordering that also works for heterogeneous hashable values."""

    return _OrderKey(item)


def _compress_ranges(seqs: Sequence[int]) -> Tuple[Tuple[int, int], ...]:
    if not seqs:
        return ()
    ranges = []
    start = previous = seqs[0]
    for seq in seqs[1:]:
        if seq == previous + 1:
            previous = seq
            continue
        ranges.append((start, previous))
        start = previous = seq
    ranges.append((start, previous))
    return tuple(ranges)


@dataclass(frozen=True)
class PublishReport:
    """The result of ``check_publish`` / ``try_publish``.

    ``missing_snapshot_batches``, ``missing_commit_seqs`` and ``conflicts``
    are independent diagnostic channels.  A blocked report may populate any
    combination of them; callers can distinguish "still waiting for input"
    from "the data at the watermark genuinely violates uniqueness".
    """

    published: bool
    watermark: int
    snapshot_complete: bool
    missing_snapshot_batches: Tuple[Any, ...] = ()
    missing_commit_seqs: Tuple[int, ...] = ()
    conflicts: Tuple[UniqueConflict, ...] = ()

    def __bool__(self) -> bool:
        return self.published

    @property
    def blocked(self) -> bool:
        return not self.published

    @property
    def has_missing_commits(self) -> bool:
        return bool(self.missing_commit_seqs)

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)

    @property
    def missing_commit_ranges(self) -> Tuple[Tuple[int, int], ...]:
        return _compress_ranges(self.missing_commit_seqs)

    @property
    def conflict_map(self) -> Mapping[Value, Tuple[Key, ...]]:
        return {conflict.value: conflict.primary_keys for conflict in self.conflicts}

    @property
    def evaluated_on_partial_state(self) -> bool:
        """True when the conflict list is provisional because input is missing.

        * A missing snapshot batch can still introduce a duplicate that has
          not been observed yet.
        * A missing commit can overwrite one of the rows currently sharing a
          value, so an observed duplicate may disappear.

        Either way the conflict list should be treated as a hint, not a final
        verdict, until both channels are complete.
        """

        return bool(self.missing_commit_seqs) or not self.snapshot_complete

    def summary(self) -> str:
        if self.published:
            return "published unique index at watermark {}".format(self.watermark)

        parts = []
        if not self.snapshot_complete:
            parts.append(
                "missing snapshot batches {!r}".format(
                    list(self.missing_snapshot_batches)
                )
            )
        if self.missing_commit_seqs:
            parts.append(
                "missing commit seqs {} (ranges {})".format(
                    list(self.missing_commit_seqs),
                    list(self.missing_commit_ranges),
                )
            )
        if self.conflicts:
            parts.append(
                "duplicate values: {}".format(
                    "; ".join(str(conflict) for conflict in self.conflicts)
                )
            )
        if not parts:
            parts.append("publish preconditions not satisfied")
        return "publish blocked at watermark {}: {}".format(
            self.watermark, "; ".join(parts)
        )
