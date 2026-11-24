"""Data model for minimum-cost deletion repairs and consistent queries.

The model has three kinds of integrity constraints:

* unique keys (single or composite) declared on a table,
* foreign keys, which may be *mandatory* or *optional*,
* explicit denial rules saying that two rows may not coexist.

Only deletions are allowed.  Every stored row carries a positive deletion
cost, and the cost of a repair is the sum of the costs of the rows it deletes.

Semantics worth spelling out:

* Key columns may not contain ``None`` (validated eagerly).  Two distinct rows
  of a table violate a key when all key columns compare equal.
* A *mandatory* foreign key requires every child value to be non-null and to
  reference a surviving parent row.
* An *optional* foreign key follows SQL ``MATCH SIMPLE``: if any child column
  of the foreign key is ``None`` the row is unconstrained; otherwise the
  (non-null) value must reference a surviving parent row.
* A foreign key always targets a declared unique key of the parent table.
  Because several parent rows may share that key value in the *inconsistent*
  input, satisfying a foreign key means "at least one matching parent
  survives".  Any valid repair keeps at most one of those parents anyway.
* A denial rule is a conjunction of column equalities between a row of the
  left table and a row of the right table.  ``None`` compares equal to
  ``None`` (the rule is a statement about the stored values, not SQL NULL
  logic).  When both sides name the same table, only pairs of *distinct*
  rows are compared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


class ModelError(ValueError):
    """Raised when a problem description is malformed."""


@dataclass(frozen=True, order=True)
class RowKey:
    """Stable identity of a stored row: (table name, row id)."""

    table: str
    rid: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.table}[{self.rid}]"


def coerce_row_key(value: Any) -> RowKey:
    """Accept a :class:`RowKey` or a ``(table, rid)`` pair."""

    if isinstance(value, RowKey):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 2:
        table, rid = value
        if isinstance(table, str) and isinstance(rid, str):
            return RowKey(table, rid)
    raise ModelError(
        "row key must be a RowKey or a (table, rid) pair, got %r" % (value,)
    )


def _as_str_tuple(value: Any, what: str) -> tuple[str, ...]:
    if isinstance(value, str):
        raise ModelError(
            "%s must be a sequence of column names, not a bare string" % what
        )
    try:
        items = tuple(value)
    except TypeError:
        raise ModelError("%s must be a sequence of column names" % what) from None
    for item in items:
        if not isinstance(item, str):
            raise ModelError("%s contains a non-string name: %r" % (what, item))
    return items


@dataclass(frozen=True)
class Row:
    """A stored row with a stable id and a positive deletion cost."""

    rid: str
    values: Mapping[str, Any]
    cost: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.rid, str) or not self.rid:
            raise ModelError("row id must be a non-empty string")
        if not isinstance(self.values, Mapping):
            raise ModelError(
                "row %r: values must be a mapping column -> value" % (self.rid,)
            )
        if isinstance(self.cost, bool) or not isinstance(self.cost, int):
            raise ModelError("row %r: deletion cost must be an integer" % (self.rid,))
        if self.cost <= 0:
            raise ModelError("row %r: deletion cost must be positive" % (self.rid,))
        object.__setattr__(self, "values", dict(self.values))


@dataclass(frozen=True)
class Table:
    """A table: ordered columns, stored rows, and zero or more unique keys."""

    name: str
    columns: Sequence[str]
    rows: Sequence[Row] = ()
    keys: Sequence[Sequence[str]] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ModelError("table name must be a non-empty string")

        columns = _as_str_tuple(self.columns, "table %r columns" % (self.name,))
        if not columns:
            raise ModelError("table %r must declare at least one column" % (self.name,))
        if len(set(columns)) != len(columns):
            raise ModelError("table %r declares duplicate column names" % (self.name,))

        rows = tuple(self.rows)
        for row in rows:
            if not isinstance(row, Row):
                raise ModelError("table %r: rows must be Row instances" % (self.name,))
        rids = [row.rid for row in rows]
        if len(set(rids)) != len(rids):
            raise ModelError("table %r contains duplicate row ids" % (self.name,))

        column_set = set(columns)
        keys = tuple(
            _as_str_tuple(key, "table %r key" % (self.name,)) for key in self.keys
        )
        for key in keys:
            if not key:
                raise ModelError("table %r declares an empty key" % (self.name,))
            if len(set(key)) != len(key):
                raise ModelError(
                    "table %r key %r repeats a column" % (self.name, key)
                )
            unknown = set(key) - column_set
            if unknown:
                raise ModelError(
                    "table %r key %r uses unknown columns %s"
                    % (self.name, key, sorted(unknown))
                )

        for row in rows:
            value_columns = set(row.values)
            if value_columns != column_set:
                missing = sorted(column_set - value_columns)
                extra = sorted(value_columns - column_set)
                raise ModelError(
                    "table %r row %r has wrong columns (missing=%s, extra=%s)"
                    % (self.name, row.rid, missing, extra)
                )
            for key in keys:
                nulls = [column for column in key if row.values[column] is None]
                if nulls:
                    raise ModelError(
                        "table %r row %r has NULL in key columns %s"
                        % (self.name, row.rid, nulls)
                    )

        object.__setattr__(self, "columns", columns)
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "keys", keys)


@dataclass(frozen=True)
class ForeignKey:
    """A foreign key from ``child_table`` to a declared key of ``parent_table``."""

    child_table: str
    child_columns: Sequence[str]
    parent_table: str
    parent_columns: Sequence[str]
    mandatory: bool = True

    def __post_init__(self) -> None:
        for label, value in (
            ("child_table", self.child_table),
            ("parent_table", self.parent_table),
        ):
            if not isinstance(value, str) or not value:
                raise ModelError("foreign key %s must be a non-empty string" % label)
        child = _as_str_tuple(self.child_columns, "foreign key child_columns")
        parent = _as_str_tuple(self.parent_columns, "foreign key parent_columns")
        if not child:
            raise ModelError("foreign key must name at least one child column")
        if len(child) != len(parent):
            raise ModelError(
                "foreign key child and parent column lists must have equal length"
            )
        if len(set(child)) != len(child):
            raise ModelError("foreign key repeats a child column")
        if len(set(parent)) != len(parent):
            raise ModelError("foreign key repeats a parent column")
        if not isinstance(self.mandatory, bool):
            raise ModelError("foreign key 'mandatory' must be a bool")
        object.__setattr__(self, "child_columns", child)
        object.__setattr__(self, "parent_columns", parent)


@dataclass(frozen=True)
class Denial:
    """Explicit rule: no left row and right row may coexist.

    A pair ``(l, r)`` violates the rule when
    ``l.values[left_columns[i]] == r.values[right_columns[i]]`` for every ``i``.
    If both sides name the same table only distinct rows are considered.
    """

    left_table: str
    right_table: str
    left_columns: Sequence[str]
    right_columns: Sequence[str]

    def __post_init__(self) -> None:
        for label, value in (
            ("left_table", self.left_table),
            ("right_table", self.right_table),
        ):
            if not isinstance(value, str) or not value:
                raise ModelError("denial %s must be a non-empty string" % label)
        left = _as_str_tuple(self.left_columns, "denial left_columns")
        right = _as_str_tuple(self.right_columns, "denial right_columns")
        if not left:
            raise ModelError("denial must name at least one column per side")
        if len(left) != len(right):
            raise ModelError(
                "denial left and right column lists must have equal length"
            )
        if len(set(left)) != len(left):
            raise ModelError("denial repeats a left column")
        if len(set(right)) != len(right):
            raise ModelError("denial repeats a right column")
        object.__setattr__(self, "left_columns", left)
        object.__setattr__(self, "right_columns", right)


@dataclass(frozen=True)
class Query:
    """An equality-join query with a projection, evaluated under set semantics.

    ``tables`` lists the tables that are joined (each at most once).
    ``equalities`` is a conjunction of column equalities across those tables.
    ``projection`` selects the output columns, in order.
    """

    tables: Sequence[str]
    equalities: Sequence[
        Sequence[Sequence[str]]
    ] = ()
    projection: Sequence[Sequence[str]] = ()

    def __post_init__(self) -> None:
        tables = _as_str_tuple(self.tables, "query tables")
        if not tables:
            raise ModelError("query must name at least one table")
        if len(set(tables)) != len(tables):
            raise ModelError("query may not repeat a table")

        equalities = []
        for pair in self.equalities:
            try:
                left, right = pair
            except (TypeError, ValueError):
                raise ModelError(
                    "each equality must be a pair of (table, column) references"
                ) from None
            equalities.append(
                (
                    _column_ref(left, "equality reference"),
                    _column_ref(right, "equality reference"),
                )
            )

        projection = tuple(
            _column_ref(ref, "projection reference") for ref in self.projection
        )
        if not projection:
            raise ModelError("query projection must name at least one column")

        object.__setattr__(self, "tables", tables)
        object.__setattr__(self, "equalities", tuple(equalities))
        object.__setattr__(self, "projection", projection)


def _column_ref(value: Any, what: str) -> tuple[str, str]:
    if isinstance(value, str):
        raise ModelError("%s must be a (table, column) pair" % what)
    try:
        items = tuple(value)
    except TypeError:
        raise ModelError("%s must be a (table, column) pair" % what) from None
    if len(items) != 2 or not all(isinstance(item, str) for item in items):
        raise ModelError("%s must be a (table, column) pair" % what)
    return (items[0], items[1])


@dataclass(frozen=True)
class Problem:
    """A complete input instance."""

    tables: Sequence[Table]
    foreign_keys: Sequence[ForeignKey] = ()
    denials: Sequence[Denial] = ()

    def __post_init__(self) -> None:
        tables = tuple(self.tables)
        for table in tables:
            if not isinstance(table, Table):
                raise ModelError("problem tables must be Table instances")
        names = [table.name for table in tables]
        if len(set(names)) != len(names):
            raise ModelError("problem declares duplicate table names")
        by_name = {table.name: table for table in tables}

        foreign_keys = tuple(self.foreign_keys)
        for fk in foreign_keys:
            if not isinstance(fk, ForeignKey):
                raise ModelError("problem foreign keys must be ForeignKey instances")
            child = _lookup_table(by_name, fk.child_table, "foreign key child")
            parent = _lookup_table(by_name, fk.parent_table, "foreign key parent")
            _require_columns(child, fk.child_columns, "foreign key child")
            _require_columns(parent, fk.parent_columns, "foreign key parent")
            parent_columns = frozenset(fk.parent_columns)
            if not any(
                frozenset(key) == parent_columns
                for key in parent.keys
            ):
                raise ModelError(
                    "foreign key %s.%s -> %s.%s must target a declared unique key"
                    % (
                        fk.child_table,
                        fk.child_columns,
                        fk.parent_table,
                        fk.parent_columns,
                    )
                )

        denials = tuple(self.denials)
        for denial in denials:
            if not isinstance(denial, Denial):
                raise ModelError("problem denials must be Denial instances")
            left = _lookup_table(by_name, denial.left_table, "denial left")
            right = _lookup_table(by_name, denial.right_table, "denial right")
            _require_columns(left, denial.left_columns, "denial left")
            _require_columns(right, denial.right_columns, "denial right")

        object.__setattr__(self, "tables", tables)
        object.__setattr__(self, "foreign_keys", foreign_keys)
        object.__setattr__(self, "denials", denials)

    def table(self, name: str) -> Table:
        for table in self.tables:
            if table.name == name:
                return table
        raise ModelError("unknown table %r" % (name,))

    def row_keys(self) -> tuple[RowKey, ...]:
        return tuple(
            RowKey(table.name, row.rid)
            for table in self.tables
            for row in table.rows
        )

    def row(self, key: RowKey) -> Row:
        table = self.table(key.table)
        for row in table.rows:
            if row.rid == key.rid:
                return row
        raise ModelError("unknown row %s" % (key,))


def _lookup_table(by_name: Mapping[str, Table], name: str, what: str) -> Table:
    try:
        return by_name[name]
    except KeyError:
        raise ModelError("%s names unknown table %r" % (what, name)) from None


def _require_columns(table: Table, columns: Iterable[str], what: str) -> None:
    unknown = set(columns) - set(table.columns)
    if unknown:
        raise ModelError(
            "%s references unknown columns %s of table %r"
            % (what, sorted(unknown), table.name)
        )


__all__ = [
    "Denial",
    "ForeignKey",
    "ModelError",
    "Problem",
    "Query",
    "Row",
    "RowKey",
    "Table",
    "coerce_row_key",
]
