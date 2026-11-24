"""Schema, dependency and instance model for the lossless-connection analyser.

Set semantics throughout: a row is a tuple, a relation is a *set* of rows,
and values are compared with ``==`` only (no ordering, no NULLs, no domain
specifics).  Values must be hashable so rows can live in sets/dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .errors import DataError, DependencyError, SchemaError

MAX_COLUMNS = 5

MVD = "MVD"
FD = "FD"


def _as_columns(raw: Iterable[str], what: str) -> tuple[str, ...]:
    """Normalise a set-like column specification into a sorted tuple."""
    cols: list[str] = []
    for c in raw:
        if not isinstance(c, str):
            raise SchemaError(f"{what}: column names must be strings, got {c!r}")
        cols.append(c)
    if len(set(cols)) != len(cols):
        raise SchemaError(f"{what}: duplicate column names in {cols!r}")
    return tuple(sorted(cols))


@dataclass(frozen=True)
class Schema:
    """The total schema: one to :data:`MAX_COLUMNS` columns."""

    columns: tuple[str, ...]

    def __post_init__(self) -> None:
        cols = _as_columns(self.columns, "schema")
        if not cols:
            raise SchemaError("schema must contain at least one column")
        if len(cols) > MAX_COLUMNS:
            raise SchemaError(
                f"schema has {len(cols)} columns; at most {MAX_COLUMNS} are supported"
            )
        object.__setattr__(self, "columns", cols)

    @classmethod
    def from_columns(cls, columns: Iterable[str]) -> "Schema":
        return cls(tuple(columns))

    def __len__(self) -> int:
        return len(self.columns)

    @property
    def column_set(self) -> frozenset[str]:
        return frozenset(self.columns)

    def require(self, cols: Iterable[str], what: str) -> tuple[str, ...]:
        out = tuple(sorted(cols))
        unknown = [c for c in out if c not in self.column_set]
        if unknown:
            raise SchemaError(
                f"{what}: unknown column(s) {unknown!r}; schema is {self.columns!r}"
            )
        return out


@dataclass(frozen=True)
class SubTable:
    """One sub-table of the decomposition (a projection schema)."""

    name: str
    columns: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise SchemaError(
                f"sub-table name must be a non-empty string, got {self.name!r}"
            )
        object.__setattr__(
            self, "columns", _as_columns(self.columns, f"sub-table {self.name!r}")
        )
        if not self.columns:
            raise SchemaError(f"sub-table {self.name!r} must have at least one column")

    def checked_against(self, schema: Schema) -> "SubTable":
        schema.require(self.columns, f"sub-table {self.name!r}")
        return self

    def as_dict(self) -> dict:
        return {"name": self.name, "columns": list(self.columns)}


def make_subtables(schema: Schema, layouts) -> tuple[SubTable, ...]:
    """Build and validate sub-tables from ``(name, columns)`` pairs or a mapping."""
    out: list[SubTable] = []
    names: set[str] = set()
    items = layouts.items() if isinstance(layouts, Mapping) else layouts
    for item in items:
        if isinstance(item, SubTable):
            st = item.checked_against(schema)
        else:
            try:
                name, cols = item
            except (TypeError, ValueError) as exc:
                raise SchemaError(
                    f"sub-table layout {item!r} must be (name, columns)"
                ) from exc
            st = SubTable(name=name, columns=tuple(cols)).checked_against(schema)
        if st.name in names:
            raise SchemaError(f"duplicate sub-table name {st.name!r}")
        names.add(st.name)
        out.append(st)
    if not out:
        raise SchemaError("at least one sub-table is required")
    covered: set[str] = set()
    for st in out:
        covered.update(st.columns)
    missing = sorted(schema.column_set - covered)
    if missing:
        raise SchemaError(
            f"sub-tables must cover every column; missing {missing!r}"
        )
    return tuple(sorted(out, key=lambda st: (st.columns, st.name)))


@dataclass(frozen=True)
class Dependency:
    """A functional dependency ``X -> Y`` or a multivalued one ``X ->> Y``.

    The formal body of a multivalued dependency is
    ``X ->> Y | (R - X - Y)``; the complement is derived from the schema, so
    storing ``X`` and ``Y`` is enough.  For an MVD, ``X`` and ``Y`` must be
    disjoint (non-trivial, complete form).
    """

    kind: str
    left: tuple[str, ...]
    right: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.kind not in (FD, MVD):
            raise DependencyError(
                f"dependency kind must be 'FD' or 'MVD', got {self.kind!r}"
            )
        object.__setattr__(
            self, "left", _as_columns(self.left, "dependency left-hand side")
        )
        object.__setattr__(
            self, "right", _as_columns(self.right, "dependency right-hand side")
        )
        if not self.right:
            raise DependencyError(f"{self.kind} {self} has an empty right-hand side")
        if self.kind == MVD:
            overlap = set(self.left) & set(self.right)
            if overlap:
                raise DependencyError(
                    f"MVD {self} overlaps left and right on {sorted(overlap)!r}; "
                    "give the complete, non-trivial form X ->> Y with X and Y disjoint"
                )

    def checked_against(self, schema: Schema) -> "Dependency":
        try:
            schema.require(self.left, f"{self.kind} left-hand side")
            schema.require(self.right, f"{self.kind} right-hand side")
        except SchemaError as exc:
            raise DependencyError(str(exc)) from exc
        return self

    @property
    def here(self) -> frozenset[str]:
        return frozenset(self.left)

    @property
    def there(self) -> frozenset[str]:
        return frozenset(self.right)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "left": list(self.left),
            "right": list(self.right),
            "text": str(self),
        }

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        arrow = "->" if self.kind == FD else "->>"
        left = ",".join(self.left) or "{}"
        return f"{left} {arrow} {','.join(self.right)}"


def dependency_from(raw) -> Dependency:
    if isinstance(raw, Dependency):
        return raw
    if isinstance(raw, Mapping):
        try:
            return Dependency(
                kind=str(raw["kind"]).upper(),
                left=tuple(_many(raw["left"])),
                right=tuple(_many(raw["right"])),
            )
        except KeyError as exc:
            raise DependencyError(f"dependency {raw!r} is missing {exc}") from exc
    try:
        kind, left, right = raw
    except (TypeError, ValueError) as exc:
        raise DependencyError(
            f"dependency {raw!r} must be ('FD'|'MVD', left, right) or a mapping"
        ) from exc
    return Dependency(kind=str(kind).upper(), left=_many(left), right=_many(right))


def _many(value) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    try:
        return tuple(value)
    except TypeError as exc:
        raise DependencyError(f"column list {value!r} is not iterable") from exc


def make_dependencies(schema: Schema, raw) -> tuple[Dependency, ...]:
    """Build, validate, de-duplicate and canonically sort dependencies.

    Accepted forms::

        Dependency("FD", ("A",), ("B",))
        ("FD", "A", "B")                 # A -> B
        ("MVD", ("A", "B"), ("C",))      # A,B ->> C
        {"kind": "MVD", "left": "A", "right": ["B"]}
    """
    out: list[Dependency] = []
    seen: set[Dependency] = set()
    for item in raw:
        dep = dependency_from(item).checked_against(schema)
        if dep not in seen:
            seen.add(dep)
            out.append(dep)
    return tuple(sorted(out, key=lambda d: (d.kind, d.left, d.right)))


@dataclass(frozen=True)
class Instance:
    """A concrete relation instance over ``schema`` (a set of rows)."""

    schema: Schema
    rows: frozenset[tuple]

    @classmethod
    def from_rows(
        cls, schema: Schema, rows: Iterable[Mapping[str, object] | Sequence]
    ) -> "Instance":
        return cls(schema=schema, rows=frozenset(to_tuple(schema, r) for r in rows))

    @classmethod
    def from_dicts(cls, schema: Schema, rows: Iterable[Mapping[str, object]]) -> "Instance":
        return cls.from_rows(schema, rows)

    def project(self, columns: Iterable[str]) -> frozenset[tuple]:
        cols = tuple(sorted(columns))
        idx = [self.schema.columns.index(c) for c in cols]
        return frozenset(tuple(row[i] for i in idx) for row in self.rows)

    def rows_as_dicts(self) -> list[dict]:
        return [
            dict(zip(self.schema.columns, row)) for row in sorted(self.rows, key=_row_sort_key)
        ]


def _row_sort_key(row: Sequence) -> tuple:
    return tuple(repr(v) for v in row)


def to_tuple(schema: Schema, row: Mapping[str, object] | Sequence) -> tuple:
    if isinstance(row, Mapping):
        missing = [c for c in schema.columns if c not in row]
        if missing:
            raise DataError(f"row {row!r} is missing column(s) {missing!r}")
        extra = sorted(c for c in row if c not in schema.column_set)
        if extra:
            raise DataError(f"row {row!r} has unknown column(s) {extra!r}")
        tup = tuple(row[c] for c in schema.columns)
    else:
        try:
            tup = tuple(row)
        except TypeError as exc:
            raise DataError(f"row {row!r} is not a mapping or sequence") from exc
        if len(tup) != len(schema.columns):
            raise DataError(
                f"row {row!r} has {len(tup)} value(s) but the schema has "
                f"{len(schema.columns)}"
            )
    for v in tup:
        if v is None:
            raise DataError(f"row {row!r} contains NULL; NULLs are not supported")
        try:
            hash(v)
        except TypeError as exc:
            raise DataError(f"value {v!r} in row {row!r} is not hashable") from exc
    return tup
