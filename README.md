# relanalysis — lossless-join analysis with checkable evidence

`relanalysis` decides whether a relational schema can be decomposed into
sub-tables and be rebuilt **exactly** by projecting each sub-table and taking
the natural join. Concretely:

> For a total schema of at most 5 columns, a set of sub-tables whose columns
> together cover the schema, functional dependencies `X -> Y`, and complete
> multivalued dependencies `X ->> Y | (R - X - Y)`, decide whether
> `π_{R1}(r) ⨝ … ⨝ π_{Rk}(r) = r` holds for **every** finite relation `r` that
> satisfies all the dependencies.

The answer always comes with machine-checkable evidence:

* **Lossless** — a step-by-step chase derivation of the all-distinguished
  witness row, each step recording which dependency fired, on which rows, with
  which symbol substitutions. Steps where an MVD-derived row is first merged by
  an FD and only then drives a further MVD swap (`MVD -> FD -> MVD`) are
  called out explicitly.
* **Lossy** — a finite relation that satisfies every stated dependency, the
  projections of each sub-table, the join rows that are **not** in the
  relation, and, for every such extra row, the original rows in each sub-table
  that supply it.

Every evidence document is accepted by an **independent verifier**
(`verify_evidence`) that re-implements the chase and the join from scratch and
replays each recorded step.

## Semantics (what the code actually implements)

* **Set semantics.** A relation is a *set* of rows; duplicate rows collapse.
* **Equality only.** Values are compared with `==`. They may be any hashable
  object (ints, strings, tuples, booleans). There is no ordering, no domain
  operations, and **NULLs are rejected** at the boundary.
* **Functional dependency** `X -> Y`: for any two rows that agree on `X`, the
  projections on `Y` are equal.
* **Multivalued dependency** `X ->> Y` (complete form, `X` and `Y` disjoint):
  for any two rows `r1`, `r2` with `r1[X] = r2[X]`, both swapped rows
  `(r1[X ∪ Y], r2[R − X − Y])` and `(r2[X ∪ Y], r1[R − X − Y])` are present.
  This is exactly "any two same-`X` rows may exchange their `Y` values with
  the remaining columns and stay inside the relation".
* **Lossless join.** The decomposition is lossless iff every instance
  satisfying the dependencies joins back exactly. This is the classical chase
  characterisation: build the decomposition tableau (one row per sub-table,
  distinguished symbol `a:C` on its own columns, null `n:i:C` elsewhere) and
  chase it. The decomposition is lossless iff an all-distinguished row is
  derived.

## Public API

```python
from relanalysis import analyze, verify_evidence

result = analyze(
    ["A", "B", "C", "D"],                                    # schema
    [("R1", ["A", "B"]), ("R2", ["A", "C"]), ("R3", ["B", "D"])],  # sub-tables
    [("FD", ("C",), ("A",)),
     ("MVD", ("A",), ("B",)),
     ("MVD", ("B",), ("C",))],
)
result.lossless            # -> bool
result.verdict             # -> "lossless" / "lossy"
result.to_dict()           # -> JSON-serialisable evidence document
result.explain()           # -> multi-line human-readable derivation
verify_evidence(result.to_dict()).accepted   # -> True, independently checked
```

Accepted input forms:

| argument      | forms                                                                 |
|---------------|-----------------------------------------------------------------------|
| `schema`      | list of column names, or a `Schema`                                   |
| `subtables`   | sequence of `(name, columns)`, a `{name: columns}` mapping, or `SubTable` objects |
| `dependencies`| `("FD", left, right)`, `("MVD", left, right)`, mappings with `kind`/`left`/`right`, or `Dependency` |

`left`/`right` may be a single column name or a sequence of column names.

Also exported: `analyze_problem(document)` for the JSON problem shape used by
the CLI; `Schema`, `SubTable`, `Dependency`, `Instance`; the operational
semantics `dependency_violations`, `satisfies_dependencies`,
`join_relation`, `join_extra`, `sources_for_extra_row`; the chase primitives
`build_seeds`, `chase_tableau`, `interleaved_swaps`; and the error types
`SchemaError`, `DependencyError`, `DataError`, `EvidenceError`.

### Evidence document shape

```jsonc
{
  "format": "relanalysis-evidence/v1",
  "schema": ["A", "B", "C"],
  "subtables": [{"name": "R1", "columns": ["A", "B"]}, ...],
  "dependencies": [{"kind": "FD", "left": ["B"], "right": ["C"], "text": "B -> C"}],
  "verdict": "lossless",
  "method": { "tableau": [...], "canonical_dependencies": [...] },
  "lossless": {
    "seed_rows":  [{"row_id": 0, "subtable": "R1", "symbols": {...}}, ...],
    "steps": [
      {"seq": 1, "kind": "fd-merge", "dependency": {...}, "parents": [0, 1],
       "substitutions": {"n:0:C": "a:C"}, "affected_rows": [0]},
      {"seq": 2, "kind": "mvd-swap", "dependency": {...}, "parents": [0, 1],
       "direction": "first-keeps", "new_row_id": 2, "row": {...}}
    ],
    "witness_row_id": 0,
    "witness_row": {"A": "a:A", "B": "a:B", "C": "a:C"},
    "final_tableau": [{"row_id": 0, "symbols": {...}}, ...],
    "interleavings": [
      {"step": 6, "kind": "mvd-after-fd-merge-of-mvd-row",
       "merged_parents": [{"row_id": 5, "created_by_step": 3,
                           "fd_merge_steps": [4], ...}]}
    ]
  },
  "counterexample": null
}
```

For a lossy verdict the `counterexample` object is populated instead:

```jsonc
"counterexample": {
  "instance": [{"A": "a:A", "B": "a:B", "C": "n:0:C"}, ...],
  "construction": "small-search",          // or "chased-tableau"
  "dependency_check": {"satisfied": true, "violations": []},
  "projections": {"R1": [["a:A", "a:B"], ...], "R2": [...]},
  "join_rows": [...],
  "extra_rows": [
    {"row": {"A": "a:A", "B": "a:B", "C": "a:C"},
     "suppliers": {"R1": [{"instance_row": 0, "projection": {...}}],
                   "R2": [{"instance_row": 1, "projection": {...}}]}}
  ],
  "target_row": {"A": "a:A", "B": "a:B", "C": "a:C"},
  "target_row_is_extra": true
}
```

Values in a counterexample are ordinary constants. The construction is
free to rename them, since only equality matters; one extra join row is
always relabelled onto the chase's all-distinguished row `(a:A, a:B, …)` so
the failure is directly comparable with the tableau test. The verifier
recomputes everything and does not rely on the claimed values.

### What `verify_evidence` checks

For every document:

1. format tag, schema, sub-tables and dependencies are well formed and the
   sub-tables cover the schema;
2. recorded seed rows equal the tableau-derived seeds;
3. every recorded step is **legal** where it is replayed — the named
   dependency exists, parents agree on its left side, an FD step's
   substitutions really merge the right side and change exactly the rows it
   claims, an MVD step's row is exactly the swap it claims and is new;
4. the claimed witness is all-distinguished and derivable from the replayed
   tableau, and the optional `final_tableau` snapshot matches the replayed
   rows exactly;
5. every `MVD -> FD -> MVD` interleaving is disclosed, correctly attributed to
   a real MVD-created row and a real intervening FD merge, and none is
   fabricated or omitted;
6. an **independent** from-scratch chase agrees with the claimed verdict.

For a lossy claim it additionally checks the counterexample: exact schema
columns per row, no duplicate rows, all dependencies satisfied, the join
strictly larger than the instance, every extra row documented with at least
one valid supplier per sub-table and none invented, no undocumented extras,
and no chase witness derivable (so the verdict itself is sound).

`verify_evidence` returns a `VerificationResult` with `.accepted`, `.errors`
and `.notes`; it is falsy when rejected. Malformed input never raises —
it is returned as a rejection.

## How the verdict is computed

`chase_tableau` alternates two rules until nothing changes:

* **FD `X -> Y`**: two rows agreeing on `X` have their `Y` symbols unified
  (distinguished symbols win; nulls collapse to one representative). The
  substitution is applied to the whole tableau.
* **MVD `X ->> Y`**: two rows agreeing on `X` produce the two swapped rows.

Because rows added by an MVD can agree with another row only after an FD has
merged some of their symbols, the rules must interleave; the loop does that
automatically and `interleaved_swaps` reports exactly those steps.

Order independence is enforced structurally, not assumed:

* sub-tables are sorted canonically by `(columns, name)` before seeding;
* dependencies are deduplicated and sorted by `(kind, left, right)`;
* chase rules are applied in canonical dependency order and ascending row id;
* all report lists are sorted by content, and dependency/sub-table order never
  enters the output.

Consequently permuting sub-tables and/or dependencies yields a **byte-identical**
evidence document (`tests/test_invariance.py` asserts `==` over all
permutations).

Termination: MVD steps only copy existing symbols, and FD steps only merge
existing symbols, so the symbol set per column never grows; the tableau is
bounded and the chase always reaches a fixpoint.

Counterexamples: for a lossy verdict the code first runs a small, seeded,
bounded search for a small constant-valued instance (closed under the MVD
rules, FD-checked, join strictly larger). Each candidate is only accepted
after the operational checks pass. If the search finds nothing, it falls back
to the closed chased tableau read as a relation (symbols as constants), which
always works when the witness is absent. The chosen instance is then greedily
minimised under a bounded budget, relabelled canonically, and re-checked.
Nothing from the search is trusted: every claim in the document is recomputed
by the verifier.

## Reproducing

Requires only Python 3.11.6 and the standard library. From the workspace root:

```sh
# all tests (unittest discovery from the workspace root)
python3 -m unittest discover -s tests -v

# one module
python3 -m unittest tests.test_analysis -v

# a single case through the library
python3 - <<'PY'
from relanalysis import analyze, verify_evidence
r = analyze(["A","B","C","D"],
            [("R1",["A","B"]),("R2",["A","C"]),("R3",["B","D"])],
            [("FD",("C",),("A",)),("MVD",("A",),("B",)),("MVD",("B",),("C",))])
print(r.verdict)
print(r.explain())
print(verify_evidence(r.to_dict()).accepted)
PY

# the CLI: analyse a problem JSON and self-verify
python3 -m relanalysis problem.json
python3 -m relanalysis problem.json --json > evidence.json
python3 -m relanalysis --verify evidence.json
```

Example problem file:

```json
{
  "schema": ["A", "B", "C", "D"],
  "subtables": [
    {"name": "R1", "columns": ["A", "B"]},
    {"name": "R2", "columns": ["A", "C"]},
    {"name": "R3", "columns": ["B", "D"]}
  ],
  "dependencies": [
    {"kind": "FD", "left": ["C"], "right": ["A"]},
    {"kind": "MVD", "left": ["A"], "right": ["B"]},
    {"kind": "MVD", "left": ["B"], "right": ["C"]}
  ]
}
```

`python3 -m relanalysis --verify` prints `ACCEPTED`/`REJECTED` and exits 0/1;
the analysis command self-verifies before exiting.

## Test coverage

`tests/` is discoverable from the workspace root with
`python3 -m unittest discover -s tests`. Covered scenarios:

| file | scope |
|------|-------|
| `test_model.py` | schema column limit (≤5), sub-table coverage, dependency parsing, set semantics, NULL rejection |
| `test_chase.py` | seed tableau, symbol unification, both chase rules, witness derivation, interleaving detection |
| `test_semantics.py` | FD/MVD satisfaction, natural join, extra rows, suppliers |
| `test_analysis.py` | normal/edge verdicts, lossless derivations, lossy counterexamples, interleaving evidence |
| `test_evidence.py` | acceptance of genuine evidence; rejection of forged witnesses, illegal steps, wrong substitutions, fabricated counterexamples, abbreviated suppliers, malformed documents |
| `test_invariance.py` | identical evidence under **all** permutations of sub-tables and dependencies |
| `test_oracle.py` | verdict cross-checked against exhaustive enumeration of every small finite instance |
| `test_boundaries_cli.py` | 5-column limit, empty/trivial cases, symbol-like column names, input errors, CLI and `--verify` |

Status of the verification runs in this submission: the full suite
(`python3 -m unittest discover -s tests`) runs **136 tests**, all passing, in
about 0.5 s. Randomised sweeps outside the suite (600 mixed 5-column
decompositions, 400 guaranteed-lossless decompositions, and 196 deliberately
corrupted evidence documents) produced evidence the verifier accepted for every
genuine document and rejected for every corrupted one; the worst observed
analysis time was under 0.5 s. Those sweeps are exploratory scripts and are not
part of the graded test suite.

## Limits

* At most 5 columns and one `Schema` per analysis.
* Dependencies are given in complete, non-trivial MVD form (`X ->> Y` with
  `X ∩ Y = ∅`); the complement `R − X − Y` is derived automatically.
* Only equality is interpreted: uninterpreted constants, no constraints,
  no NULLs.
* A lossy counterexample is a genuine finite relation, but its *values* are
  synthetic constants chosen by the search; only the equational pattern
  matters for the lossless-join question.
* The counterexample search is seeded from the canonical problem, so results
  are deterministic across runs and order-independent; it is a search, not an
  exhaustive minimality proof, so the instance is small in practice but not
  guaranteed minimum-cardinality.
