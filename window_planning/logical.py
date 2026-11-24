"""Path-independent materialisation of window logical values.

The *logical* result of a window depends only on its specification and the
current row values (which may themselves be generated columns of earlier
windows) -- never on the physical order an executor happens to pick. This
module materialises those values from a canonical decorated sort so that:

* the planner can measure sorting costs on keys that reference generated
  columns before any physical step has run;
* the verification reference implementation has a small, auditable primitive.

The executor later re-materialises values while replaying the chosen plan;
the verification entry point asserts both computations agree row by row.
"""

from __future__ import annotations

from .spec import ROWNO_COLUMN, WindowFunction, WindowSpec
from .validation import topo_order, validate_specs


class _Rev:
    """Reverse-order wrapper for DESC ordering keys."""

    __slots__ = ("value",)

    def __init__(self, value: object):
        self.value = value

    def __lt__(self, other: "_Rev") -> bool:
        return other.value < self.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Rev) and other.value == self.value


class _NullRank:
    """Encode NULLS FIRST / LAST as an ordinary comparable pair."""

    __slots__ = ("null_flag", "value")

    def __init__(self, value: object, null_first: bool, desc: bool):
        is_null = value is None
        if null_first:
            self.null_flag = 0 if is_null else 1
        else:
            self.null_flag = 1 if is_null else 0
        if is_null:
            self.value = 0
        elif desc:
            self.value = _Rev(value)
        else:
            self.value = value

    def __lt__(self, other: "_NullRank") -> bool:
        if self.null_flag != other.null_flag:
            return self.null_flag < other.null_flag
        return self.value < other.value

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, _NullRank)
            and self.null_flag == other.null_flag
            and self.value == other.value
        )


def _order_decoration(row: dict, spec: WindowSpec) -> tuple[object, ...]:
    decoration = []
    for key in spec.order_by:
        decoration.append(
            _NullRank(
                row[key.column],
                null_first=key.nulls.value == "FIRST",
                desc=key.direction.value == "DESC",
            )
        )
    return tuple(decoration)


def _partition_decoration(row: dict, spec: WindowSpec) -> tuple[object, ...]:
    return tuple(row[column] for column in spec.partition_by)


def materialise_logical(
    rows: list[dict], specs: list[WindowSpec]
) -> dict[str, list[object]]:
    """Compute every window's output column in a valid topological order.

    Rows are mutated in place: each generated column becomes a normal key that
    dependent windows can sort or partition on. The returned mapping is the
    same data grouped by window name for convenient assertions.
    """
    base_columns = (
        frozenset(rows[0]) - {ROWNO_COLUMN} if rows else frozenset()
    )
    dependencies = validate_specs(specs, base_columns)
    ordered_specs = topo_order(specs, dependencies)
    columns: dict[str, list[object]] = {
        spec.name: [None] * len(rows) for spec in specs
    }

    for spec in ordered_specs:
        indices = sorted(
            range(len(rows)),
            key=lambda i: (
                _partition_decoration(rows[i], spec),
                _order_decoration(rows[i], spec),
                i,  # canonical physical tail: original row number
            ),
        )
        _fill_one(rows, indices, spec, columns)
        for i, value in enumerate(columns[spec.name]):
            rows[i][spec.name] = value
    return columns


def _fill_one(
    rows: list[dict],
    indices: list[int],
    spec: WindowSpec,
    columns: dict[str, list[object]],
) -> None:
    start = 0
    while start < len(indices):
        partition_signature = _partition_decoration(rows[indices[start]], spec)
        end = start + 1
        while end < len(indices):
            if _partition_decoration(rows[indices[end]], spec) != partition_signature:
                break
            end += 1

        row_number = 0
        dense_rank = 0
        cumulative = 0
        cursor = start
        while cursor < end:
            order_signature = _order_decoration(rows[indices[cursor]], spec)
            peer_end = cursor + 1
            while peer_end < end:
                if _order_decoration(rows[indices[peer_end]], spec) != order_signature:
                    break
                peer_end += 1

            dense_rank += 1
            cumulative += peer_end - cursor
            for position in range(cursor, peer_end):
                row_number += 1
                row_index = indices[position]
                if spec.function is WindowFunction.ROW_NUMBER:
                    columns[spec.name][row_index] = row_number
                elif spec.function is WindowFunction.DENSE_RANK:
                    columns[spec.name][row_index] = dense_rank
                else:
                    columns[spec.name][row_index] = cumulative
            cursor = peer_end
        start = end
