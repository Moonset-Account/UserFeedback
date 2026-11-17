#!/usr/bin/env python3
"""Local verification harness for the anonymizer library.

Usage:
    python tools/local_verify.py --list
    python tools/local_verify.py --case C01|C02|C03|C04

Each case runs real assertions against the library in this workspace and
prints one JSON record with the case id, assertion counts
(passed/failed/skipped), the deterministic seed and an expected/observed
summary.  Exit code is non-zero if any assertion fails, no assertions ran,
or the case itself errored.  Only synthetic data is used; no credentials,
real samples or account content appear in the output.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anonymizer import (  # noqa: E402
    GenNode,
    GeneralizationTree,
    anonymize,
    MAX_QUASI_IDENTIFIERS,
    MAX_TREE_LAYERS,
)

SEED = 20261117  # data is fully deterministic; the seed is declared for replay


class Checker:
    def __init__(self) -> None:
        self.results = []

    def check(self, name, condition, detail=""):
        self.results.append(
            {"name": name, "passed": bool(condition), "detail": str(detail)}
        )


# --------------------------------------------------------------------------
# shared synthetic fixtures (no real data)
# --------------------------------------------------------------------------

def age_tree():
    return GeneralizationTree(
        GenNode("Age*", [
            GenNode("young", [GenNode("a18"), GenNode("a25")]),
            GenNode("mid", [GenNode("a40"), GenNode("a52")]),
        ])
    )


def zip_tree():
    return GeneralizationTree(
        GenNode("Zip*", [
            GenNode("z1*", [GenNode("z11"), GenNode("z12")]),
            GenNode("z2*", [GenNode("z21"), GenNode("z22")]),
        ])
    )


def independent_brute_force(rec_leaves, sens, trees, fields, weights, k, l,
                            penalty, budget):
    """Verifier-side re-implementation of grouping/loss/selection used to
    cross-check the library on a small domain."""
    best = None
    for vector in product(*(range(t.max_level + 1) for t in trees)):
        groups = {}
        for i, leaves in enumerate(rec_leaves):
            key = tuple(t.generalize(leaf, lv) for t, leaf, lv in zip(trees, leaves, vector))
            groups.setdefault(key, []).append(i)
        suppressed = 0
        for members in groups.values():
            distinct = len({sens[i] for i in members})
            if len(members) < k or distinct < l:
                suppressed += len(members)
        if suppressed > budget:
            continue
        per_record = sum(weights[f][lv] for f, lv in zip(fields, vector))
        total = (len(rec_leaves) - suppressed) * per_record + suppressed * penalty
        if best is None or total < best[0]:
            best = (total, vector, suppressed)
    return best


# --------------------------------------------------------------------------
# C01: exhaustive small-domain cross-check
# --------------------------------------------------------------------------

def case_c01():
    chk = Checker()
    fields = ["age", "zip"]
    trees = [age_tree(), zip_tree()]
    qi = {"age": trees[0], "zip": trees[1]}
    weights = {"age": [0, 1, 2], "zip": [0, 1, 2]}
    k, l, penalty = 2, 2, 5

    # scenario A: 8 records, optimum retains everything
    recs_a = [
        {"age": a, "zip": z, "cond": s}
        for a, z, s in [
            ("a18", "z11", "sA"), ("a25", "z11", "sA"),
            ("a40", "z12", "sB"), ("a52", "z12", "sB"),
            ("a18", "z21", "sC"), ("a25", "z21", "sC"),
            ("a40", "z22", "sD"), ("a52", "z22", "sD"),
        ]
    ]
    res_a = anonymize(recs_a, quasi_identifiers=qi, sensitive_field="cond",
                      k=k, l=l, max_suppressed=8, loss_weights=weights,
                      suppression_penalty=penalty)
    leaves_a = [[t.resolve(r[f]) for t, f in zip(trees, fields)] for r in recs_a]
    sens_a = [r["cond"] for r in recs_a]
    brute_a = independent_brute_force(leaves_a, sens_a, trees, fields, weights,
                                      k, l, penalty, 8)
    chk.check("A: feasible", res_a.feasible)
    chk.check("A: exhaustive candidate count", res_a.candidates_evaluated == 9,
              f"observed {res_a.candidates_evaluated}")
    chk.check("A: levels match brute-force minimum",
              res_a.levels == dict(zip(fields, brute_a[1])),
              f"expected {brute_a[1]}, observed {res_a.levels}")
    chk.check("A: total loss equals brute-force minimum",
              res_a.total_loss == brute_a[0],
              f"expected {brute_a[0]}, observed {res_a.total_loss}")
    chk.check("A: expected levels (age=0, zip=2)",
              res_a.levels == {"age": 0, "zip": 2}, f"observed {res_a.levels}")
    chk.check("A: expected total loss 16", res_a.total_loss == 16,
              f"observed {res_a.total_loss}")
    retained_groups = [g for g in res_a.groups if g.decision == "retained"]
    chk.check("A: every retained group meets k",
              all(g.size >= k for g in retained_groups),
              f"sizes {[g.size for g in retained_groups]}")
    chk.check("A: every retained group meets l",
              all(g.distinct_sensitive >= l for g in retained_groups),
              f"distinct {[g.distinct_sensitive for g in retained_groups]}")
    chk.check("A: retained + suppressed covers all records",
              len(res_a.retained) + res_a.suppressed_count == len(recs_a))

    # scenario B: 9 records, optimum suppresses exactly two whole groups
    weights_b = {"age": [0, 1, 10], "zip": [0, 1, 2]}
    penalty_b = 10
    recs_b = recs_a[:8] + [{"age": "a40", "zip": "z21", "cond": "sB"}]
    recs_b[6] = {"age": "a40", "zip": "z22", "cond": "sB"}
    recs_b[7] = {"age": "a52", "zip": "z22", "cond": "sB"}
    res_b = anonymize(recs_b, quasi_identifiers=qi, sensitive_field="cond",
                      k=k, l=l, max_suppressed=9, loss_weights=weights_b,
                      suppression_penalty=penalty_b)
    leaves_b = [[t.resolve(r[f]) for t, f in zip(trees, fields)] for r in recs_b]
    sens_b = [r["cond"] for r in recs_b]
    brute_b = independent_brute_force(leaves_b, sens_b, trees, fields, weights_b,
                                      k, l, penalty_b, 9)
    chk.check("B: feasible", res_b.feasible)
    chk.check("B: levels match brute-force minimum",
              res_b.levels == dict(zip(fields, brute_b[1])),
              f"expected {brute_b[1]}, observed {res_b.levels}")
    chk.check("B: total loss equals brute-force minimum",
              res_b.total_loss == brute_b[0],
              f"expected {brute_b[0]}, observed {res_b.total_loss}")
    chk.check("B: expected levels (age=0, zip=2)",
              res_b.levels == {"age": 0, "zip": 2}, f"observed {res_b.levels}")
    chk.check("B: expected total loss 58", res_b.total_loss == 58,
              f"observed {res_b.total_loss}")
    chk.check("B: expected suppressed count 5", res_b.suppressed_count == 5,
              f"observed {res_b.suppressed_count}")
    sup_groups = [g for g in res_b.groups if g.decision == "suppressed"]
    sup_members = sorted(i for g in sup_groups for i in g.members)
    chk.check("B: suppression is whole-group only",
              sup_members == sorted(res_b.suppressed_indices),
              f"group members {sup_members} vs suppressed {res_b.suppressed_indices}")
    chk.check("B: suppressed groups each violate k or l",
              all(not (g.meets_k and g.meets_l) for g in sup_groups),
              f"groups {[(g.size, g.distinct_sensitive) for g in sup_groups]}")
    chk.check("B: no lower-loss vector exists (brute-force min)",
              brute_b[0] == res_b.total_loss)

    summary = {
        "samples": "synthetic: A=8 records, B=9 records, 2 QI fields, depth-2 trees",
        "expected": {"A": {"levels": {"age": 0, "zip": 2}, "total_loss": 16},
                     "B": {"levels": {"age": 0, "zip": 2}, "total_loss": 58,
                           "suppressed": 5}},
        "observed": {"A": {"levels": res_a.levels, "total_loss": res_a.total_loss},
                     "B": {"levels": res_b.levels, "total_loss": res_b.total_loss,
                           "suppressed": res_b.suppressed_count}},
    }
    return chk, summary


# --------------------------------------------------------------------------
# C02: loss accounting and deterministic tie-break
# --------------------------------------------------------------------------

def case_c02():
    chk = Checker()
    age = GeneralizationTree(GenNode("A*", [GenNode("a1"), GenNode("a2"), GenNode("a3")]))
    zip_ = GeneralizationTree(GenNode("Z*", [GenNode("z1"), GenNode("z2")]))
    qi = {"age": age, "zip": zip_}
    weights = {"age": [0, 3], "zip": [0, 3]}

    # scenario 1: exact tie between vectors (0,1) and (1,0) at loss 12
    recs = [
        {"age": "a1", "zip": "z1", "cond": "s1"},
        {"age": "a1", "zip": "z2", "cond": "s2"},
        {"age": "a2", "zip": "z1", "cond": "s3"},
        {"age": "a2", "zip": "z2", "cond": "s4"},
    ]
    res = anonymize(recs, quasi_identifiers=qi, sensitive_field="cond",
                    k=2, l=2, max_suppressed=4, loss_weights=weights,
                    suppression_penalty=100)
    chk.check("tie: feasible", res.feasible)
    chk.check("tie: lexicographically smallest vector wins",
              res.levels == {"age": 0, "zip": 1},
              f"observed {res.levels} (tied with {{'age': 1, 'zip': 0}})")
    chk.check("tie: generalization loss = retained * per-record weight",
              res.generalization_loss == 4 * (0 + 3),
              f"observed {res.generalization_loss}")
    chk.check("tie: suppression loss is zero", res.suppression_loss == 0)
    chk.check("tie: total = gen + suppression",
              res.total_loss == res.generalization_loss + res.suppression_loss,
              f"observed {res.total_loss}")
    res_again = anonymize(recs, quasi_identifiers=qi, sensitive_field="cond",
                          k=2, l=2, max_suppressed=4, loss_weights=weights,
                          suppression_penalty=100)
    chk.check("tie: deterministic across runs",
              (res.levels, res.total_loss, [g.key_paths for g in res.groups])
              == (res_again.levels, res_again.total_loss,
                  [g.key_paths for g in res_again.groups]))

    # scenario 2: one singleton group suppressed, penalty accounted per record
    recs2 = recs + [{"age": "a3", "zip": "z1", "cond": "s5"}]
    res2 = anonymize(recs2, quasi_identifiers=qi, sensitive_field="cond",
                     k=2, l=2, max_suppressed=4, loss_weights=weights,
                     suppression_penalty=1)
    chk.check("penalty: feasible", res2.feasible)
    chk.check("penalty: expected levels (age=0, zip=1)",
              res2.levels == {"age": 0, "zip": 1}, f"observed {res2.levels}")
    chk.check("penalty: exactly one record suppressed",
              res2.suppressed_count == 1, f"observed {res2.suppressed_count}")
    chk.check("penalty: suppression loss = count * penalty",
              res2.suppression_loss == 1 * 1,
              f"observed {res2.suppression_loss}")
    chk.check("penalty: generalization loss = 4 retained * 3",
              res2.generalization_loss == 12, f"observed {res2.generalization_loss}")
    chk.check("penalty: total loss 13", res2.total_loss == 13,
              f"observed {res2.total_loss}")
    sup_groups = [g for g in res2.groups if g.decision == "suppressed"]
    chk.check("penalty: suppressed group is the singleton, whole-group",
              len(sup_groups) == 1 and sup_groups[0].members == (4,),
              f"observed {[g.members for g in sup_groups]}")

    summary = {
        "samples": "synthetic: 4+1 records, 2 QI fields, depth-1 trees",
        "expected": {"tie_levels": {"age": 0, "zip": 1}, "tie_loss": 12,
                     "penalty_total": 13, "penalty_suppressed": 1},
        "observed": {"tie_levels": res.levels, "tie_loss": res.total_loss,
                     "penalty_total": res2.total_loss,
                     "penalty_suppressed": res2.suppressed_count},
    }
    return chk, summary


# --------------------------------------------------------------------------
# C03: node identity vs display name, missing leaf, uniform level
# --------------------------------------------------------------------------

def case_c03():
    chk = Checker()
    dup_x = GenNode("Dup", [GenNode("a1")])
    dup_y = GenNode("Dup", [GenNode("a2")])
    missing = GenNode("(missing)")
    root = GenNode("R", [dup_x, dup_y, missing])
    tree = GeneralizationTree(root, missing_leaf=missing)
    leaf_a1 = dup_x.children[0]
    leaf_a2 = dup_y.children[0]
    qi = {"region": tree}
    recs = [
        {"region": leaf_a1, "cond": "s1"},
        {"region": leaf_a1, "cond": "s2"},
        {"region": leaf_a2, "cond": "s3"},
        {"region": leaf_a2, "cond": "s4"},
        {"region": None, "cond": "s5"},
    ]

    # scenario 1: level 0 chosen; missing record forms its own suppressed group
    res0 = anonymize(recs, quasi_identifiers=qi, sensitive_field="cond",
                     k=2, l=2, max_suppressed=1,
                     loss_weights={"region": [0, 1, 2]}, suppression_penalty=10)
    chk.check("missing: level 0 chosen", res0.levels == {"region": 0},
              f"observed {res0.levels}")
    sup0 = [g for g in res0.groups if g.decision == "suppressed"]
    chk.check("missing: exactly one suppressed group", len(sup0) == 1,
              f"observed {len(sup0)}")
    chk.check("missing: suppressed group is the independent missing leaf",
              sup0 and sup0[0].key_names == ("(missing)",)
              and sup0[0].members == (4,),
              f"observed {sup0[0].key_names if sup0 else None}")
    chk.check("missing: missing leaf path is independent of other leaves",
              tree.path_of(missing) != tree.path_of(leaf_a1)
              and tree.path_of(missing) != tree.path_of(leaf_a2))

    # scenario 2: level 1 chosen; same-name nodes must NOT merge
    res1 = anonymize(recs, quasi_identifiers=qi, sensitive_field="cond",
                     k=2, l=2, max_suppressed=1,
                     loss_weights={"region": [5, 1, 10]}, suppression_penalty=3)
    chk.check("identity: level 1 chosen", res1.levels == {"region": 1},
              f"observed {res1.levels}")
    retained1 = [g for g in res1.groups if g.decision == "retained"]
    chk.check("identity: two retained groups, not one merged group",
              len(retained1) == 2, f"observed {len(retained1)}")
    chk.check("identity: both groups display the repeated name 'Dup'",
              all(g.key_names == ("Dup",) for g in retained1),
              f"observed {[g.key_names for g in retained1]}")
    chk.check("identity: group keys differ by node identity (path)",
              len({g.key_paths for g in retained1}) == 2,
              f"observed {[g.key_paths for g in retained1]}")
    chk.check("identity: each retained group has exactly 2 members",
              sorted(g.size for g in retained1) == [2, 2],
              f"observed {[g.size for g in retained1]}")
    chk.check("uniform: one level per field for the whole batch",
              isinstance(res1.levels.get("region"), int)
              and all(len(r["quasi_paths"]["region"]) == 1 for r in res1.retained),
              "all retained records generalized to depth 1 (level 1)")

    # scenario 3: resolution rules for duplicate names / unknown / missing
    dup_tree = GeneralizationTree(GenNode("W", [
        GenNode("P", [GenNode("X")]),
        GenNode("Q", [GenNode("X")]),
    ]))
    try:
        dup_tree.resolve("X")
        chk.check("resolve: ambiguous duplicate leaf name rejected", False)
    except ValueError:
        chk.check("resolve: ambiguous duplicate leaf name rejected", True)
    leaf_x = dup_tree.root.children[0].children[0]
    chk.check("resolve: explicit GenNode resolves by identity",
              dup_tree.resolve(leaf_x) is leaf_x)
    try:
        dup_tree.resolve("nope")
        chk.check("resolve: unknown value rejected", False)
    except ValueError:
        chk.check("resolve: unknown value rejected", True)
    try:
        dup_tree.resolve(None)
        chk.check("resolve: missing value without missing leaf rejected", False)
    except ValueError:
        chk.check("resolve: missing value without missing leaf rejected", True)
    foreign = GenNode("alien")
    try:
        dup_tree.resolve(foreign)
        chk.check("resolve: foreign node rejected", False)
    except ValueError:
        chk.check("resolve: foreign node rejected", True)

    summary = {
        "samples": "synthetic: 5 records, 1 QI field, repeated display name 'Dup', "
                   "independent '(missing)' leaf",
        "expected": {"retained_groups_at_level1": 2, "suppressed_key": "(missing)"},
        "observed": {"retained_groups_at_level1": len(retained1),
                     "suppressed_key": sup0[0].key_names if sup0 else None},
    }
    return chk, summary


# --------------------------------------------------------------------------
# C04: suppression budget boundary, infeasibility, max declared scale
# --------------------------------------------------------------------------

def case_c04():
    chk = Checker()
    s_tree = GeneralizationTree(
        GenNode("S*", [GenNode(f"s{i}") for i in range(1, 6)]))
    qi = {"s": s_tree}
    # all records share one sensitive value, so even the fully generalized
    # root group violates l=2: every level vector suppresses all 5 records
    recs = [{"s": f"s{i}", "cond": "d1"} for i in range(1, 6)]
    weights = {"s": [0, 100]}

    # boundary: minimum achievable suppression is exactly 5
    ok = anonymize(recs, quasi_identifiers=qi, sensitive_field="cond",
                   k=2, l=2, max_suppressed=5, loss_weights=weights,
                   suppression_penalty=2)
    chk.check("budget: feasible at budget == minimum suppression", ok.feasible)
    chk.check("budget: suppressed count equals budget",
              ok.suppressed_count == 5, f"observed {ok.suppressed_count}")
    chk.check("budget: total loss = 5 * penalty",
              ok.total_loss == 10, f"observed {ok.total_loss}")

    # infeasible: budget one below the minimum
    no = anonymize(recs, quasi_identifiers=qi, sensitive_field="cond",
                   k=2, l=2, max_suppressed=4, loss_weights=weights,
                   suppression_penalty=2)
    chk.check("infeasible: flagged not feasible", not no.feasible)
    chk.check("infeasible: no levels/retained data emitted",
              no.levels is None and no.retained == [] and no.groups == [])
    chk.check("infeasible: reason states budget and minimum achievable",
              "4" in no.reason and "5" in no.reason,
              f"reason: {no.reason}")
    chk.check("infeasible: candidates were still evaluated",
              no.candidates_evaluated == 2,
              f"observed {no.candidates_evaluated}")

    # per-group verification basis on the boundary solution
    chk.check("basis: group count matches singleton groups",
              len(ok.groups) == 5, f"observed {len(ok.groups)}")
    chk.check("basis: group sizes sum to record count",
              sum(g.size for g in ok.groups) == len(recs))
    chk.check("basis: every group exposes the verification fields",
              all(isinstance(g.size, int) and isinstance(g.distinct_sensitive, int)
                  and isinstance(g.meets_k, bool) and isinstance(g.meets_l, bool)
                  and g.decision in ("retained", "suppressed") and g.reason
                  and isinstance(g.members, tuple) and g.key_paths
                  for g in ok.groups))
    chk.check("basis: suppressed groups carry a reason and violate k/l",
              all(g.decision == "suppressed" and not (g.meets_k and g.meets_l)
                  for g in ok.groups))

    # max declared scale: 6 QI fields, 4-layer trees
    deep_qi = {}
    deep_rec = []
    for j in range(MAX_QUASI_IDENTIFIERS):
        f = f"f{j}"
        deep_qi[f] = GeneralizationTree(
            GenNode(f"r{j}", [GenNode(f"m{j}", [GenNode(f"s{j}", [
                GenNode(f"v{j}a"), GenNode(f"v{j}b")])])]))
    for i in range(4):
        rec = {"cond": f"c{i % 2}"}
        for j in range(MAX_QUASI_IDENTIFIERS):
            rec[f"f{j}"] = f"v{j}{'ab'[i % 2]}"
        deep_rec.append(rec)
    deep_weights = {f: [0, 1, 2, 3] for f in deep_qi}
    deep = anonymize(deep_rec, quasi_identifiers=deep_qi, sensitive_field="cond",
                     k=2, l=2, max_suppressed=4, loss_weights=deep_weights,
                     suppression_penalty=1)
    chk.check("scale: 6 fields x 4 layers accepted", deep.feasible)
    chk.check("scale: levels reported for all 6 fields",
              deep.levels is not None and len(deep.levels) == MAX_QUASI_IDENTIFIERS,
              f"observed {deep.levels}")
    chk.check("scale: exhaustive candidates = 4**6",
              deep.candidates_evaluated == (MAX_TREE_LAYERS ** MAX_QUASI_IDENTIFIERS),
              f"observed {deep.candidates_evaluated}")
    chk.check("scale: group basis sizes sum to record count",
              sum(g.size for g in deep.groups) == 4)

    # beyond declared limits rejected
    too_many = dict(deep_qi)
    too_many["f6"] = deep_qi["f0"]
    try:
        anonymize(deep_rec, quasi_identifiers=too_many, sensitive_field="cond",
                  k=2, l=2, max_suppressed=4, loss_weights={
                      **deep_weights, "f6": [0, 1, 2, 3]}, suppression_penalty=1)
        chk.check("scale: 7 quasi-identifiers rejected", False)
    except ValueError:
        chk.check("scale: 7 quasi-identifiers rejected", True)
    try:
        GeneralizationTree(GenNode("r", [GenNode("a", [GenNode("b", [
            GenNode("c", [GenNode("d")])])])]))
        chk.check("scale: 5-layer tree rejected", False)
    except ValueError:
        chk.check("scale: 5-layer tree rejected", True)
    try:
        anonymize(recs, quasi_identifiers=qi, sensitive_field="cond",
                  k=2, l=2, max_suppressed=5, loss_weights={"s": [0]},
                  suppression_penalty=2)
        chk.check("scale: wrong weight-vector length rejected", False)
    except ValueError:
        chk.check("scale: wrong weight-vector length rejected", True)

    summary = {
        "samples": "synthetic: 5 singleton records (budget boundary); "
                   "4 records x 6 QI fields x 4 layers (max scale)",
        "expected": {"min_suppression": 5, "budget_ok": 5, "budget_fail": 4,
                     "max_candidates": MAX_TREE_LAYERS ** MAX_QUASI_IDENTIFIERS},
        "observed": {"suppressed_at_budget": ok.suppressed_count,
                     "infeasible_reason": no.reason,
                     "max_candidates": deep.candidates_evaluated},
    }
    return chk, summary


CASES = {
    "C01": ("Exhaustive small-domain cross-check: k/l constraints, whole-group "
            "suppression, minimum total loss vs independent brute force", case_c01),
    "C02": ("Loss accounting (generalization + suppression penalty) and "
            "deterministic tie-break by level vector", case_c02),
    "C03": ("Node identity vs repeated display names, independent missing "
            "leaf, uniform per-field level", case_c03),
    "C04": ("Suppression budget boundary, infeasible case, max declared "
            "scale, per-group verification basis", case_c04),
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list cases and exit")
    parser.add_argument("--case", choices=sorted(CASES), help="run one case")
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps(
            {"seed": SEED,
             "cases": [{"case": cid, "description": desc}
                       for cid, (desc, _) in sorted(CASES.items())]},
            indent=2))
        return 0
    if not args.case:
        parser.error("one of --list or --case is required")

    desc, fn = CASES[args.case]
    try:
        chk, summary = fn()
        error = None
    except Exception:
        chk, summary, error = Checker(), {}, traceback.format_exc()

    passed = sum(1 for r in chk.results if r["passed"])
    failed = len(chk.results) - passed
    record = {
        "case": args.case,
        "description": desc,
        "seed": SEED,
        "assertions": len(chk.results),
        "passed": passed,
        "failed": failed,
        "skipped": 0,
        "summary": summary,
        "failures": [r for r in chk.results if not r["passed"]],
    }
    if error is not None:
        record["error"] = error
    print(json.dumps(record, indent=2, ensure_ascii=False))

    if error is not None or failed > 0 or len(chk.results) == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
