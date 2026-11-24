"""Independent evidence verification.

``verify_evidence`` does **not** trust :mod:`relanalysis.analysis` or the main
chase.  It parses an evidence document, rebuilds the decomposition tableau
from the declared schema and sub-tables, replays every recorded chase step
against the chase rules, and re-derives the verdict with its own chase
implementation.  A document is accepted only when

* the schema / sub-tables / dependencies are well formed,
* every recorded step is *legal* at the point it is replayed,
* the claimed witness is actually all-distinguished, and
* a from-scratch chase agrees with the claimed verdict,

and, for a lossy claim, when the finite counterexample independently checks out
(dependencies satisfied, join strictly larger, every extra row documented with
valid suppliers, no undocumented extras).

The verifier intentionally re-implements the chase and the join so that a bug
in the main engine cannot silently validate its own evidence.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from .errors import EvidenceError
from .model import Dependency

ACCEPT_FORMAT = "relanalysis-evidence/v1"


class VerificationResult:
    def __init__(self, accepted: bool, errors: list[str], notes: list[str] | None = None):
        self.accepted = bool(accepted)
        self.errors = list(errors)
        self.notes = list(notes or [])

    def __bool__(self) -> bool:
        return self.accepted

    def __repr__(self) -> str:
        state = "accepted" if self.accepted else "rejected"
        return f"VerificationResult({state}, errors={self.errors!r})"


def verify_evidence(document: Mapping, *, max_rows: int = 20_000) -> VerificationResult:
    """Validate an evidence document produced by :func:`relanalysis.analyze`."""
    try:
        verifier = _Verifier(document, max_rows=max_rows)
        return verifier.run()
    except EvidenceError as exc:
        return VerificationResult(False, [str(exc)])


class _Verifier:
    def __init__(self, document: Mapping, *, max_rows: int):
        if not isinstance(document, Mapping):
            raise EvidenceError("evidence must be a mapping")
        self.doc = document
        self.max_rows = max_rows
        self.errors: list[str] = []
        self.notes: list[str] = []

    # -- helpers -----------------------------------------------------------
    def _fail(self, message: str) -> None:
        self.errors.append(message)

    def _parse(self) -> None:
        doc = self.doc
        if doc.get("format") != ACCEPT_FORMAT:
            self._fail(f"unknown evidence format {doc.get('format')!r}")
        try:
            self.columns = list(doc["schema"])
            self.subtables = [(st["name"], list(st["columns"])) for st in doc["subtables"]]
            self.dependencies = [
                Dependency(kind=d["kind"], left=tuple(d["left"]), right=tuple(d["right"]))
                for d in doc["dependencies"]
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceError(f"malformed evidence document: {exc}") from exc

        if not self.columns:
            self._fail("schema is empty")
        if len(set(self.columns)) != len(self.columns):
            self._fail("schema contains duplicate column names")
        colset = set(self.columns)
        if not self.subtables:
            self._fail("no sub-tables declared")
        names = set()
        for name, cols in self.subtables:
            if name in names:
                self._fail(f"duplicate sub-table name {name!r}")
            names.add(name)
            if not cols:
                self._fail(f"sub-table {name!r} has no columns")
            unknown = set(cols) - colset
            if unknown:
                self._fail(f"sub-table {name!r} uses unknown columns {sorted(unknown)!r}")
            if len(set(cols)) != len(cols):
                self._fail(f"sub-table {name!r} repeats a column")
        covered = set()
        for _, cols in self.subtables:
            covered.update(cols)
        if covered != colset:
            self._fail(f"sub-tables do not cover the schema; missing {sorted(colset - covered)!r}")
        for dep in self.dependencies:
            unknown = (set(dep.left) | set(dep.right)) - colset
            if unknown:
                self._fail(f"dependency {dep} uses unknown columns {sorted(unknown)!r}")
            if not dep.right:
                self._fail(f"dependency {dep} has an empty right-hand side")
            if dep.kind == "MVD" and set(dep.left) & set(dep.right):
                self._fail(f"MVD {dep} is not in complete, non-trivial form")

    # -- entry -------------------------------------------------------------
    def run(self) -> VerificationResult:
        self._parse()
        if self.errors:
            return VerificationResult(False, self.errors, self.notes)
        verdict = self.doc.get("verdict")
        if verdict == "lossless":
            self._verify_lossless()
        elif verdict == "lossy":
            self._verify_lossy()
        else:
            self._fail(f"unknown verdict {verdict!r}")
        return VerificationResult(not self.errors, self.errors, self.notes)

    # -- tableau helpers ---------------------------------------------------
    def _seed_row(self, index: int) -> dict:
        _, own = self.subtables[index]
        own = set(own)
        return {
            col: (f"a:{col}" if col in own else f"n:{index}:{col}")
            for col in self.columns
        }

    # -- lossless ----------------------------------------------------------
    def _verify_lossless(self) -> None:
        section = self.doc.get("lossless")
        if not isinstance(section, Mapping):
            self._fail("lossless verdict has no 'lossless' section")
            return
        if self.doc.get("counterexample") is not None:
            self._fail("a lossless document must not carry a counterexample")

        seeds = [self._seed_row(i) for i in range(len(self.subtables))]
        declared = section.get("seed_rows")
        if not isinstance(declared, list) or len(declared) != len(seeds):
            self._fail(
                f"expected {len(seeds)} seed row(s), document declares "
                f"{len(declared) if isinstance(declared, list) else 'none'}"
            )
        else:
            for expected_id, entry in enumerate(declared):
                if entry.get("row_id") != expected_id:
                    self._fail(f"seed row {expected_id}: row_id is {entry.get('row_id')!r}")
                if entry.get("subtable") != self.subtables[expected_id][0]:
                    self._fail(
                        f"seed row {expected_id}: sub-table {entry.get('subtable')!r} "
                        f"should be {self.subtables[expected_id][0]!r}"
                    )
                if dict(entry.get("symbols", {})) != seeds[expected_id]:
                    self._fail(f"seed row {expected_id} symbols disagree with the tableau")

        result = self._replay(section.get("steps"))
        if result is None:
            return
        tableau, created_by, fd_merges = result

        witness_id = section.get("witness_row_id")
        if not isinstance(witness_id, int) or witness_id not in tableau:
            self._fail(f"witness row {witness_id!r} was never derived by the recorded steps")
            return
        expected_witness = {col: f"a:{col}" for col in self.columns}
        if tableau[witness_id] != expected_witness:
            self._fail(f"witness row {witness_id} is not all-distinguished: {tableau[witness_id]}")
        declared_witness = section.get("witness_row")
        if declared_witness is not None and dict(declared_witness) != tableau[witness_id]:
            self._fail("declared witness_row disagrees with the replayed tableau")

        final_tableau = section.get("final_tableau")
        if final_tableau is not None:
            if not isinstance(final_tableau, list):
                self._fail("final_tableau is not a list")
            else:
                declared_rows = {}
                for entry in final_tableau:
                    row_id = entry.get("row_id")
                    symbols = entry.get("symbols")
                    if not isinstance(row_id, int) or not isinstance(symbols, Mapping):
                        self._fail("final_tableau entry is malformed")
                        continue
                    if row_id in declared_rows:
                        self._fail(f"final_tableau lists row {row_id} twice")
                    declared_rows[row_id] = dict(symbols)
                if set(declared_rows) != set(tableau):
                    self._fail(
                        "final_tableau row ids do not match the replayed tableau"
                    )
                for row_id, symbols in declared_rows.items():
                    if row_id in tableau and symbols != tableau[row_id]:
                        self._fail(
                            f"final_tableau row {row_id} disagrees with the replayed "
                            "step outcome"
                        )

        self._check_interleavings(section.get("interleavings"), created_by, fd_merges)

        independent = self._independent_chase()
        if independent is None:
            self._fail("independent chase exceeded its budget")
            return
        if not independent:
            self._fail(
                "independent chase never derives an all-distinguished row; the "
                "lossless claim is unsound"
            )
            return
        self.notes.append(
            f"replayed {len(section.get('steps', []))} legal step(s); witness row "
            f"{witness_id} is all-distinguished and re-derived independently"
        )

    def _replay(self, steps) -> tuple[dict, dict, dict] | None:
        if not isinstance(steps, list):
            self._fail("steps list is missing")
            return None
        tableau = {i: dict(self._seed_row(i)) for i in range(len(self.subtables))}
        created_by = {i: ("seed", None) for i in range(len(self.subtables))}
        fd_merges: dict[int, list[int]] = {i: [] for i in range(len(self.subtables))}
        next_id = len(self.subtables)
        seen_rows = {tuple(row[c] for c in self.columns) for row in tableau.values()}
        for position, step in enumerate(steps, start=1):
            if not isinstance(step, Mapping):
                self._fail(f"step {position} is not a mapping")
                return None
            if step.get("seq") != position:
                self._fail(f"step sequence broken: expected {position}, got {step.get('seq')!r}")
                return None
            kind = step.get("kind")
            if kind == "fd-merge":
                if not self._replay_fd(step, tableau, fd_merges):
                    return None
            elif kind == "mvd-swap":
                if not self._replay_mvd(step, tableau, created_by, next_id, seen_rows):
                    return None
                created_by[next_id] = ("mvd", position)
                fd_merges.setdefault(next_id, [])
                next_id += 1
            else:
                self._fail(f"step {position}: unknown kind {kind!r}")
                return None
        return tableau, created_by, fd_merges

    def _replay_fd(self, step: Mapping, tableau: dict, fd_merges: dict) -> bool:
        seq = step.get("seq")
        dep = self._step_dependency(step)
        if dep is None:
            return False
        if dep.kind != "FD":
            self._fail(f"step {seq}: fd-merge names a non-FD dependency {dep}")
            return False
        parents = step.get("parents")
        if (
            not isinstance(parents, list)
            or len(parents) != 2
            or any(not isinstance(p, int) or p not in tableau for p in parents)
        ):
            self._fail(f"step {seq}: invalid parents {parents!r}")
            return False
        first, second = tableau[parents[0]], tableau[parents[1]]
        if any(first[c] != second[c] for c in dep.left):
            self._fail(f"step {seq}: parents {parents} do not agree on {list(dep.left)}")
            return False
        subs = step.get("substitutions")
        if not isinstance(subs, Mapping) or not subs:
            self._fail(f"step {seq}: no substitutions recorded")
            return False
        right_values = {first[c] for c in dep.right} | {second[c] for c in dep.right}
        for key, value in subs.items():
            if not isinstance(key, str) or not isinstance(value, str):
                self._fail(f"step {seq}: substitution {key!r} -> {value!r} is not symbol-to-symbol")
                return False
            if key == value:
                self._fail(f"step {seq}: substitution {key!r} maps a symbol to itself")
                return False
            if key not in right_values or value not in right_values:
                self._fail(
                    f"step {seq}: substitution {key!r} -> {value!r} renames symbols "
                    f"outside the FD's right-hand side {list(dep.right)}"
                )
                return False
        if any(_resolve(first[c], subs) != _resolve(second[c], subs) for c in dep.right):
            self._fail(f"step {seq}: substitutions do not make the parents agree on {list(dep.right)}")
            return False
        declared_affected = step.get("affected_rows")
        affected = []
        for row_id in sorted(tableau):
            row = tableau[row_id]
            changed = False
            for col in self.columns:
                value = row[col]
                if value in subs and subs[value] != value:
                    row[col] = subs[value]
                    changed = True
            if changed:
                affected.append(row_id)
                fd_merges.setdefault(row_id, []).append(seq)
        if not isinstance(declared_affected, list) or sorted(declared_affected) != affected:
            self._fail(
                f"step {seq}: affected_rows {declared_affected!r} but replay changes {affected}"
            )
            return False
        return True

    def _replay_mvd(self, step: Mapping, tableau: dict, created_by: dict, next_id: int, seen: set) -> bool:
        seq = step.get("seq")
        dep = self._step_dependency(step)
        if dep is None:
            return False
        if dep.kind != "MVD":
            self._fail(f"step {seq}: mvd-swap names a non-MVD dependency {dep}")
            return False
        if step.get("new_row_id") != next_id:
            self._fail(f"step {seq}: new_row_id {step.get('new_row_id')!r}, expected {next_id}")
            return False
        parents = step.get("parents")
        if (
            not isinstance(parents, list)
            or len(parents) != 2
            or any(not isinstance(p, int) or p not in tableau for p in parents)
        ):
            self._fail(f"step {seq}: invalid parents {parents!r}")
            return False
        first, second = tableau[parents[0]], tableau[parents[1]]
        if any(first[c] != second[c] for c in dep.left):
            self._fail(f"step {seq}: parents {parents} do not agree on {list(dep.left)}")
            return False
        row = step.get("row")
        if not isinstance(row, Mapping) or set(row) != set(self.columns):
            self._fail(f"step {seq}: row does not list exactly the schema columns")
            return False
        union = set(dep.left) | set(dep.right)
        direction = step.get("direction")
        candidates = {}
        for name, keep, take in (
            ("first-keeps", first, second),
            ("second-keeps", second, first),
        ):
            candidates[name] = {c: (keep[c] if c in union else take[c]) for c in self.columns}
        if direction not in candidates:
            self._fail(f"step {seq}: unknown direction {direction!r}")
            return False
        expected = candidates[direction]
        if {c: row[c] for c in self.columns} != expected:
            self._fail(f"step {seq}: row is not the {direction} MVD swap of {parents}")
            return False
        key = tuple(expected[c] for c in self.columns)
        if key in seen:
            self._fail(f"step {seq}: derived row already exists in the tableau")
            return False
        seen.add(key)
        tableau[next_id] = dict(expected)
        return True

    def _step_dependency(self, step: Mapping) -> Dependency | None:
        dep = step.get("dependency")
        if not isinstance(dep, Mapping):
            self._fail(f"step {step.get('seq')}: dependency is missing")
            return None
        try:
            return Dependency(kind=dep["kind"], left=tuple(dep["left"]), right=tuple(dep["right"]))
        except (KeyError, TypeError, ValueError):
            self._fail(f"step {step.get('seq')}: malformed dependency {dep!r}")
            return None

    def _check_interleavings(self, claimed, created_by: dict, fd_merges: dict) -> None:
        """Every MVD step driven by an FD-merged MVD row must be disclosed."""
        if claimed is None:
            self._fail("lossless evidence is missing the 'interleavings' list")
            return
        for item in claimed:
            if not isinstance(item, Mapping):
                self._fail("interleaving entry is not a mapping")
                continue
            step = item.get("step")
            for parent in item.get("merged_parents", []):
                row_id = parent.get("row_id")
                created = parent.get("created_by_step")
                merges = parent.get("fd_merge_steps", [])
                if row_id not in created_by:
                    self._fail(f"interleaving for step {step}: row {row_id} does not exist")
                    continue
                origin, actual_created = created_by[row_id]
                if origin != "mvd" or actual_created != created:
                    self._fail(
                        f"interleaving for step {step}: row {row_id} was not created by "
                        f"MVD step {created}"
                    )
                for merge_step in merges:
                    if merge_step not in fd_merges.get(row_id, []):
                        self._fail(
                            f"interleaving for step {step}: FD step {merge_step} did not "
                            f"change row {row_id}"
                        )
                    if not (created < merge_step < step):
                        self._fail(
                            f"interleaving for step {step}: FD step {merge_step} is not "
                            f"between creation ({created}) and use ({step})"
                        )
        # Ensure nothing is omitted: any row used by a swap after an FD merge.
        for step in self._steps_for_check:
            if step.get("kind") != "mvd-swap":
                continue
            for parent in step.get("parents", []):
                origin, created = created_by.get(parent, ("seed", None))
                if origin != "mvd":
                    continue
                relevant = [m for m in fd_merges.get(parent, []) if created < m < step["seq"]]
                if relevant and not any(
                    item.get("step") == step["seq"]
                    and any(p.get("row_id") == parent for p in item.get("merged_parents", []))
                    for item in claimed
                ):
                    self._fail(
                        f"step {step['seq']} uses MVD row {parent} after FD merge "
                        f"{relevant} but the interleaving is not disclosed"
                    )

    # -- independent chase -------------------------------------------------
    def _independent_chase(self) -> bool | None:
        """From-scratch chase, implemented separately from the engine."""
        columns = list(self.columns)
        rows = [self._seed_row(i) for i in range(len(self.subtables))]
        fds = [d for d in self.dependencies if d.kind == "FD"]
        mvds = [d for d in self.dependencies if d.kind == "MVD"]
        steps = 0
        limit = self.max_rows * 4
        while True:
            steps += 1
            if steps > limit:
                return None
            merged = False
            for dep in fds:
                found = None
                for i in range(len(rows)):
                    for j in range(i + 1, len(rows)):
                        if all(rows[i][c] == rows[j][c] for c in dep.left):
                            pairs = [
                                (rows[i][c], rows[j][c])
                                for c in dep.right
                                if rows[i][c] != rows[j][c]
                            ]
                            if pairs:
                                found = _unify(pairs)
                                break
                    if found:
                        break
                if found:
                    rows = [
                        {c: _resolve(row[c], found) for c in columns} for row in rows
                    ]
                    merged = True
                    break
            if merged:
                if len(rows) > self.max_rows:
                    return None
                continue
            added = False
            for dep in mvds:
                union = set(dep.left) | set(dep.right)
                existing = {tuple(r[c] for c in columns) for r in rows}
                candidate = None
                for i in range(len(rows)):
                    for j in range(i + 1, len(rows)):
                        if not all(rows[i][c] == rows[j][c] for c in dep.left):
                            continue
                        for keep, take in ((rows[i], rows[j]), (rows[j], rows[i])):
                            trial = {c: (keep[c] if c in union else take[c]) for c in columns}
                            if tuple(trial[c] for c in columns) not in existing:
                                candidate = trial
                                break
                        if candidate:
                            break
                    if candidate:
                        break
                if candidate is not None:
                    rows.append(candidate)
                    added = True
                    if len(rows) > self.max_rows:
                        return None
                    break
            if not added:
                break
        return any(all(row[c] == f"a:{c}" for c in columns) for row in rows)

    # -- lossy -------------------------------------------------------------
    def _verify_lossy(self) -> None:
        section = self.doc.get("counterexample")
        if not isinstance(section, Mapping):
            self._fail("lossy verdict has no 'counterexample' section")
            return
        if self.doc.get("lossless") is not None:
            self._fail("a lossy document must not carry a lossless section")
        instance = section.get("instance")
        if not isinstance(instance, list) or not instance:
            self._fail("counterexample instance is missing or empty")
            return
        rows: list[dict] = []
        seen: set[tuple] = set()
        for index, row in enumerate(instance):
            if not isinstance(row, Mapping) or set(row) != set(self.columns):
                self._fail(f"instance row {index} does not list exactly the schema columns")
                return
            tup = tuple(row[c] for c in self.columns)
            if tup in seen:
                self._fail(f"instance row {index} duplicates an earlier row (set semantics)")
            seen.add(tup)
            rows.append(dict(row))

        violations = self._violations(rows)
        if violations:
            self._fail("counterexample violates dependencies: " + "; ".join(violations[:3]))
        declared = section.get("dependency_check") or {}
        if declared.get("satisfied") is not True:
            self._fail("document does not declare the instance dependency-satisfying")

        join = self._join(rows)
        extra = join - seen
        if not extra:
            self._fail("natural join equals the instance; not a counterexample")

        extra_list = section.get("extra_rows")
        if not isinstance(extra_list, list) or not extra_list:
            self._fail("document lists no extra rows")
            return
        declared_extra: set[tuple] = set()
        for item in extra_list:
            row = item.get("row")
            if not isinstance(row, Mapping) or set(row) != set(self.columns):
                self._fail("an extra row does not list exactly the schema columns")
                continue
            tup = tuple(row[c] for c in self.columns)
            declared_extra.add(tup)
            if tup not in extra:
                self._fail(f"declared extra row {list(tup)} is not actually extra")
                continue
            suppliers = item.get("suppliers")
            if not isinstance(suppliers, Mapping):
                self._fail(f"extra row {list(tup)} has no suppliers mapping")
                continue
            for name, cols in self.subtables:
                supply = suppliers.get(name)
                if not isinstance(supply, list) or not supply:
                    self._fail(f"extra row {list(tup)} has no supplier for {name!r}")
                    continue
                for supplied in supply:
                    rid = supplied.get("instance_row")
                    if not isinstance(rid, int) or not 0 <= rid < len(rows):
                        self._fail(f"supplier row index {rid!r} out of range")
                        continue
                    source = rows[rid]
                    if not all(source[c] == row[c] for c in cols):
                        self._fail(
                            f"supplier {rid} for {name!r} does not match the extra row "
                            f"on {list(cols)}"
                        )
        missing = extra - declared_extra
        if missing:
            self._fail(
                f"{len(missing)} extra join row(s) undocumented, e.g. "
                f"{list(next(iter(missing)))}"
            )

        # soundness: an independent chase must not find a witness
        independent = self._independent_chase()
        if independent is None:
            self._fail("independent chase exceeded its budget")
            return
        if independent:
            self._fail(
                "independent chase derives the all-distinguished row; the lossy "
                "claim contradicts the tableau test"
            )
            return
        self.notes.append(
            f"instance {len(rows)} row(s), join {len(join)} row(s), "
            f"{len(extra)} extra; all dependencies satisfied; no chase witness"
        )

    def _violations(self, rows: Sequence[Mapping]) -> list[str]:
        columns = list(self.columns)
        present = {tuple(r[c] for c in columns) for r in rows}
        out: list[str] = []
        for dep in self.dependencies:
            if dep.kind == "FD":
                for i in range(len(rows)):
                    for j in range(i + 1, len(rows)):
                        if all(rows[i][c] == rows[j][c] for c in dep.left) and any(
                            rows[i][c] != rows[j][c] for c in dep.right
                        ):
                            out.append(f"FD {dep} broken by rows {i},{j}")
            else:
                union = set(dep.left) | set(dep.right)
                broken = False
                for i in range(len(rows)):
                    for j in range(i + 1, len(rows)):
                        if not all(rows[i][c] == rows[j][c] for c in dep.left):
                            continue
                        for keep, take in ((rows[i], rows[j]), (rows[j], rows[i])):
                            swapped = tuple(
                                keep[c] if c in union else take[c] for c in columns
                            )
                            if swapped not in present:
                                out.append(
                                    f"MVD {dep} broken: swapped row {list(swapped)} "
                                    f"missing for rows {i},{j}"
                                )
                                broken = True
                                break
                        if broken:
                            break
                    if broken:
                        break
        return out

    def _join(self, rows: Sequence[Mapping]) -> set[tuple]:
        columns = list(self.columns)
        result: set[tuple] = set()

        def recurse(depth: int, values: dict) -> None:
            if depth == len(self.subtables):
                if all(c in values for c in columns):
                    result.add(tuple(values[c] for c in columns))
                return
            _, sub_cols = self.subtables[depth]
            projection = {tuple(r[c] for c in sub_cols) for r in rows}
            for tup in projection:
                candidate = dict(values)
                compatible = True
                for col, value in zip(sub_cols, tup):
                    if col in candidate and candidate[col] != value:
                        compatible = False
                        break
                    candidate[col] = value
                if compatible:
                    recurse(depth + 1, candidate)

        recurse(0, {})
        return result

    # set after parsing for interleaving completeness checks
    @property
    def _steps_for_check(self):
        section = self.doc.get("lossless") or {}
        steps = section.get("steps") if isinstance(section, Mapping) else None
        return steps if isinstance(steps, list) else []


def _resolve(value: str, subs: Mapping[str, str]) -> str:
    seen = set()
    while value in subs and value not in seen:
        seen.add(value)
        value = subs[value]
    return value


def _unify(pairs: Sequence[tuple[str, str]]) -> dict[str, str]:
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        a_dist, b_dist = ra.startswith("a:"), rb.startswith("a:")
        if a_dist != b_dist:
            rep, other = (ra, rb) if a_dist else (rb, ra)
        else:
            rep, other = (ra, rb) if repr(ra) <= repr(rb) else (rb, ra)
        parent[other] = rep
    return {k: find(k) for k in list(parent) if find(k) != k}
