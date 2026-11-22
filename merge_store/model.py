"""Immutable data model shared by the MERGE execution engine.

The model deliberately keeps row identifiers (``row_id``) separate from the
column values, because the whole point of the engine is that a row keeps its
identity even when a MERGE statement rewrites the columns used to match it.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "TargetRow",
    "TargetTable",
    "Rule",
    "Match",
    "Change",
    "Conflict",
    "MergeOutcome",
    "MergeError",
    "MergeConfigError",
    "MergeConflictError",
]


class MergeError(ValueError):
    """Base class for MERGE plan/input errors."""


class MergeConfigError(MergeError):
    """Raised when the statement definition or input table is malformed.

    These are programming/definition errors (bad rule combination, duplicate
    stable row ids, unhashable match key, ...), not data conflicts.  Data
    conflicts are reported through :class:`MergeOutcome` instead.
    """


class MergeConflictError(MergeError):
    """Raised by ``execute_merge(..., raise_on_conflict=True)``.

    The full :class:`MergeOutcome` is available as ``.outcome`` so callers can
    inspect the matches and conflict locations that rejected the statement.
    """

    def __init__(self, outcome: "MergeOutcome") -> None:
        self.outcome = outcome
        kinds = ", ".join(sorted({c.kind for c in outcome.conflicts})) or "unknown"
        super().__init__(f"MERGE statement rejected ({kinds})")


def _copy_values(values: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(values, Mapping):
        raise MergeConfigError(f"row values must be a mapping, got {type(values).__name__}")
    return dict(values)


@dataclass(frozen=True)
class TargetRow:
    """One target-table row: a stable id plus its column values."""

    row_id: Hashable
    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        try:
            hash(self.row_id)
        except TypeError as exc:  # pragma: no cover - defensive
            raise MergeConfigError(f"row_id {self.row_id!r} is not hashable") from exc
        if self.row_id is None:
            raise MergeConfigError("row_id must not be None")
        object.__setattr__(self, "values", _copy_values(self.values))

    def key(self, columns: Iterable[str]) -> tuple[Any, ...]:
        """Return the match-key tuple for ``columns`` (missing column -> NULL)."""
        return tuple(self.values.get(column) for column in columns)


@dataclass(frozen=True)
class TargetTable:
    """Ordered, immutable collection of :class:`TargetRow` objects.

    Rows may be supplied as ``TargetRow`` instances or as ``(row_id, values)``
    pairs.  Row identifiers must be unique and non-``None``.
    """

    rows: tuple[TargetRow, ...]

    def __post_init__(self) -> None:
        normalized: list[TargetRow] = []
        for item in self.rows:
            if isinstance(item, TargetRow):
                normalized.append(TargetRow(item.row_id, item.values))
            elif isinstance(item, tuple) and len(item) == 2:
                row_id, values = item
                normalized.append(TargetRow(row_id, values))
            else:
                raise MergeConfigError(
                    "target rows must be TargetRow instances or (row_id, values) pairs"
                )
        seen: set[Hashable] = set()
        for row in normalized:
            if row.row_id in seen:
                raise MergeConfigError(f"duplicate target row_id {row.row_id!r}")
            seen.add(row.row_id)
        object.__setattr__(self, "rows", tuple(normalized))

    # -- convenience -----------------------------------------------------
    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[Hashable, Mapping[str, Any]]]) -> "TargetTable":
        return cls(tuple((rid, values) for rid, values in pairs))

    def __iter__(self):
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def row(self, row_id: Hashable) -> TargetRow:
        for row in self.rows:
            if row.row_id == row_id:
                return row
        raise KeyError(row_id)

    @property
    def row_ids(self) -> tuple[Hashable, ...]:
        return tuple(row.row_id for row in self.rows)

    def as_dict(self) -> dict[Hashable, dict[str, Any]]:
        return {row.row_id: dict(row.values) for row in self.rows}

    def key_of(self, row_id: Hashable, columns: Iterable[str]) -> tuple[Any, ...]:
        return self.row(row_id).key(columns)


_MATCHED = "matched"
_NOT_MATCHED = "not_matched"
_ACTIONS = ("update", "delete", "insert", "skip")


def _normalize_when(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if text in ("matched", "match"):
        return _MATCHED
    if text in ("not_matched", "unmatched", "no_match", "notmatch"):
        return _NOT_MATCHED
    raise MergeConfigError(f"unknown rule condition {value!r}")


@dataclass(frozen=True)
class Rule:
    """One ordered WHEN ... THEN ... rule.

    Parameters
    ----------
    action:
        ``"update"``, ``"delete"``, ``"insert"`` or ``"skip"``.
    when:
        ``"matched"`` or ``"not_matched"``.  Optional for update/delete
        (defaults to matched) and insert (defaults to not_matched); required
        for skip, because a skip rule can apply on either side.
    assign:
        For ``update``: a mapping ``column -> value`` or a callable
        ``(source_values, target_values) -> mapping``.  A mapping value that is
        itself callable is invoked with the same arguments.  Required for
        update (an empty mapping is a legal no-op).
        For ``insert``: same shapes, with ``target_values`` always ``None``;
        ``None`` means "copy the source row".
    predicate:
        Optional ``(source_values, target_values_or_None) -> bool`` guard.
        Rules are evaluated in declaration order and the first applicable one
        wins.
    name:
        Optional label copied into :class:`Match` records.
    """

    action: str
    when: str | None = None
    assign: Mapping[str, Any] | Callable[..., Mapping[str, Any]] | None = None
    predicate: Callable[[Mapping[str, Any], Mapping[str, Any] | None], bool] | None = None
    name: str = ""

    def __post_init__(self) -> None:
        action = str(self.action).strip().lower()
        if action not in _ACTIONS:
            raise MergeConfigError(f"unknown rule action {self.action!r}")
        when = _normalize_when(self.when)
        if when is None:
            if action in ("update", "delete"):
                when = _MATCHED
            elif action == "insert":
                when = _NOT_MATCHED
            else:
                raise MergeConfigError("skip rules must declare when='matched' or 'not_matched'")
        if action in ("update", "delete") and when != _MATCHED:
            raise MergeConfigError(f"{action} rules only apply to matched rows")
        if action == "insert" and when != _NOT_MATCHED:
            raise MergeConfigError("insert rules only apply to not-matched rows")
        if action == "update" and self.assign is None:
            raise MergeConfigError("update rules require an assign mapping or callable")
        if action in ("delete", "skip") and self.assign is not None:
            raise MergeConfigError(f"{action} rules must not define assign")
        if self.predicate is not None and not callable(self.predicate):
            raise MergeConfigError("predicate must be callable")
        if self.assign is not None and not callable(self.assign) and not isinstance(self.assign, Mapping):
            raise MergeConfigError("assign must be a mapping or callable")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "when", when)
        object.__setattr__(self, "name", self.name or f"{when}:{action}")


@dataclass(frozen=True)
class Match:
    """How one source row was bound, using the target table as of statement start."""

    source_index: int
    source_values: Mapping[str, Any]
    source_key: tuple[Any, ...]
    matched: bool
    target_row_id: Hashable | None
    target_key: tuple[Any, ...] | None
    target_values_before: Mapping[str, Any] | None
    rule_index: int | None
    rule_name: str | None
    rule_action: str | None
    effective_action: str
    reason: str
    candidate_row_ids: tuple[Hashable, ...] = ()


@dataclass(frozen=True)
class Change:
    """One applied update/delete/insert, with before and after values."""

    source_index: int
    action: str
    target_row_id: Hashable
    before: Mapping[str, Any] | None
    after: Mapping[str, Any] | None
    key_before: tuple[Any, ...] | None
    key_after: tuple[Any, ...] | None
    source_values: Mapping[str, Any]


@dataclass(frozen=True)
class Conflict:
    """A statement-level reason for rejecting the whole MERGE."""

    kind: str
    message: str
    key: tuple[Any, ...] | None = None
    row_ids: tuple[Hashable, ...] = ()
    source_indices: tuple[int, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MergeOutcome:
    """Result of one statement execution.

    ``target`` is the committed table when ``committed`` is true and the
    untouched input table otherwise.  ``projected`` is always the table the
    statement would produce, so final-key validation can be inspected even
    when the statement was rejected.
    """

    committed: bool
    target: TargetTable
    projected: TargetTable
    changes: tuple[Change, ...]
    matches: tuple[Match, ...]
    conflicts: tuple[Conflict, ...]

    @property
    def ok(self) -> bool:
        return self.committed

    def change_for(self, source_index: int) -> Change | None:
        for change in self.changes:
            if change.source_index == source_index:
                return change
        return None

    def match_for(self, source_index: int) -> Match | None:
        for match in self.matches:
            if match.source_index == source_index:
                return match
        return None

    def conflict_kinds(self) -> tuple[str, ...]:
        return tuple(conflict.kind for conflict in self.conflicts)
