"""In-memory table storage and equality-key lookup indexes."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Hashable

from .model import COLUMNS, DuplicateRowError, InvalidRowError, Row, TABLES


def row_from_tuple(table: str, spec: Row | tuple | list) -> Row:
    if isinstance(spec, Row):
        if spec.table != table:
            raise InvalidRowError(f"expected a {table} row, got {spec.table}")
        return spec
    if not isinstance(spec, (tuple, list)):
        raise InvalidRowError(f"{table} row must be a Row, tuple, or list")
    if len(spec) == 3:
        row_id, x, y = spec
        multiplicity = 1
    elif len(spec) == 4:
        row_id, x, y, multiplicity = spec
    else:
        raise InvalidRowError(
            f"{table} row tuple must be (row_id, x, y) or (row_id, x, y, multiplicity)"
        )
    return Row(table, row_id, (x, y), multiplicity)


class TableSet:
    """Immutable-after-commit collection of the three physical tables."""

    def __init__(
        self,
        rows: dict[str, dict[Hashable, Row]] | None = None,
    ) -> None:
        self.rows: dict[str, dict[Hashable, Row]] = {t: {} for t in TABLES}
        if rows:
            for table in TABLES:
                self.rows[table].update(rows.get(table, {}))

        # Equality indexes.  Values are lists of physical Row objects.
        self.r_ab: dict[tuple[Any, Any], list[Row]] = {}
        self.s_bc: dict[tuple[Any, Any], list[Row]] = {}
        self.s_b: dict[Any, list[Row]] = {}
        self.t_ca: dict[tuple[Any, Any], list[Row]] = {}
        self.t_c: dict[Any, list[Row]] = {}
        self.r_a: dict[Any, list[Row]] = {}
        self.r_b: dict[Any, list[Row]] = {}

        for row in self.rows["R"].values():
            a, b = row.values
            self.r_ab.setdefault((a, b), []).append(row)
            self.r_a.setdefault(a, []).append(row)
            self.r_b.setdefault(b, []).append(row)
        for row in self.rows["S"].values():
            b, c = row.values
            self.s_bc.setdefault((b, c), []).append(row)
            self.s_b.setdefault(b, []).append(row)
        for row in self.rows["T"].values():
            c, a = row.values
            self.t_ca.setdefault((c, a), []).append(row)
            self.t_c.setdefault(c, []).append(row)

    def copy_mutable_rows(self) -> dict[str, dict[Hashable, Row]]:
        return {table: dict(self.rows[table]) for table in TABLES}

    def get(self, table: str, row_id: Hashable) -> Row | None:
        return self.rows[table].get(row_id)

    def require(self, table: str, row_id: Hashable) -> Row:
        try:
            return self.rows[table][row_id]
        except KeyError as exc:
            from .model import RowNotFoundError

            raise RowNotFoundError(f"{table} row {row_id!r} does not exist") from exc

    def all_rows(self, table: str) -> list[Row]:
        return list(self.rows[table].values())

    def closed_keys(self) -> set[tuple[Any, Any, Any]]:
        """Return all (a,b,c) keys with at least one row on all three edges."""
        keys: set[tuple[Any, Any, Any]] = set()
        for r in self.rows["R"].values():
            a, b = r.values
            for s in self.s_b.get(b, ()):  # S(b,c)
                c = s.values[1]
                if self.t_ca.get((c, a)):
                    keys.add((a, b, c))
        return keys

    def closed_keys_for_row(self, row: Row) -> set[tuple[Any, Any, Any]]:
        """Return closed triangle keys containing this physical edge."""
        keys: set[tuple[Any, Any, Any]] = set()
        if row.table == "R":
            a, b = row.values
            for s in self.s_b.get(b, ()):  # S(b,c)
                c = s.values[1]
                if self.t_ca.get((c, a)):
                    keys.add((a, b, c))
        elif row.table == "S":
            b, c = row.values
            for r in self.r_b.get(b, ()):  # R(a,b)
                ra = r.values[0]
                if self.t_ca.get((c, ra)):
                    keys.add((ra, b, c))
        else:  # T(c,a)
            c, a = row.values
            for r in self.r_a.get(a, ()):  # R(a,b)
                b = r.values[1]
                if self.s_bc.get((b, c)):
                    keys.add((a, b, c))
        return keys


def initial_tables(
    R: Iterable[Row | tuple] | None = None,
    S: Iterable[Row | tuple] | None = None,
    T: Iterable[Row | tuple] | None = None,
) -> TableSet:
    rows: dict[str, dict[Hashable, Row]] = {t: {} for t in TABLES}
    for table, specs in (("R", R or ()), ("S", S or ()), ("T", T or ())):
        for spec in specs:
            row = row_from_tuple(table, spec)
            if row.row_id in rows[table]:
                raise DuplicateRowError(f"duplicate {table} row_id {row.row_id!r}")
            rows[table][row.row_id] = row
    return TableSet(rows)
