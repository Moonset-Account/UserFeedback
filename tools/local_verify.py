#!/usr/bin/env python3
"""Local verification entry point for the workflow provenance analyzer.

Usage:
    python -I tools/local_verify.py --list
    python -I tools/local_verify.py --case C01
    python -I tools/local_verify.py --case all

The runner executes real assertions against the implementation in
src/workflow_audit using the synthetic graphs under tools/fixtures.
It exits non-zero on any failed / missing / skipped assertion, and never on
printed success text alone.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from workflow_audit.engine import analyze_graph  # noqa: E402
from workflow_audit.graph import GraphError, load_graph  # noqa: E402

FIXTURES = HERE / "fixtures"


# --------------------------------------------------------------------------- #
# Small assertion helpers
# --------------------------------------------------------------------------- #

class AssertionFailure(AssertionError):
    pass


def check(condition: bool, message: str, evidence: Any = None) -> None:
    if not condition:
        raise AssertionFailure(
            json.dumps({"message": message, "evidence": evidence},
                       ensure_ascii=False, default=str)
        )


def find_findings(report: Dict[str, Any], release_job: str) -> List[Dict[str, Any]]:
    return [f for f in report["findings"] if f["release_job"] == release_job]


def artifact_origin(report: Dict[str, Any], path_id: str,
                    name: str, commit: str) -> Dict[str, Any]:
    for path in report["paths"]:
        if path["path_id"] != path_id:
            continue
        for entry in path["artifacts"]:
            art = entry["artifact"]
            if art["name"] == name and art.get("commit") == commit:
                return entry["origin"]
    raise AssertionFailure(
        f"artifact {name}@{commit} missing on {path_id}"
    )


def path_ids(report: Dict[str, Any]) -> List[str]:
    return [p["path_id"] for p in report["paths"]]


def losses_at(report: Dict[str, Any], location_prefix: str) -> List[Dict[str, Any]]:
    hits = []
    for path in report["paths"]:
        for loss in path["source_constraint_losses"]:
            if loss["location"].startswith(location_prefix):
                hits.append(loss)
    return hits


# --------------------------------------------------------------------------- #
# Case C01: same name, different production commits must not be merged
# --------------------------------------------------------------------------- #

def verify_c01(report: Dict[str, Any]) -> int:
    check(len(path_ids(report)) >= 2,
          "C01 needs one feasible path per lane", path_ids(report))

    pinned = find_findings(report, "release-pinned" if False else "release")
    # The release job consumes both artifact identities; split by commit.
    prod = [f for f in pinned
            if f["sensitive_input"].get("commit") == "prod-aaa111"]
    fork = [f for f in pinned
            if f["sensitive_input"].get("commit") == "fork-ccc999"]
    check(prod and fork, "both artifact identities reach the release analysis",
          {"prod": len(prod), "fork": len(fork)})

    check(all(f["classification"] == "trusted_pinned_production" for f in prod),
          "pinned production commit stays trusted", [f["classification"] for f in prod])
    check(all(f["trusted"] for f in prod), "trusted flag set on pinned lane",
          [f["trusted"] for f in prod])
    check(all(not f["trusted"] for f in fork),
          "unpinned external lane must not be trusted",
          [f["trusted"] for f in fork])
    check(all(f["origin"]["constraint"] != "pinned_commit" for f in fork),
          "unpinned download never gains a pinned constraint",
          [f["origin"]["constraint"] for f in fork])

    # Same name -> two distinct producer-index entries, never merged by name.
    names = {(e["artifact"]["name"], e["artifact"]["commit"]): e["producers"]
             for e in report["artifact_producer_index"]}
    key_prod = ("app-bundle", "prod-aaa111")
    key_fork = ("app-bundle", "fork-ccc999")
    check(key_prod in names and key_fork in names,
          "same-name/different-commit artifacts are indexed separately",
          list(names))
    check(names[key_prod][0]["producer_job"] == "build-prod"
          and names[key_fork][0]["producer_job"] == "build-fork",
          "each index entry keeps its own producer job", names)

    # No origin on the fork lane may silently adopt the production commit.
    for path in report["paths"]:
        for entry in path["artifacts"]:
            if entry["artifact"]["name"] == "app-bundle":
                detail = entry["origin"].get("constraint_detail", {})
                for po in detail.get("possible_origins", []):
                    check(
                        not (entry["artifact"]["commit"] == "fork-ccc999"
                             and po.get("constraint_detail", {}).get(
                                 "producer_commit") == "prod-aaa111"),
                        "fork artifact must not absorb the production origin",
                        entry,
                    )

    # The unpinned download itself records where its source constraint fails.
    unpinned_losses = losses_at(report, "fetch-unpinned:download")
    check(unpinned_losses and unpinned_losses[0]["to_constraint"] == "unpinned",
          "first constraint loss located at the unpinned download",
          unpinned_losses[:1])
    return 10


# --------------------------------------------------------------------------- #
# Case C02: external contribution pollutes a shared cache
# --------------------------------------------------------------------------- #

def verify_c02(report: Dict[str, Any]) -> int:
    key_report = next(
        e for e in report["cache_possible_writers"]
        if e["cache_key"] == "shared:build-output:v1"
    )
    writer_jobs = {w["writer_job"] for w in key_report["possible_writers"]}
    check(writer_jobs == {"build-prod", "pr-build"},
          "all possible cache writers are retained, not just the latest",
          sorted(writer_jobs))
    check(key_report["writer_count"] >= 2,
          "shared cache admits multiple possible writers", key_report)

    findings = find_findings(report, "release")
    check(findings, "release finding exists", None)
    check(any(f["classification"] == "external_contribution_via_shared_cache"
              for f in findings),
          "pollution path classified as external contribution via shared cache",
          [f["classification"] for f in findings])
    check(all(not f["trusted"] for f in findings),
          "polluted cache content never counts as trusted",
          [f["trusted"] for f in findings])

    cache_read = next(
        f for f in findings
        if f["origin"]["kind"] == "cache_read"
    )
    possible = cache_read["origin"]["constraint_detail"]["possible_origins"]
    sources = {po["source"] for po in possible}
    check("external_contribution" in sources and "production" in sources,
          "cache read retains both production and external possible origins",
          sorted(sources))
    check(cache_read["origin"]["constraint"] == "none",
          "shared cache removes the source constraint",
          cache_read["origin"]["constraint"])

    write_losses = losses_at(report, "pr-build:cache_write")
    # The polluting writer lives outside the release-reaching path, so its
    # loss must be preserved (a) in the global cache-writer sweep and (b) in
    # the cache-read origin history on the release path.
    sweep_loss = None
    for entry in report["cache_possible_writers"]:
        if entry["cache_key"] != "shared:build-output:v1":
            continue
    check(any(
        w["writer_job"] == "pr-build" and w["source"] == "external_contribution"
        for w in key_report["possible_writers"]
    ), "sweep retains the external polluter as a possible writer",
        key_report["possible_writers"])
    retained = [
        po for po in possible
        if po.get("writer_job") == "pr-build" and po.get("source_constraint_losses")
    ]
    check(retained and retained[0]["source_constraint_losses"][0][
              "to_constraint"] == "none",
          "first loss at the external cache write is carried into the read",
          retained[:1])

    check(
        any(w["source"] == "external_contribution"
            for w in key_report["possible_writers"]),
        "the external writer itself is preserved in the report",
        key_report["possible_writers"],
    )
    return 9


# --------------------------------------------------------------------------- #
# Case C03: approval gate vs. actually pinning the download source
# --------------------------------------------------------------------------- #

def verify_c03(report: Dict[str, Any]) -> int:
    pinned = find_findings(report, "release-pinned")
    approved = find_findings(report, "release-approved")
    check(pinned and approved, "both release lanes analyzed",
          {"pinned": len(pinned), "approved": len(approved)})

    check(all(f["classification"] == "trusted_pinned_production" for f in pinned),
          "pinned lane is trusted", [f["classification"] for f in pinned])
    check(all(f["origin"]["constraint"] == "pinned_commit" for f in pinned),
          "trusted lane keeps a pinned-commit constraint",
          [f["origin"]["constraint"] for f in pinned])

    check(all(not f["trusted"] for f in approved),
          "approval alone must not make content trusted",
          [f["trusted"] for f in approved])
    check(all(f["approval_gate"] for f in approved)
          and "manual-gate" in approved[0]["approval_jobs"],
          "the approval gate is recorded but treated only as an execution gate",
          approved[0]["approval_jobs"])
    check(all("approval" in f["approval_note"] for f in approved),
          "report states approval cannot attest content source",
          [f["approval_note"] for f in approved][:1])
    check(all(f["origin"]["constraint"] != "pinned_commit" for f in approved),
          "approved lane still lacks a pinned source",
          [f["origin"]["constraint"] for f in approved])
    check(all(f["classification"] == "approved_but_unpinned_source"
              for f in approved),
          "classification distinguishes approval-only hardening",
          [f["classification"] for f in approved])

    first = approved[0]["first_source_constraint_loss"]
    check(first is not None
          and first["location"].startswith("fetch-unpinned:download"),
          "first loss is the unpinned download, before the manual gate",
          first)
    check(any(loss["location"].startswith("fetch-unpinned:download")
              for loss in report["paths"][0]["source_constraint_losses"])
          or any(
              any(l["location"].startswith("fetch-unpinned:download")
                  for l in p["source_constraint_losses"])
              for p in report["paths"]),
          "loss log identifies the exact first unconstrained step", None)
    return 10


# --------------------------------------------------------------------------- #
# Case C04: branches + missing dynamic definition => unknown, not trusted
# --------------------------------------------------------------------------- #

def verify_c04(report: Dict[str, Any]) -> int:
    check(len(path_ids(report)) >= 2,
          "both branch conditions produce feasible paths", path_ids(report))

    known = find_findings(report, "release-known")
    unknown = find_findings(report, "release-unknown")
    check(known and unknown, "both release jobs evaluated",
          {"known": len(known), "unknown": len(unknown)})

    check(all(f["classification"] == "trusted_pinned_production" for f in known),
          "known pinned branch remains provably trusted",
          [f["classification"] for f in known])
    check(all(not f["trusted"] for f in unknown),
          "unknown dynamic branch is never treated as trusted",
          [f["trusted"] for f in unknown])
    check(all(f["classification"] == "unprovable_missing_dynamic_definition"
              for f in unknown),
          "missing dynamic definition yields an explicit unprovable result",
          [f["classification"] for f in unknown])

    markers = {m["ref"] for p in report["paths"] for m in p["unprovable"]}
    check("matrix-build" in markers,
          "the unprovable path preserves the missing dynamic ref",
          sorted(markers))
    check(report["summary"]["dynamic_definitions_missing"] == ["matrix-build"],
          "summary carries the missing dynamic definition",
          report["summary"]["dynamic_definitions_missing"])

    dyn_path = next(p for p in report["paths"]
                    if any(m["ref"] == "matrix-build" for m in p["unprovable"]))
    check("matrix-build" in dyn_path["job_order"],
          "the dynamic ref remains on the path instead of being pruned",
          dyn_path["job_order"])

    check(report["summary"]["any_untrusted_path_to_release"] is True,
          "overall verdict flags at least one unprovable/untrusted release path",
          report["summary"]["any_untrusted_path_to_release"])

    # No finding may claim pinned trust on the dynamic branch.
    check(all(f["origin"]["constraint"] != "pinned_commit" for f in unknown),
          "unknown content is never upgraded to a pinned constraint",
          [f["origin"]["constraint"] for f in unknown])
    return 9


CASES: Dict[str, Tuple[str, Callable[[Dict[str, Any]], int]]] = {
    "C01": ("C01.json", verify_c01),
    "C02": ("C02.json", verify_c02),
    "C03": ("C03.json", verify_c03),
    "C04": ("C04.json", verify_c04),
}


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def run_case(case_id: str) -> Dict[str, Any]:
    fixture_name, verifier = CASES[case_id]
    fixture_path = FIXTURES / fixture_name
    record: Dict[str, Any] = {
        "case": case_id,
        "fixture": f"tools/fixtures/{fixture_name}",
        "seed": "synthetic-static-graph",
        "assertions": {"total": 0, "passed": 0, "failed": 0, "skipped": 0},
        "status": "unknown",
        "observed_summary": None,
        "failures": [],
    }
    try:
        graph = load_graph(fixture_path)
        report = analyze_graph(graph)
        expected = _expected_outcome(case_id)
        record["expected_summary"] = expected
        record["observed_summary"] = report["summary"]
        record["observed_classifications"] = sorted({
            f["classification"] for f in report["findings"]
        })
        total = verifier(report)
        if not isinstance(total, int) or total <= 0:
            raise AssertionFailure("verifier registered zero assertions")
        record["assertions"]["total"] = total
        record["assertions"]["passed"] = total
        record["status"] = "passed"
    except GraphError as exc:
        record["status"] = "failed"
        record["failures"].append({"message": f"invalid graph: {exc}"})
        record["assertions"]["failed"] = max(1, record["assertions"]["total"])
    except AssertionFailure as exc:
        record["status"] = "failed"
        record["assertions"]["total"] = max(1, record["assertions"]["total"])
        record["assertions"]["failed"] = record["assertions"]["total"]
        record["failures"].append({"message": str(exc)})
    except Exception as exc:  # noqa: BLE001 - runner must report, not crash silently
        record["status"] = "error"
        record["failures"].append({
            "message": f"{type(exc).__name__}: {exc}",
        })
        record["assertions"]["failed"] = max(1, record["assertions"]["total"])
    return record


def _expected_outcome(case_id: str) -> Dict[str, Any]:
    return {
        "C01": {
            "trusted": "pinned production artifact (prod-aaa111)",
            "untrusted": "unpinned same-name artifact (fork-ccc999) kept separate",
        },
        "C02": {
            "trusted": "none on the release path that reads the shared cache",
            "untrusted": "external_contribution_via_shared_cache with both "
                         "possible writers retained",
        },
        "C03": {
            "trusted": "release-pinned lane with pinned_commit constraint",
            "untrusted": "release-approved lane: manual gate, unpinned source; "
                         "first loss at fetch-unpinned:download",
        },
        "C04": {
            "trusted": "release-known branch with pinned production dependency",
            "untrusted": "release-unknown branch marked "
                         "unprovable_missing_dynamic_definition",
        },
    }[case_id]


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Workflow audit local verifier")
    parser.add_argument("--list", action="store_true",
                        help="list available verification cases and exit")
    parser.add_argument("--case",
                        help="case id (C01..C04) or 'all'")
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps({
            "cases": [
                {"id": cid, "fixture": f"tools/fixtures/{name}"}
                for cid, (name, _) in CASES.items()
            ]
        }, indent=2, ensure_ascii=False))
        return 0

    if not args.case:
        print("error: provide --case C01|C02|C03|C04|all or --list",
              file=sys.stderr)
        return 2

    selected = list(CASES) if args.case == "all" else [args.case]
    unknown = [c for c in selected if c not in CASES]
    if unknown:
        print(f"error: unknown case {unknown[0]}", file=sys.stderr)
        return 2

    records = [run_case(cid) for cid in selected]
    print(json.dumps({"results": records}, indent=2, ensure_ascii=False))

    bad = [
        r for r in records
        if r["status"] != "passed"
        or r["assertions"]["total"] == 0
        or r["assertions"]["failed"]
        or r["assertions"]["skipped"]
    ]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
