"""High-level analysis: decide lossless join and build checkable evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from . import semantics
from .chase import chase_tableau, interleaved_swaps
from .model import (
    FD,
    Dependency,
    Schema,
    SubTable,
    make_dependencies,
    make_subtables,
)
from .tableau import build_seeds

EVIDENCE_FORMAT = "relanalysis-evidence/v1"


@dataclass
class Analysis:
    """The verdict plus a self-contained, independently checkable evidence doc."""

    schema: Schema
    subtables: tuple[SubTable, ...]
    dependencies: tuple[Dependency, ...]
    lossless: bool
    evidence: dict

    @property
    def verdict(self) -> str:
        return "lossless" if self.lossless else "lossy"

    def explain(self) -> str:
        return explain_evidence(self.evidence)

    def to_dict(self) -> dict:
        return dict(self.evidence)


def analyze(schema, subtables, dependencies, *, minimize: bool = True) -> Analysis:
    """Analyse a decomposition and return its verdict plus evidence.

    Parameters accept either rich objects or plain declarations::

        analyze(
            ["A", "B", "C", "D"],
            [("R1", ["A", "B"]), ("R2", ["B", "C"]), ("R3", ["C", "D"])],
            [("MVD", ("A",), ("B",)), ("FD", ("C",), ("D",))],
        )
    """
    schema = schema if isinstance(schema, Schema) else Schema.from_columns(schema)
    subtables = make_subtables(schema, subtables)
    dependencies = make_dependencies(schema, dependencies)

    seeds, _ = build_seeds(schema, subtables)
    names = [sub.name for sub in subtables]
    run = chase_tableau(
        schema, seeds, names, dependencies, stop_at_witness=True
    )

    method = {
        "tableau": [
            {
                "row_id": index,
                "subtable": subtables[index].name,
                "symbols": dict(seeds[index]),
            }
            for index in range(len(seeds))
        ],
        "canonical_subtables": [sub.name for sub in subtables],
        "canonical_dependencies": [str(dep) for dep in dependencies],
    }

    if run.witness_row_id is not None:
        evidence = _lossless_evidence(
            schema, subtables, dependencies, seeds, run, method
        )
    else:
        evidence = _lossy_evidence(
            schema, subtables, dependencies, seeds, names, method, minimize=minimize
        )
    return Analysis(
        schema=schema,
        subtables=subtables,
        dependencies=dependencies,
        lossless=evidence["verdict"] == "lossless",
        evidence=evidence,
    )


def analyze_problem(problem: Mapping) -> Analysis:
    """Analyse a JSON-style problem document."""
    try:
        schema = problem["schema"]
        subtables = [(st["name"], st["columns"]) for st in problem["subtables"]]
        dependencies = problem["dependencies"]
    except KeyError as exc:
        raise KeyError(f"problem document is missing {exc}") from exc
    return analyze(schema, subtables, dependencies)


# --------------------------------------------------------------------------
# lossless evidence
# --------------------------------------------------------------------------


def _lossless_evidence(schema, subtables, dependencies, seeds, run, method) -> dict:
    observations = interleaved_swaps(run.meta, run.steps)
    witness = run.witness_row()
    evidence = {
        "format": EVIDENCE_FORMAT,
        "schema": list(schema.columns),
        "subtables": [sub.as_dict() for sub in subtables],
        "dependencies": [dep.as_dict() for dep in dependencies],
        "verdict": "lossless",
        "method": method,
        "lossless": {
            "seed_rows": [
                {
                    "row_id": index,
                    "subtable": subtables[index].name,
                    "symbols": dict(seeds[index]),
                }
                for index in range(len(seeds))
            ],
            "steps": run.steps,
            "witness_row_id": run.witness_row_id,
            "witness_row": witness,
            "final_tableau": [
                {"row_id": row_id, "symbols": dict(run.rows[row_id])}
                for row_id in sorted(run.rows)
            ],
            "interleavings": observations,
            "argument": (
                "The chase of the decomposition tableau derived the "
                "all-distinguished row "
                f"{witness}; by the chase characterisation the decomposition "
                "is lossless for every instance that satisfies the dependencies."
            ),
        },
        "counterexample": None,
    }
    return evidence


# --------------------------------------------------------------------------
# lossy evidence: finite counterexample
# --------------------------------------------------------------------------


def _lossy_evidence(
    schema, subtables, dependencies, seeds, names, method, *, minimize
) -> dict:
    full = chase_tableau(schema, seeds, names, dependencies, stop_at_witness=False)
    columns = list(schema.columns)
    unique: dict[tuple, dict] = {}
    for row in full.row_dicts():
        unique.setdefault(tuple(row[c] for c in columns), {c: row[c] for c in columns})
    tableau_instance = [unique[key] for key in sorted(unique)]

    target = tuple(f"a:{col}" for col in columns)
    assert target not in {tuple(r[c] for c in columns) for r in tableau_instance}

    rows, origin = _find_small_counterexample(schema, subtables, dependencies, target)
    if rows is None:
        rows, origin = tableau_instance, "chased-tableau"
    if minimize:
        rows = _minimize(schema, subtables, dependencies, rows, target)
    rows = _canonical_relabel(schema, subtables, rows, target)
    rows = sorted(rows, key=_row_sort_key)

    extras = sorted(
        semantics.join_relation(schema, subtables, rows)
        - semantics.tuple_set(schema, rows),
        key=_row_sort_key,
    )
    extra_list = []
    for extra in extras:
        extra_list.append(
            {
                "row": dict(zip(columns, extra)),
                "suppliers": semantics.sources_for_extra_row(
                    schema, subtables, rows, extra
                ),
            }
        )
    dep_check = semantics.dependency_violations(schema, rows, dependencies)
    projections = semantics.projection_matrix(schema, subtables, rows)
    join_rows = sorted(
        (
            dict(zip(columns, tup))
            for tup in semantics.join_relation(schema, subtables, rows)
        ),
        key=_row_sort_key,
    )
    target_present = any(
        tuple(extra["row"][c] for c in columns) == target for extra in extra_list
    )
    return {
        "format": EVIDENCE_FORMAT,
        "schema": columns,
        "subtables": [sub.as_dict() for sub in subtables],
        "dependencies": [dep.as_dict() for dep in dependencies],
        "verdict": "lossy",
        "method": method,
        "lossless": None,
        "counterexample": {
            "instance": rows,
            "instance_size": len(rows),
            "construction": origin,
            "minimized": bool(minimize),
            "dependency_check": {"satisfied": not dep_check, "violations": dep_check},
            "projections": projections,
            "join_rows": join_rows,
            "extra_rows": extra_list,
            "target_row": dict(zip(columns, target)),
            "target_row_is_extra": target_present,
            "argument": (
                "The finite instance satisfies every dependency (re-checked "
                "operationally).  The natural join of its sub-table projections "
                f"contains {len(extras)} row(s) absent from the instance"
                + (
                    ", among them the all-distinguished chase row "
                    f"{dict(zip(columns, target))}"
                    if target_present
                    else ""
                )
                + ".  Hence the decomposition is not lossless."
            ),
        },
    }


def _canonical_relabel(schema, subtables, rows, target):
    """Injectively relabel values so one extra join row is the marker row.

    Values are compared only for equality, so an injective renaming preserves
    both dependency satisfaction and the join / instance difference.  The
    first extra join row is renamed onto ``target`` (``a:A``, ``a:B``, ...),
    which makes the counterexample visibly mirror the failed chase: that row
    is supplied by one original row per sub-table.
    """
    columns = list(schema.columns)
    if not rows:
        return rows
    extras = sorted(
        semantics.join_relation(schema, subtables, rows) - semantics.tuple_set(schema, rows),
        key=_row_sort_key,
    )
    if not extras:
        return rows
    chosen = dict(zip(columns, extras[0]))
    mapping: dict = {}
    # marker names first
    for column in columns:
        mapping.setdefault(chosen[column], f"a:{column}")
    # keep an injective mapping for everything else
    counter = 0
    for row in rows:
        for column in columns:
            value = row[column]
            if value in mapping:
                continue
            while True:
                counter += 1
                name = f"d{counter}"
                if name not in mapping.values():
                    break
            mapping[value] = name
    renamed = [{c: mapping[row[c]] for c in columns} for row in rows]
    # de-duplicate: set semantics apply to instances
    renamed = list({tuple(r[c] for c in columns): r for r in renamed}.values())
    # sanity: the marker row must be derived by the join but not present
    marker = tuple(f"a:{c}" for c in columns)
    assert marker not in {tuple(r[c] for c in columns) for r in renamed}
    assert marker in semantics.join_relation(schema, subtables, renamed)
    return renamed


def _find_small_counterexample(schema, subtables, dependencies, target, attempts=400):
    """Bounded randomized search for a *small* constant-valued counterexample.

    Every candidate is closed under exactly the MVD rules and rejected when an
    FD is broken; acceptance then requires the join to be strictly larger.
    Nothing is trusted from the search itself — the caller re-verifies the
    returned instance, and a failure simply falls back to the chased tableau.
    """
    import random

    columns = list(schema.columns)
    pool = {column: [f"{column}{i}" for i in range(3)] for column in columns}
    rng = random.Random(_search_seed(schema, subtables, dependencies))
    fd_only = [dep for dep in dependencies if dep.kind == FD]
    best = None
    for _ in range(attempts):
        size = rng.choice([2, 3, 4])
        seed_rows = [{c: rng.choice(pool[c]) for c in columns} for _ in range(size)]
        seed_rows = list({tuple(row[c] for c in columns): row for row in seed_rows}.values())
        if len(seed_rows) < 2:
            continue
        closed = _close_mvds(schema, seed_rows, dependencies, cap=16)
        if closed is None or len(closed) > 16:
            continue
        keys = {tuple(row[c] for c in columns) for row in closed}
        if target in keys:
            continue
        if semantics.dependency_violations(schema, closed, fd_only):
            continue
        if not semantics.join_relation(schema, subtables, closed) > keys:
            continue
        if best is None or len(closed) < len(best):
            best = closed
            if len(best) <= 2:
                break
    return best, "small-search" if best is not None else "chased-tableau"


def _search_seed(schema, subtables, dependencies) -> int:
    import hashlib
    import json

    payload = json.dumps(
        {
            "schema": sorted(schema.columns),
            "subtables": sorted([list(st.columns) for st in subtables]),
            "dependencies": sorted(
                [[dep.kind, list(dep.left), list(dep.right)] for dep in dependencies]
            ),
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _close_mvds(schema, rows, dependencies, *, cap: int):
    """Close a candidate instance under the MVD rules only; ``None`` on overflow."""
    columns = list(schema.columns)
    out = [dict(row) for row in rows]
    present = {tuple(row[c] for c in columns) for row in out}
    out = list({tuple(row[c] for c in columns): row for row in out}.values())
    mvds = [dep for dep in dependencies if dep.kind == "MVD"]
    changed = True
    while changed:
        changed = False
        for dep in mvds:
            union = dep.here | dep.there
            buckets: dict[tuple, list[int]] = {}
            for index in range(len(out)):
                buckets.setdefault(tuple(out[index][c] for c in dep.left), []).append(index)
            for ids in buckets.values():
                if len(ids) < 2:
                    continue
                for pos in range(len(ids)):
                    for other in ids[pos + 1 :]:
                        first, second = ids[pos], other
                        for keep, take in ((first, second), (second, first)):
                            candidate = {
                                c: (out[keep][c] if c in union else out[take][c])
                                for c in columns
                            }
                            key = tuple(candidate[c] for c in columns)
                            if key in present:
                                continue
                            present.add(key)
                            out.append(candidate)
                            changed = True
                            if len(out) > cap:
                                return None
                        if changed:
                            break
                    if changed:
                        break
                if changed:
                    break
            if changed:
                break
    return out


def _minimize(schema, subtables, dependencies, rows, target):
    """Greedily drop rows while the counterexample property survives.

    Every intermediate instance keeps the invariants: it satisfies all
    dependencies, does not contain the target row, and its natural join still
    contains the target row.  Work is bounded: at most ``max_passes`` sweeps
    and ``max_checks`` membership tests, so a pathological tableau cannot
    stall the analysis.  The result is always a valid counterexample because a
    drop is only accepted after all invariants are re-checked.
    """
    columns = list(schema.columns)
    max_passes = 8
    max_checks = 4000

    def ok(candidate: list[dict]) -> bool:
        if not candidate:
            return False
        if target in {tuple(r[c] for c in columns) for r in candidate}:
            return False
        if not semantics.satisfies_dependencies(schema, candidate, dependencies):
            return False
        return target in semantics.join_relation(schema, subtables, candidate)

    kept = sorted(rows, key=_row_sort_key)
    checks = 0
    for _ in range(max_passes):
        changed = False
        for row in list(kept):
            if checks >= max_checks:
                return kept
            candidate = [other for other in kept if other is not row]
            checks += 1
            if ok(candidate):
                kept = candidate
                changed = True
        if not changed:
            break
    return kept


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _row_sort_key(row: Mapping | Sequence) -> tuple:
    if isinstance(row, Mapping):
        return tuple(repr(row[c]) for c in sorted(row))
    return tuple(repr(value) for value in row)


def _compact(row: Mapping | None) -> dict:
    return {key: row[key] for key in sorted(row)} if row else {}


def explain_evidence(evidence: Mapping) -> str:
    """Human-readable rendering of an evidence document."""
    lines: list[str] = []
    lines.append(
        "schema: "
        + ", ".join(evidence["schema"])
        + "   sub-tables: "
        + ", ".join(
            f"{st['name']}({','.join(st['columns'])})" for st in evidence["subtables"]
        )
    )
    lines.append(
        "dependencies: "
        + ("; ".join(dep["text"] for dep in evidence["dependencies"]) or "<none>")
    )
    lines.append(f"verdict: {evidence['verdict']}")
    if evidence["verdict"] == "lossless":
        lines.extend(_explain_lossless(evidence))
    else:
        lines.extend(_explain_lossy(evidence))
    return "\n".join(lines)


def _explain_lossless(evidence: Mapping) -> list[str]:
    section = evidence["lossless"]
    lines = ["initial tableau (a:C = distinguished symbol for column C):"]
    rows = [row["symbols"] for row in section["seed_rows"]]
    lines.extend(_render(evidence["schema"], rows))
    for step in section["steps"]:
        if step["kind"] == "fd-merge":
            subs = ", ".join(f"{k} := {v}" for k, v in step["substitutions"].items())
            lines.append(
                f"  step {step['seq']}: FD {step['dependency_text']} on rows "
                f"{step['parents']} unifies {subs} (rows {step['affected_rows']} change)"
            )
        else:
            lines.append(
                f"  step {step['seq']}: MVD {step['dependency_text']} on rows "
                f"{step['parents']} ({step['direction']}) adds row {step['new_row_id']}"
            )
    for observation in section.get("interleavings", []):
        parents = ", ".join(
            f"row {p['row_id']} (MVD step {p['created_by_step']}, "
            f"FD merge steps {p['fd_merge_steps']})"
            for p in observation["merged_parents"]
        )
        lines.append(
            f"  note: step {observation['step']} applies "
            f"{observation['dependency']} to {parents}; an MVD-derived row is "
            "first merged by an FD and then drives a further swap "
            "(MVD -> FD -> MVD)"
        )
    lines.append(
        "witness row "
        f"{section['witness_row_id']}: {_compact(section['witness_row'])}"
        "  (every column holds its distinguished symbol -> lossless)"
    )
    return lines


def _explain_lossy(evidence: Mapping) -> list[str]:
    section = evidence["counterexample"]
    lines = [
        f"finite counterexample instance ({section['instance_size']} row(s), "
        f"built by {section.get('construction', 'unknown')}); values are ordinary "
        "constants shown verbatim.  The row marked [chase witness row] is absent "
        "from the instance yet appears in the join:"
    ]
    lines.extend(_render(evidence["schema"], section["instance"]))
    check = section["dependency_check"]
    lines.append(
        "dependency check: "
        + ("every stated dependency is satisfied" if check["satisfied"] else "VIOLATED")
    )
    lines.append("sub-table projections: " + str(section["projections"]))
    lines.append(f"natural join has {len(section['join_rows'])} row(s) = "
                 f"{section['instance_size']} original + "
                 f"{len(section['extra_rows'])} extra")
    for extra in section["extra_rows"]:
        marker = " [chase witness row]" if extra["row"] == section["target_row"] else ""
        lines.append("  extra joined row (absent from the instance)" + marker + ": "
                     + str(extra["row"]))
        for name, suppliers in extra["suppliers"].items():
            ids = [s["instance_row"] for s in suppliers]
            shown = "; ".join(
                f"row {s['instance_row']} {s['projection']}" for s in suppliers
            )
            lines.append(f"    supplied by {name} from {shown}")
    return lines


def _render(columns: Sequence[str], rows: Sequence[Mapping]) -> list[str]:
    if not rows:
        return ["  <empty>"]
    cols = list(columns)
    cells = [["<none>" if row[c] is None else str(row[c]) for c in cols] for row in rows]
    widths = [
        max([len(c)] + [len(cell[i]) for cell in cells]) for i, c in enumerate(cols)
    ]
    head = "  " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
    sep = "  " + "-+-".join("-" * width for width in widths)
    body = [
        "  " + " | ".join(cell[i].ljust(widths[i]) for i in range(len(cols)))
        for cell in cells
    ]
    return [head, sep] + body
