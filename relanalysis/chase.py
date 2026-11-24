"""Tableau chase for functional and multivalued dependencies.

The chase is the classical decision procedure behind the lossless-join test.
Rows are dictionaries mapping every column to a *symbol*:

* ``a:C`` is the distinguished symbol of column ``C``.
* ``n:i:C`` is the null symbol contributed by the ``i``-th sub-table.

Rules, applied alternately until a fixpoint is reached:

* **FD** ``X -> Y``: if two rows agree on ``X``, unify their symbols in ``Y``
  (a null is replaced by the other symbol).  The substitution is applied to
  the entire tableau.
* **MVD** ``X ->> Y``: if two rows agree on ``X``, add the row that keeps
  ``X U Y`` from the first row and ``R - X - Y`` from the second, and the
  mirror image.

The decomposition is lossless for *every* instance satisfying the
dependencies exactly when an all-distinguished row -- the *witness* -- is
derived.  A row added by an MVD may first have to be merged by an FD before it
agrees with another row on a further MVD left-hand side; alternating both
rules to a fixpoint handles exactly that interleaving.

Determinism: dependencies are processed in canonical order and rows in id
order, so the same (canonically sorted) problem always yields the same chase.
Everything reportable is content-keyed or id-ordered, never dependent on the
order in which the caller supplied dependencies or sub-tables.

Termination: MVD steps only copy existing symbols and FD steps only merge
two distinct symbols into one, so the number of distinct symbols per column
never grows and the tableau is bounded by ``(len(subtables) + 1) ** len(schema)``.
The row budget is therefore a safety net, not part of the semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .model import FD, MVD, Dependency, Schema
from .tableau import distinguished, is_distinguished

DEFAULT_ROW_BUDGET = 200_000


def content_key(row: Mapping[str, object], columns: Sequence[str]) -> tuple:
    return tuple(row[c] for c in columns)


def _agree(row1: Mapping, row2: Mapping, cols: Iterable[str]) -> bool:
    return all(row1[c] == row2[c] for c in cols)


def _choose_representative(a: str, b: str) -> tuple[str, str]:
    """Pick the surviving symbol: distinguished beats null, then lexicographic.

    Two *different* distinguished symbols never occur in the same column
    (they are column tagged), so the choice is always well defined.
    """
    a_dist, b_dist = is_distinguished(a), is_distinguished(b)
    if a_dist != b_dist:
        return (a, b) if a_dist else (b, a)
    return (a, b) if repr(a) <= repr(b) else (b, a)


def normalize_substitutions(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Turn value-equivalence pairs into ``old symbol -> representative``."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    for a, b in pairs:
        if a == b:
            continue
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        rep, other = _choose_representative(ra, rb)
        parent[other] = rep
    return {
        symbol: find(symbol)
        for symbol in list(parent)
        if find(symbol) != symbol
    }


@dataclass
class ChaseRun:
    schema: Schema
    rows: dict[int, dict] = field(default_factory=dict)
    meta: dict[int, dict] = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    witness_row_id: int | None = None
    iterations: int = 0
    closed: bool = False

    def row_dicts(self) -> list[dict]:
        return [dict(self.rows[i]) for i in sorted(self.rows)]

    def witness_row(self) -> dict | None:
        if self.witness_row_id is None:
            return None
        return dict(self.rows[self.witness_row_id])


def find_witness(schema: Schema, rows: Mapping[int, Mapping]) -> int | None:
    for row_id in sorted(rows):
        row = rows[row_id]
        if all(row[c] == distinguished(c) for c in schema.columns):
            return row_id
    return None


def chase_tableau(
    schema: Schema,
    seeds: Sequence[Mapping],
    subtable_names: Sequence[str],
    dependencies: Sequence[Dependency],
    *,
    stop_at_witness: bool = True,
    row_budget: int = DEFAULT_ROW_BUDGET,
) -> ChaseRun:
    columns = list(schema.columns)
    rows: dict[int, dict] = {}
    meta: dict[int, dict] = {}
    for index, seed in enumerate(seeds):
        rows[index] = {c: seed[c] for c in columns}
        meta[index] = {
            "origin": "seed",
            "subtable": subtable_names[index],
            "created_step": None,
            "fd_merge_steps": [],
        }
    fds = [d for d in dependencies if d.kind == FD]
    mvds = [d for d in dependencies if d.kind == MVD]
    run = ChaseRun(schema=schema, rows=rows, meta=meta)

    if stop_at_witness:
        run.witness_row_id = find_witness(schema, rows)
        if run.witness_row_id is not None:
            return run

    next_id = len(seeds)
    while True:
        run.iterations += 1
        if len(rows) > row_budget:  # pragma: no cover - safety net
            raise RuntimeError(
                f"chase exceeded the row budget ({row_budget}); this cannot happen "
                "for a fixed symbol set and indicates a bug"
            )

        fd_hit = _next_fd(rows, columns, fds)
        if fd_hit is not None:
            dep, first, second, subs = fd_hit
            affected = []
            for row_id in sorted(rows):
                row = rows[row_id]
                changed = False
                for col in columns:
                    value = row[col]
                    if value in subs and subs[value] != value:
                        row[col] = subs[value]
                        changed = True
                if changed:
                    affected.append(row_id)
                    meta[row_id]["fd_merge_steps"].append(len(run.steps) + 1)
            run.steps.append(
                {
                    "seq": len(run.steps) + 1,
                    "kind": "fd-merge",
                    "dependency": dep.as_dict(),
                    "dependency_text": str(dep),
                    "parents": [first, second],
                    "substitutions": dict(sorted(subs.items())),
                    "affected_rows": sorted(affected),
                }
            )
            if stop_at_witness:
                witness = find_witness(schema, rows)
                if witness is not None:
                    run.witness_row_id = witness
                    return run
            continue

        applications = _next_mvd_batch(schema, rows, columns, mvds)
        if applications:
            for dep, first, second, direction, new_row in applications:
                seq = len(run.steps) + 1
                rows[next_id] = new_row
                meta[next_id] = {
                    "origin": "mvd",
                    "dependency": str(dep),
                    "parents": [first, second],
                    "created_step": seq,
                    "fd_merge_steps": [],
                }
                run.steps.append(
                    {
                        "seq": seq,
                        "kind": "mvd-swap",
                        "dependency": dep.as_dict(),
                        "dependency_text": str(dep),
                        "parents": [first, second],
                        "direction": direction,
                        "new_row_id": next_id,
                        "row": dict(new_row),
                    }
                )
                next_id += 1
                if stop_at_witness:
                    witness = find_witness(schema, rows)
                    if witness is not None:
                        run.witness_row_id = witness
                        return run
            continue

        run.closed = True
        run.witness_row_id = find_witness(schema, rows)
        return run


def _next_fd(rows: Mapping[int, dict], columns: Sequence[str], fds: Sequence[Dependency]):
    for dep in fds:
        buckets: dict[tuple, list[int]] = {}
        for row_id in sorted(rows):
            buckets.setdefault(tuple(rows[row_id][c] for c in dep.left), []).append(row_id)
        for key in sorted(buckets, key=repr):
            ids = buckets[key]
            if len(ids) < 2:
                continue
            first = ids[0]
            for second in ids[1:]:
                pairs = [
                    (rows[first][col], rows[second][col])
                    for col in dep.right
                    if rows[first][col] != rows[second][col]
                ]
                if not pairs:
                    continue
                subs = normalize_substitutions(pairs)
                if subs:
                    return dep, first, second, subs
    return None


def _next_mvd_batch(
    schema: Schema, rows: Mapping[int, dict], columns: Sequence[str], mvds: Sequence[Dependency]
) -> list[tuple]:
    """All MVD swaps that are new, grouped in canonical order.

    Batching keeps the fixpoint identical (the swaps only copy existing
    symbols) while avoiding a quadratic rescan after every single addition.
    The order is canonical: dependency order, then ascending row ids.
    """
    existing = {content_key(row, columns) for row in rows.values()}
    out: list[tuple] = []
    for dep in mvds:
        union = dep.here | dep.there
        buckets: dict[tuple, list[int]] = {}
        for row_id in sorted(rows):
            buckets.setdefault(tuple(rows[row_id][c] for c in dep.left), []).append(row_id)
        for key in sorted(buckets, key=repr):
            ids = buckets[key]
            if len(ids) < 2:
                continue
            for pos in range(len(ids)):
                for other in ids[pos + 1 :]:
                    first, second = ids[pos], other
                    for direction, keep_id, take_id in (
                        ("first-keeps", first, second),
                        ("second-keeps", second, first),
                    ):
                        keep, take = rows[keep_id], rows[take_id]
                        new_row = {
                            col: (keep[col] if col in union else take[col])
                            for col in columns
                        }
                        content = content_key(new_row, columns)
                        if content in existing:
                            continue
                        existing.add(content)
                        out.append((dep, first, second, direction, new_row))
    return out


def interleaved_swaps(meta: Mapping[int, dict], steps: Sequence[Mapping]) -> list[dict]:
    """MVD steps whose parent row was created by an MVD and later FD-merged.

    This is the ``MVD -> FD-merge -> MVD`` pattern the derivation must expose.
    A parent qualifies when its creating MVD step precedes an FD merge that
    changed it, which in turn precedes the step that uses it.
    """
    observations = []
    step_by_seq = {step["seq"]: step for step in steps}
    for step in steps:
        if step["kind"] != "mvd-swap":
            continue
        parents = []
        for parent in step["parents"]:
            entry = meta.get(parent, {})
            created = entry.get("created_step")
            if created is None:
                continue
            merges = [s for s in entry.get("fd_merge_steps", []) if created < s < step["seq"]]
            if merges:
                parents.append(
                    {
                        "row_id": parent,
                        "created_by_step": created,
                        "fd_merge_steps": merges,
                        "created_by": step_by_seq[created]["dependency_text"],
                        "merges": [
                            {
                                "step": s,
                                "dependency": step_by_seq[s]["dependency_text"],
                                "substitutions": step_by_seq[s]["substitutions"],
                            }
                            for s in merges
                        ],
                    }
                )
        if parents:
            observations.append(
                {
                    "step": step["seq"],
                    "kind": "mvd-after-fd-merge-of-mvd-row",
                    "dependency": step["dependency_text"],
                    "parents": list(step["parents"]),
                    "merged_parents": parents,
                }
            )
    return observations
