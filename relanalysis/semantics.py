"""Direct semantics: dependency satisfaction and natural join for instances.

These routines are *operational* and deliberately independent of the chase;
:mod:`relanalysis.evidence` carries its own copy so that an evidence document
can be validated without trusting this module.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from .model import FD, Dependency, Schema, SubTable


def tuple_set(schema: Schema, rows: Iterable[Mapping]) -> set[tuple]:
    return {tuple(row[c] for c in schema.columns) for row in rows}


def project_tuples(
    schema: Schema, columns: Sequence[str], rows: Iterable[Mapping]
) -> set[tuple]:
    cols = tuple(sorted(columns))
    return {tuple(row[c] for c in cols) for row in rows}


def satisfies_dependencies(
    schema: Schema, rows: Sequence[Mapping], dependencies: Sequence[Dependency]
) -> bool:
    """Fast boolean check: does the instance satisfy every dependency?

    Short-circuits on the first violation and avoids building witness
    dictionaries, which matters inside minimisation loops.
    """
    columns = tuple(schema.columns)
    position = {column: index for index, column in enumerate(columns)}
    tuples = [tuple(row[column] for column in columns) for row in rows]
    present = set(tuples)
    column_count = len(columns)
    for dep in dependencies:
        left_idx = tuple(position[column] for column in dep.left)
        buckets: dict[tuple, list[tuple]] = {}
        for row in tuples:
            buckets.setdefault(tuple(row[k] for k in left_idx), []).append(row)
        if dep.kind == FD:
            right_idx = tuple(position[column] for column in dep.right)
            for ids in buckets.values():
                if len(ids) < 2:
                    continue
                first = ids[0]
                for other in ids[1:]:
                    if any(first[k] != other[k] for k in right_idx):
                        return False
            continue
        keep_idx = set(position[column] for column in dep.here | dep.there)
        keep_mask = tuple(k in keep_idx for k in range(column_count))
        for ids in buckets.values():
            if len(ids) < 2:
                continue
            for pos in range(len(ids)):
                for other in ids[pos + 1 :]:
                    t1, t2 = ids[pos], other
                    if any(t1[k] != t2[k] for k in range(column_count)):
                        if tuple(
                            t1[k] if keep_mask[k] else t2[k] for k in range(column_count)
                        ) not in present:
                            return False
                        if tuple(
                            t2[k] if keep_mask[k] else t1[k] for k in range(column_count)
                        ) not in present:
                            return False
    return True


def dependency_violations(
    schema: Schema, rows: Sequence[Mapping], dependencies: Sequence[Dependency]
) -> list[dict]:
    """Return at most one witness of violation per broken dependency.

    * FD ``X -> Y``: two rows agree on ``X`` but differ on some column of ``Y``.
    * MVD ``X ->> Y``: two rows agree on ``X`` but one of the two swapped rows
      (``X U Y`` from one row, the remaining columns from the other) is absent.

    Rows are bucketed by their ``X`` projection, so the work is linear in the
    number of rows plus the number of same-``X`` row pairs.
    """
    columns = tuple(schema.columns)
    position = {column: index for index, column in enumerate(columns)}
    tuples = [tuple(row[column] for column in columns) for row in rows]
    present = set(tuples)
    violations: list[dict] = []
    column_count = len(columns)

    for dep in dependencies:
        left_idx = tuple(position[column] for column in dep.left)
        right_idx = tuple(position[column] for column in dep.right)
        buckets: dict[tuple, list[int]] = {}
        for index, row in enumerate(tuples):
            buckets.setdefault(tuple(row[k] for k in left_idx), []).append(index)

        if dep.kind == FD:
            found = None
            for ids in buckets.values():
                if len(ids) < 2:
                    continue
                # rows in the same X-bucket must share one Y projection
                representative = tuple(tuples[ids[0]][k] for k in right_idx)
                for index in ids[1:]:
                    if tuple(tuples[index][k] for k in right_idx) != representative:
                        found = (ids[0], index)
                        break
                if found:
                    break
            if found:
                i, j = found
                violations.append(
                    {
                        "dependency": dep.as_dict(),
                        "rows": [i, j],
                        "detail": (
                            f"rows {i} and {j} agree on {list(dep.left)} "
                            f"but differ on {list(dep.right)}"
                        ),
                    }
                )
            continue

        keep_idx = set(position[column] for column in dep.here | dep.there)
        keep_mask = tuple(k in keep_idx for k in range(column_count))
        found = None
        for ids in buckets.values():
            if len(ids) < 2:
                continue
            for pos in range(len(ids)):
                for other in ids[pos + 1 :]:
                    first, second = ids[pos], other
                    t1, t2 = tuples[first], tuples[second]
                    swapped = tuple(
                        t1[k] if keep_mask[k] else t2[k] for k in range(column_count)
                    )
                    if swapped not in present:
                        found = (first, second, True, swapped)
                        break
                    swapped = tuple(
                        t2[k] if keep_mask[k] else t1[k] for k in range(column_count)
                    )
                    if swapped not in present:
                        found = (first, second, False, swapped)
                        break
                if found:
                    break
            if found:
                break
        if found:
            i, j, first_keeps, swapped = found
            violations.append(
                {
                    "dependency": dep.as_dict(),
                    "rows": [i, j],
                    "missing_row": list(swapped),
                    "expects": "X U Y from row " + (str(i) if first_keeps else str(j))
                    + " and R - X - Y from the other",
                    "detail": (
                        f"rows {i} and {j} agree on {list(dep.left)} but the "
                        f"MVD-swapped row {list(swapped)} is absent"
                    ),
                }
            )
    return violations


def join_relation(
    schema: Schema, subtables: Sequence[SubTable], rows: Sequence[Mapping]
) -> set[tuple]:
    """Natural join of the sub-table projections, as tuples over ``schema``."""
    rows = list(rows)
    if not rows:
        return set()
    if not subtables:
        return set()
    ordered = sorted(subtables, key=lambda s: (len(s.columns), s.columns, s.name))
    current: dict[tuple, dict] = {}
    covered: tuple[str, ...] = ()
    # seed with the first projection
    first = ordered[0]
    for tup in sorted({tuple(row[c] for c in first.columns) for row in rows}):
        current[tup] = dict(zip(first.columns, tup))
    covered = first.columns
    for sub in ordered[1:]:
        projection = {tuple(row[c] for c in sub.columns) for row in rows}
        nxt: dict[tuple, dict] = {}
        for values in current.values():
            for tup in projection:
                candidate = dict(values)
                ok = True
                for col, value in zip(sub.columns, tup):
                    if col in candidate and candidate[col] != value:
                        ok = False
                        break
                    candidate[col] = value
                if not ok:
                    continue
                new_covered = tuple(sorted(set(covered) | set(sub.columns)))
                key = tuple(candidate[c] for c in new_covered)
                nxt.setdefault(key, candidate)
        current = nxt
        covered = tuple(sorted(set(covered) | set(sub.columns)))
        if not current:
            return set()
    return {tuple(values[c] for c in schema.columns) for values in current.values()}


def join_extra(
    schema: Schema, subtables: Sequence[SubTable], rows: Sequence[Mapping]
) -> set[tuple]:
    return join_relation(schema, subtables, rows) - tuple_set(schema, rows)


def projection_matrix(
    schema: Schema, subtables: Sequence[SubTable], rows: Sequence[Mapping]
) -> dict[str, list[list]]:
    return {
        sub.name: [list(t) for t in sorted(project_tuples(schema, sub.columns, rows))]
        for sub in sorted(subtables, key=lambda s: s.name)
    }


def sources_for_extra_row(
    schema: Schema,
    subtables: Sequence[SubTable],
    rows: Sequence[Mapping],
    extra: Sequence,
) -> dict[str, list[dict]]:
    """For every sub-table, the original rows whose projection yields ``extra``."""
    want = dict(zip(schema.columns, extra))
    out: dict[str, list[dict]] = {}
    for sub in sorted(subtables, key=lambda s: s.name):
        matches = []
        for index, row in enumerate(rows):
            if all(row[c] == want[c] for c in sub.columns):
                matches.append(
                    {
                        "instance_row": index,
                        "row": {c: row[c] for c in schema.columns},
                        "projection": {c: row[c] for c in sub.columns},
                    }
                )
        out[sub.name] = matches
    return out
