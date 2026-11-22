"""Deterministic MERGE execution for in-memory relation stores.

Semantics implemented here (all deliberate, all documented in README):

* Matching is performed against a *snapshot* of the target table taken when the
  statement starts.  A row keeps its stable ``row_id`` even when an earlier
  source row rewrites the columns used to match it, and no source row can ever
  re-bind to a row that was inserted by the same statement.
* Ordinary equality only.  A match key containing ``None`` (a NULL) never
  matches anything -- not even another NULL.
* Rules are declared in order; for each source row the first rule whose
  condition side (matched / not matched) and predicate apply wins.
* More than one source row whose effective action is UPDATE or DELETE against
  the same target row rejects the whole statement.  Source rows that are
  matched but take a skip rule are exempt from that count and never trigger a
  fallback insert.
* Every action is applied to a projection; the projection must pass a final
  uniqueness check on the match columns before anything is committed.  On any
  conflict the input table is left untouched.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Hashable

from .model import (
    Change,
    Conflict,
    Match,
    MergeConfigError,
    MergeConflictError,
    MergeOutcome,
    Rule,
    TargetRow,
    TargetTable,
)

__all__ = [
    "execute_merge",
    "MergeStatement",
    "RelationStore",
    "default_insert_row_id",
]

_MODIFYING = frozenset({"update", "delete"})
_NONE_ACTION = "none"


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _sort_token(value: Any) -> tuple[str, Any]:
    """A total order token over heterogeneous Python values.

    Row ids and match keys are only required to be hashable, so sorting them
    must not rely on ``<`` between unrelated types.
    """

    if value is None:
        return ("none", "")
    if isinstance(value, bool):
        return ("bool", int(value))
    if isinstance(value, (int, float)):
        return ("number", value)
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, bytes):
        return ("bytes", value.hex())
    if isinstance(value, (tuple, list)):
        return ("sequence", tuple(_sort_token(part) for part in value))
    return (type(value).__name__, repr(value))


def _key_token(key: tuple[Any, ...] | None) -> tuple[tuple[str, Any], ...]:
    if key is None:
        return ()
    return tuple(_sort_token(part) for part in key)


def _normalize_columns(columns: Iterable[str]) -> tuple[str, ...]:
    try:
        normalized = tuple(columns)
    except TypeError as exc:
        raise MergeConfigError("match_columns must be an iterable of column names") from exc
    if not normalized:
        raise MergeConfigError("match_columns must not be empty")
    for column in normalized:
        if not isinstance(column, str) or not column:
            raise MergeConfigError(f"invalid match column {column!r}")
    if len(set(normalized)) != len(normalized):
        raise MergeConfigError("match_columns must be unique")
    return normalized


def _normalize_source(source_rows: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(source_rows):
        if not isinstance(row, Mapping):
            raise MergeConfigError(
                f"source row {index} must be a mapping, got {type(row).__name__}"
            )
        normalized.append(dict(row))
    return tuple(normalized)


def _normalize_rules(rules: Iterable[Rule]) -> tuple[Rule, ...]:
    normalized = tuple(rules)
    for rule in normalized:
        if not isinstance(rule, Rule):
            raise MergeConfigError(f"rules must be Rule instances, got {type(rule).__name__}")
    return normalized


def _key_has_null(key: tuple[Any, ...]) -> bool:
    return any(part is None for part in key)


def _ensure_hashable_key(key: tuple[Any, ...], *, where: str) -> None:
    try:
        hash(key)
    except TypeError as exc:
        raise MergeConfigError(
            f"{where} match key {key!r} contains unhashable values"
        ) from exc


def default_insert_row_id(source_index: int, values: Mapping[str, Any]) -> Hashable:
    """Default stable id for a row inserted by source row ``source_index``.

    Deterministic across runs and namespaced so it cannot be confused with a
    caller-supplied id.
    """

    return ("merge_insert", source_index)


def _resolve_assignment(
    assign: Mapping[str, Any] | Callable[..., Mapping[str, Any]] | None,
    source: Mapping[str, Any],
    target: Mapping[str, Any] | None,
    *,
    action: str,
) -> dict[str, Any]:
    if assign is None:
        if action == "insert":
            return dict(source)
        raise MergeConfigError("update rule is missing an assign mapping")
    if callable(assign):
        produced = assign(source, target)
    else:
        produced = {}
        for column, spec in assign.items():
            produced[column] = spec(source, target) if callable(spec) else spec
    if not isinstance(produced, Mapping):
        raise MergeConfigError("rule assignment must produce a mapping")
    resolved = dict(produced)
    if action == "insert":
        merged = dict(source)
        merged.update(resolved)
        return merged
    return resolved


def _select_rule(
    rules: tuple[Rule, ...],
    side: str,
    source: Mapping[str, Any],
    target_values: Mapping[str, Any] | None,
) -> tuple[int, Rule] | None:
    for index, rule in enumerate(rules):
        if rule.when != side:
            continue
        if rule.predicate is not None and not rule.predicate(source, target_values):
            continue
        return index, rule
    return None


# ---------------------------------------------------------------------------
# statement object
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MergeStatement:
    """A reusable MERGE statement: match columns plus ordered rules."""

    match_columns: Sequence[str]
    rules: Sequence[Rule]

    def __post_init__(self) -> None:
        object.__setattr__(self, "match_columns", _normalize_columns(self.match_columns))
        object.__setattr__(self, "rules", _normalize_rules(self.rules))

    def execute(
        self,
        target: TargetTable,
        source_rows: Iterable[Mapping[str, Any]],
        *,
        insert_row_id: Callable[[int, Mapping[str, Any]], Hashable] | None = None,
        raise_on_conflict: bool = False,
    ) -> MergeOutcome:
        return execute_merge(
            source_rows,
            target,
            self.match_columns,
            self.rules,
            insert_row_id=insert_row_id,
            raise_on_conflict=raise_on_conflict,
        )


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------
@dataclass
class _Planned:
    """One source row's binding and (optionally) the row it would change."""

    source_index: int
    source_values: Mapping[str, Any]
    source_key: tuple[Any, ...]
    matched: bool
    target_row: TargetRow | None
    target_key: tuple[Any, ...] | None
    target_values: Mapping[str, Any] | None
    rule_index: int | None
    rule: Rule | None
    effective_action: str
    reason: str
    candidates: tuple[TargetRow, ...] = ()
    new_values: dict[str, Any] | None = None
    insert_row_id: Hashable | None = None


def _match_record(planned: _Planned) -> Match:
    return Match(
        source_index=planned.source_index,
        source_values=dict(planned.source_values),
        source_key=planned.source_key,
        matched=planned.matched,
        target_row_id=planned.target_row.row_id if planned.target_row else None,
        target_key=planned.target_key,
        target_values_before=dict(planned.target_values) if planned.target_values is not None else None,
        rule_index=planned.rule_index,
        rule_name=planned.rule.name if planned.rule else None,
        rule_action=planned.rule.action if planned.rule else None,
        effective_action=planned.effective_action,
        reason=planned.reason,
        candidate_row_ids=tuple(row.row_id for row in planned.candidates),
    )


def execute_merge(
    source_rows: Iterable[Mapping[str, Any]],
    target: TargetTable,
    match_columns: Iterable[str],
    rules: Iterable[Rule],
    *,
    insert_row_id: Callable[[int, Mapping[str, Any]], Hashable] | None = None,
    raise_on_conflict: bool = False,
) -> MergeOutcome:
    """Execute one MERGE statement and return an inspectable outcome.

    Parameters
    ----------
    source_rows:
        Ordered source rows (mappings).  Position defines source order.
    target:
        The target table with stable row ids, as of statement start.
    match_columns:
        Columns compared for matching; any key containing ``None`` never
        matches.
    rules:
        Ordered :class:`Rule` objects.  First applicable rule per source row
        wins.
    insert_row_id:
        Factory ``(source_index, values) -> row_id`` for inserted rows.  The
        default is deterministic and collision-avoiding.
    raise_on_conflict:
        When true, raise :class:`MergeConflictError` (carrying the outcome)
        instead of returning a rejected outcome.
    """

    if not isinstance(target, TargetTable):
        raise MergeConfigError("target must be a TargetTable")
    columns = _normalize_columns(match_columns)
    sources = _normalize_source(source_rows)
    statement_rules = _normalize_rules(rules)
    id_factory = insert_row_id or default_insert_row_id

    conflicts: list[Conflict] = []

    # ---- snapshot index ------------------------------------------------
    index: dict[tuple[Any, ...], list[TargetRow]] = {}
    for row in target.rows:
        key = row.key(columns)
        _ensure_hashable_key(key, where=f"target row {row.row_id!r}")
        if _key_has_null(key):
            # NULL keys are invisible to ordinary equality, so they are never
            # indexed and can never be matched (nor do they violate final
            # uniqueness, which mirrors SQL UNIQUE semantics).
            continue
        index.setdefault(key, []).append(row)

    # ---- bind each source row against the snapshot ---------------------
    plans: list[_Planned] = []
    used_ids: set[Hashable] = set(target.row_ids)
    for source_index, source in enumerate(sources):
        key = tuple(source.get(column) for column in columns)
        _ensure_hashable_key(key, where=f"source row {source_index}")
        if _key_has_null(key):
            candidates: tuple[TargetRow, ...] = ()
            binding_reason = "null_key_never_matches"
        else:
            candidates = tuple(index.get(key, ()))
            binding_reason = "snapshot_key_match" if candidates else "no_matching_target_row"

        if len(candidates) > 1:
            conflicts.append(
                Conflict(
                    kind="duplicate_target_key",
                    message=(
                        f"source row {source_index} matches {len(candidates)} target "
                        "rows sharing one key"
                    ),
                    key=key,
                    row_ids=tuple(row.row_id for row in candidates),
                    source_indices=(source_index,),
                    details={"phase": "statement_start", "columns": columns},
                )
            )
            plans.append(
                _Planned(
                    source_index=source_index,
                    source_values=source,
                    source_key=key,
                    matched=True,
                    target_row=None,
                    target_key=key,
                    target_values=None,
                    rule_index=None,
                    rule=None,
                    effective_action="error",
                    reason="ambiguous_target_match",
                    candidates=candidates,
                )
            )
            continue

        target_row = candidates[0] if candidates else None
        matched = target_row is not None
        target_values = dict(target_row.values) if target_row is not None else None
        side = "matched" if matched else "not_matched"
        selection = _select_rule(statement_rules, side, source, target_values)

        if selection is None:
            plans.append(
                _Planned(
                    source_index=source_index,
                    source_values=source,
                    source_key=key,
                    matched=matched,
                    target_row=target_row,
                    target_key=target_row.key(columns) if target_row else None,
                    target_values=target_values,
                    rule_index=None,
                    rule=None,
                    effective_action=_NONE_ACTION,
                    reason=f"no_{side}_rule_applied",
                    candidates=candidates,
                )
            )
            continue

        rule_index, rule = selection
        planned = _Planned(
            source_index=source_index,
            source_values=source,
            source_key=key,
            matched=matched,
            target_row=target_row,
            target_key=target_row.key(columns) if target_row else None,
            target_values=target_values,
            rule_index=rule_index,
            rule=rule,
            effective_action=rule.action,
            reason=binding_reason,
            candidates=candidates,
        )

        if rule.action == "update":
            assert target_row is not None
            delta = _resolve_assignment(rule.assign, source, target_values, action="update")
            new_values = dict(target_values or {})
            new_values.update(delta)
            planned.new_values = new_values
        elif rule.action == "insert":
            delta = _resolve_assignment(rule.assign, source, None, action="insert")
            new_values = dict(delta)
            candidate = id_factory(source_index, dict(new_values))
            try:
                hash(candidate)
            except TypeError as exc:
                raise MergeConfigError(
                    f"insert_row_id produced unhashable id {candidate!r}"
                ) from exc
            suffix = 0
            base = candidate
            while candidate in used_ids:
                suffix += 1
                candidate = (base, suffix)
            used_ids.add(candidate)
            planned.insert_row_id = candidate
            planned.new_values = new_values
        plans.append(planned)

    # ---- multiplicity conflicts ---------------------------------------
    modifying: dict[Hashable, list[_Planned]] = {}
    for planned in plans:
        if planned.effective_action in _MODIFYING and planned.target_row is not None:
            modifying.setdefault(planned.target_row.row_id, []).append(planned)

    blocked_ids: set[Hashable] = set()
    for row_id, planned_group in modifying.items():
        if len(planned_group) > 1:
            blocked_ids.add(row_id)
            conflicts.append(
                Conflict(
                    kind="multiple_modifying_source_rows",
                    message=(
                        f"target row {row_id!r} would be modified/deleted by "
                        f"{len(planned_group)} source rows"
                    ),
                    key=planned_group[0].target_key,
                    row_ids=(row_id,),
                    source_indices=tuple(item.source_index for item in planned_group),
                    details={
                        "actions": tuple(item.effective_action for item in planned_group),
                        "rule_names": tuple(item.rule.name if item.rule else "" for item in planned_group),
                    },
                )
            )

    # ---- apply the projection ------------------------------------------
    # Contested rows (two or more true modifiers) are already a rejection;
    # none of their effects are projected, so ``projected`` reflects only the
    # uncontested actions plus inserts.  This keeps the would-be final state
    # unambiguous for inspection.
    working: dict[Hashable, dict[str, Any]] = {
        row.row_id: dict(row.values) for row in target.rows
    }
    order: list[Hashable] = list(target.row_ids)
    removed: set[Hashable] = set()
    applied: list[_Planned] = []

    for planned in plans:
        if planned.effective_action == "update":
            assert planned.target_row is not None and planned.new_values is not None
            row_id = planned.target_row.row_id
            if row_id in blocked_ids:
                continue
            working[row_id] = dict(planned.new_values)
            applied.append(planned)
        elif planned.effective_action == "delete":
            assert planned.target_row is not None
            row_id = planned.target_row.row_id
            if row_id in blocked_ids:
                continue
            removed.add(row_id)
            applied.append(planned)
        elif planned.effective_action == "insert":
            assert planned.insert_row_id is not None and planned.new_values is not None
            working[planned.insert_row_id] = dict(planned.new_values)
            order.append(planned.insert_row_id)
            applied.append(planned)

    projected_rows = tuple(
        TargetRow(row_id, working[row_id])
        for row_id in order
        if row_id not in removed
    )
    projected = TargetTable(projected_rows)

    # ---- final uniqueness check ----------------------------------------
    # Only keys that the statement actually touched are validated.  A pre-
    # existing duplicate among rows the statement never bound is not this
    # statement's responsibility (it would already have been invisible to
    # matching), mirroring the "only flag ambiguity on binding" rule above.
    touched_ids: set[Hashable] = set()
    for planned in applied:
        if planned.effective_action == "insert":
            touched_ids.add(planned.insert_row_id)
        elif planned.target_row is not None:
            touched_ids.add(planned.target_row.row_id)

    final_index: dict[tuple[Any, ...], list[Hashable]] = {}
    for row in projected.rows:
        key = row.key(columns)
        _ensure_hashable_key(key, where=f"projected row {row.row_id!r}")
        if _key_has_null(key):
            continue
        final_index.setdefault(key, []).append(row.row_id)

    source_of_change: dict[Hashable, int] = {}
    for planned in applied:
        if planned.effective_action == "insert":
            source_of_change[planned.insert_row_id] = planned.source_index
        elif planned.target_row is not None:
            source_of_change.setdefault(planned.target_row.row_id, planned.source_index)

    for key, row_ids in final_index.items():
        if len(row_ids) > 1 and any(row_id in touched_ids for row_id in row_ids):
            conflicts.append(
                Conflict(
                    kind="duplicate_final_key",
                    message=(
                        f"final target rows {list(row_ids)!r} share the match key; "
                        "the statement cannot commit"
                    ),
                    key=key,
                    row_ids=tuple(row_ids),
                    source_indices=tuple(
                        sorted(
                            source_of_change[row_id]
                            for row_id in row_ids
                            if row_id in source_of_change
                        )
                    ),
                    details={"phase": "after_actions", "columns": columns},
                )
            )

    # ---- assemble result ------------------------------------------------
    matches = tuple(_match_record(planned) for planned in plans)

    changes_list: list[Change] = []
    if not conflicts:
        for planned in applied:
            key_before = planned.target_key
            if planned.effective_action == "insert":
                changes_list.append(
                    Change(
                        source_index=planned.source_index,
                        action="insert",
                        target_row_id=planned.insert_row_id,
                        before=None,
                        after=dict(planned.new_values or {}),
                        key_before=None,
                        key_after=tuple((planned.new_values or {}).get(c) for c in columns),
                        source_values=dict(planned.source_values),
                    )
                )
            elif planned.effective_action == "update":
                changes_list.append(
                    Change(
                        source_index=planned.source_index,
                        action="update",
                        target_row_id=planned.target_row.row_id,
                        before=dict(planned.target_values or {}),
                        after=dict(planned.new_values or {}),
                        key_before=key_before,
                        key_after=tuple((planned.new_values or {}).get(c) for c in columns),
                        source_values=dict(planned.source_values),
                    )
                )
            elif planned.effective_action == "delete":
                changes_list.append(
                    Change(
                        source_index=planned.source_index,
                        action="delete",
                        target_row_id=planned.target_row.row_id,
                        before=dict(planned.target_values or {}),
                        after=None,
                        key_before=key_before,
                        key_after=None,
                        source_values=dict(planned.source_values),
                    )
                )
        changes_list.sort(key=lambda change: (change.source_index, _sort_token(change.target_row_id)))
    changes = tuple(changes_list)

    conflicts.sort(
        key=lambda conflict: (
            conflict.kind,
            _key_token(conflict.key),
            tuple(_sort_token(row_id) for row_id in conflict.row_ids),
            conflict.source_indices,
        )
    )

    committed = not conflicts
    outcome = MergeOutcome(
        committed=committed,
        target=projected if committed else target,
        projected=projected,
        changes=changes if committed else (),
        matches=matches,
        conflicts=tuple(conflicts),
    )
    if raise_on_conflict and not committed:
        raise MergeConflictError(outcome)
    return outcome


# ---------------------------------------------------------------------------
# store wrapper
# ---------------------------------------------------------------------------
class RelationStore:
    """A tiny relation store holding one :class:`TargetTable`.

    ``merge`` executes a statement; on commit the store's table is replaced
    atomically, otherwise it is left exactly as it was.
    """

    def __init__(self, table: TargetTable) -> None:
        if not isinstance(table, TargetTable):
            raise MergeConfigError("RelationStore requires a TargetTable")
        self._table = table

    @property
    def table(self) -> TargetTable:
        return self._table

    def merge(
        self,
        source_rows: Iterable[Mapping[str, Any]],
        match_columns: Iterable[str],
        rules: Iterable[Rule],
        *,
        insert_row_id: Callable[[int, Mapping[str, Any]], Hashable] | None = None,
        raise_on_conflict: bool = False,
    ) -> MergeOutcome:
        outcome = execute_merge(
            source_rows,
            self._table,
            match_columns,
            rules,
            insert_row_id=insert_row_id,
            raise_on_conflict=raise_on_conflict,
        )
        if outcome.committed:
            self._table = outcome.target
        return outcome

    def __len__(self) -> int:
        return len(self._table)

    def __iter__(self):
        return iter(self._table.rows)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RelationStore({len(self._table)} rows)"
