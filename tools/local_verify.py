#!/usr/bin/env python3
"""Local verification harness for the wasmcap capability analysis.

Usage:
    python tools/local_verify.py --list
    python tools/local_verify.py --case C01
    python tools/local_verify.py --case ALL

Each case loads a synthetic, replayable sample from ``samples/``, runs the
real analysis entry points (``wasmcap.load_summary`` / ``wasmcap.analyze``)
and records genuine assertions.  The process exits non-zero if any
assertion fails, a case is unknown/unimplemented, a case records zero
assertions, or a whole group is skipped.  This wrapper is a run entry
point, not proof of correctness on its own: auditors should still read
the implementation and the emitted reports.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wasmcap.analyze import analyze  # noqa: E402
from wasmcap.model import load_summary  # noqa: E402

SAMPLES_DIR = ROOT / "samples"


class Recorder:
    """Collects named assertions with expected/observed summaries."""

    def __init__(self):
        self.results = []

    def check(self, name, ok, expected=None, observed=None):
        self.results.append({
            "name": name,
            "status": "pass" if ok else "fail",
            "expected": expected,
            "observed": observed,
        })

    @property
    def passed(self):
        return sum(1 for r in self.results if r["status"] == "pass")

    @property
    def failed(self):
        return sum(1 for r in self.results if r["status"] == "fail")


def _load(sample_name):
    return load_summary(SAMPLES_DIR / sample_name)


def _single_entry_report(summary):
    reports = analyze(summary)["entries"]
    assert len(reports) == 1, "sample must declare exactly one entry"
    return reports[0]


def _exclusions_for(report, function):
    return [e for e in report["excluded_elements"] if e["function"] == function]


# --------------------------------------------------------------------------
# C01: direct calls and signature-compatible indirect calls reach host
# capabilities with evidence paths; incompatible elements are excluded.
# --------------------------------------------------------------------------
def case_c01(rec):
    sample = "c01_direct_indirect.json"
    report = _single_entry_report(_load(sample))

    rec.check(
        "capabilities from direct + compatible indirect calls",
        report["capabilities"] == ["log", "net.send"],
        expected=["log", "net.send"],
        observed=report["capabilities"],
    )
    rec.check(
        "reachable host functions",
        report["reachable_host_functions"] == ["h_log", "h_net_send"],
        expected=["h_log", "h_net_send"],
        observed=report["reachable_host_functions"],
    )
    ev_log = report["evidence"].get("h_log", [])
    rec.check(
        "direct-call evidence path for h_log",
        len(ev_log) == 2
        and ev_log[0]["function"] == "f_main"
        and ev_log[1].get("via", {}).get("kind") == "direct"
        and ev_log[1]["via"].get("site") == "f_main@10",
        expected="f_main --direct@f_main@10--> h_log",
        observed=ev_log,
    )
    ev_net = report["evidence"].get("h_net_send", [])
    indirect_steps = [s for s in ev_net if s.get("via", {}).get("kind") == "indirect"]
    rec.check(
        "indirect-call evidence path for h_net_send (structurally compatible alias type)",
        len(indirect_steps) == 1
        and indirect_steps[0]["via"].get("table") == "tab"
        and indirect_steps[0]["via"].get("index") == 0
        and indirect_steps[0]["via"].get("origin") == "initial"
        and indirect_steps[0]["via"].get("site") == "f_dispatch@22",
        expected="one indirect step via tab[0] (initial) at f_dispatch@22",
        observed=ev_net,
    )
    excl = _exclusions_for(report, "h_crypto_sign")
    rec.check(
        "signature-incompatible table element excluded with type_mismatch",
        len(excl) == 1
        and excl[0]["reason"] == "type_mismatch"
        and excl[0]["expected_type"] == "t_void"
        and excl[0]["actual_type"] == "t_i32",
        expected="h_crypto_sign excluded: type_mismatch t_i32 vs t_void",
        observed=excl,
    )
    rec.check(
        "crypto.sign capability not attributed to the entry",
        "crypto.sign" not in report["capabilities"],
        expected="absent",
        observed=report["capabilities"],
    )
    rec.check(
        "no violations and no unknown targets",
        report["violations"] == [] and report["unknown"] is False,
        expected={"violations": [], "unknown": False},
        observed={"violations": report["violations"], "unknown": report["unknown"]},
    )
    rec.check(
        "analysis converged",
        report["converged"] is True and report["iterations"] >= 1,
        expected="converged=True, iterations>=1",
        observed={"converged": report["converged"], "iterations": report["iterations"]},
    )
    return sample


# --------------------------------------------------------------------------
# C02: reachable update vs unreachable update vs type mismatch vs
# externally writable table (unknown targets must not read as no capability).
# --------------------------------------------------------------------------
def case_c02(rec):
    sample = "c02_updates_unknown.json"
    report = _single_entry_report(_load(sample))

    rec.check(
        "reachable table update contributes h_write as indirect target",
        "h_write" in report["reachable_host_functions"],
        expected="h_write reachable",
        observed=report["reachable_host_functions"],
    )
    ev_write = report["evidence"].get("h_write", [])
    update_steps = [s for s in ev_write
                    if s.get("via", {}).get("kind") == "indirect"
                    and s["via"].get("origin") == "update"]
    rec.check(
        "evidence for h_write cites the reachable update program point",
        len(update_steps) == 1 and update_steps[0]["via"].get("update_at") == "f_setup@5",
        expected="indirect step with origin=update, update_at=f_setup@5",
        observed=ev_write,
    )
    rec.check(
        "unreachable update does not contribute h_admin",
        "h_admin" not in report["reachable_host_functions"]
        and "admin" not in report["capabilities"],
        expected="h_admin unreachable, capability 'admin' absent",
        observed={"hosts": report["reachable_host_functions"],
                  "capabilities": report["capabilities"]},
    )
    excl_admin = _exclusions_for(report, "h_admin")
    rec.check(
        "h_admin exclusion explained as unreachable_update",
        len(excl_admin) == 1
        and excl_admin[0]["reason"] == "unreachable_update"
        and excl_admin[0]["update_at"] == "f_orphan@3",
        expected="unreachable_update at f_orphan@3",
        observed=excl_admin,
    )
    excl_odd = _exclusions_for(report, "h_odd")
    rec.check(
        "reachable update with wrong signature excluded as type_mismatch",
        len(excl_odd) == 1
        and excl_odd[0]["reason"] == "type_mismatch"
        and excl_odd[0]["actual_type"] == "t_i32",
        expected="type_mismatch (t_i32 vs t_void) for reachable update f_setup@9",
        observed=excl_odd,
    )
    rec.check(
        "externally writable table marks targets unknown (not 'no capability')",
        report["unknown"] is True
        and any(r.get("table") == "tabB" and r.get("site") == "f_main@40"
                for r in report["unknown_reasons"]),
        expected="unknown=True with reason citing tabB at f_main@40",
        observed={"unknown": report["unknown"],
                  "unknown_reasons": report["unknown_reasons"]},
    )
    rec.check(
        "known capabilities and violations still reported alongside unknown",
        report["capabilities"] == ["fs.read", "fs.write"]
        and report["violations"] == [],
        expected={"capabilities": ["fs.read", "fs.write"], "violations": []},
        observed={"capabilities": report["capabilities"],
                  "violations": report["violations"]},
    )
    return sample


# --------------------------------------------------------------------------
# C03: shared functions across entries, a call cycle, and repeated table
# updates must converge with per-entry conclusions kept isolated.
# --------------------------------------------------------------------------
def case_c03(rec):
    sample = "c03_entries_convergence.json"
    summary = _load(sample)
    reports = {r["entry"]: r for r in analyze(summary)["entries"]}

    rec.check(
        "one isolated report per entry",
        sorted(reports) == ["e1", "e2"],
        expected=["e1", "e2"],
        observed=sorted(reports),
    )
    e1, e2 = reports["e1"], reports["e2"]
    rec.check(
        "shared function f_shared reachable from both entries",
        "f_shared" in e1["reachable_functions"] and "f_shared" in e2["reachable_functions"],
        expected="f_shared reachable in both",
        observed={"e1": "f_shared" in e1["reachable_functions"],
                  "e2": "f_shared" in e2["reachable_functions"]},
    )
    rec.check(
        "entry e1 conclusion isolated from e2-only capability",
        e1["capabilities"] == ["cap.a"] and e1["reachable_host_functions"] == ["h_a"],
        expected={"capabilities": ["cap.a"], "hosts": ["h_a"]},
        observed={"capabilities": e1["capabilities"],
                  "hosts": e1["reachable_host_functions"]},
    )
    rec.check(
        "entry e2 sees direct h_b plus repeated-update targets",
        e2["capabilities"] == ["cap.a", "cap.b"]
        and e2["reachable_host_functions"] == ["h_a", "h_b"],
        expected={"capabilities": ["cap.a", "cap.b"], "hosts": ["h_a", "h_b"]},
        observed={"capabilities": e2["capabilities"],
                  "hosts": e2["reachable_host_functions"]},
    )
    e1_setup2_excl = [e for e in e1["excluded_elements"]
                      if e["reason"] == "unreachable_update"
                      and e["update_at"].startswith("f_setup2@")]
    rec.check(
        "e1 excludes e2-only table updates as unreachable (entry isolation)",
        sorted(e["update_at"] for e in e1_setup2_excl) == ["f_setup2@2", "f_setup2@5"],
        expected=["f_setup2@2", "f_setup2@5"],
        observed=sorted(e["update_at"] for e in e1_setup2_excl),
    )
    e2_setup1_excl = [e for e in e2["excluded_elements"]
                      if e["reason"] == "unreachable_update"
                      and e["update_at"].startswith("f_setup1@")]
    rec.check(
        "e2 excludes e1-only table update as unreachable (isolation is symmetric)",
        sorted(e["update_at"] for e in e2_setup1_excl) == ["f_setup1@2"],
        expected=["f_setup1@2"],
        observed=sorted(e["update_at"] for e in e2_setup1_excl),
    )
    rec.check(
        "call cycle and repeated updates converge in multiple monotone rounds",
        e1["converged"] and e2["converged"]
        and e1["iterations"] >= 2 and e2["iterations"] >= 2,
        expected="converged=True, iterations>=2 for both entries",
        observed={"e1": {"converged": e1["converged"], "iterations": e1["iterations"]},
                  "e2": {"converged": e2["converged"], "iterations": e2["iterations"]}},
    )
    again = json.dumps(analyze(summary), sort_keys=True)
    first = json.dumps({"entries": [reports["e1"], reports["e2"]]}, sort_keys=True)
    rec.check(
        "re-running the analysis is deterministic (fixpoint is stable)",
        again == first,
        expected="identical serialized reports on re-run",
        observed="identical" if again == first else "diverged",
    )
    return sample


# --------------------------------------------------------------------------
# C04: capabilities beyond the entry's allowed set are named; sensitive
# table elements that cannot affect the entry are excluded with reasons.
# --------------------------------------------------------------------------
def case_c04(rec):
    sample = "c04_overprivilege_exclusions.json"
    report = _single_entry_report(_load(sample))

    rec.check(
        "over-privilege capability named exactly",
        report["violations"] == ["shell.exec"],
        expected=["shell.exec"],
        observed=report["violations"],
    )
    rec.check(
        "full capability set of the entry",
        report["capabilities"] == ["shell.exec", "ui"],
        expected=["shell.exec", "ui"],
        observed=report["capabilities"],
    )
    ev_shell = report["evidence"].get("h_shell", [])
    rec.check(
        "evidence path for over-privilege goes through f_helper",
        [s["function"] for s in ev_shell] == ["f_main", "f_helper", "h_shell"]
        and all(s.get("via", {}).get("kind") == "direct" for s in ev_shell[1:]),
        expected="f_main -> f_helper -> h_shell (direct)",
        observed=ev_shell,
    )
    excl_vault = _exclusions_for(report, "h_vault")
    rec.check(
        "sensitive h_vault table element excluded: type_mismatch",
        len(excl_vault) == 1
        and excl_vault[0]["reason"] == "type_mismatch"
        and excl_vault[0]["expected_type"] == "t_void"
        and excl_vault[0]["actual_type"] == "t_i32"
        and "vault.read" not in report["capabilities"],
        expected="type_mismatch t_i32 vs t_void; vault.read absent",
        observed={"exclusion": excl_vault, "capabilities": report["capabilities"]},
    )
    excl_format = _exclusions_for(report, "h_format")
    rec.check(
        "sensitive h_format update excluded: unreachable_update",
        len(excl_format) == 1
        and excl_format[0]["reason"] == "unreachable_update"
        and excl_format[0]["update_at"] == "f_orphan@2"
        and "disk.format" not in report["capabilities"],
        expected="unreachable_update at f_orphan@2; disk.format absent",
        observed={"exclusion": excl_format, "capabilities": report["capabilities"]},
    )
    rec.check(
        "sensitive functions not in reachable host set",
        "h_vault" not in report["reachable_host_functions"]
        and "h_format" not in report["reachable_host_functions"],
        expected="neither h_vault nor h_format reachable",
        observed=report["reachable_host_functions"],
    )
    return sample


CASES = {
    "C01": {
        "description": "direct calls and signature-compatible indirect calls yield "
                       "host capabilities with evidence paths",
        "sample": "c01_direct_indirect.json",
        "fn": case_c01,
    },
    "C02": {
        "description": "reachable update vs unreachable update vs type mismatch vs "
                       "externally writable table (unknown targets)",
        "sample": "c02_updates_unknown.json",
        "fn": case_c02,
    },
    "C03": {
        "description": "shared functions, call cycle and repeated table updates "
                       "converge with per-entry isolation",
        "sample": "c03_entries_convergence.json",
        "fn": case_c03,
    },
    "C04": {
        "description": "over-privilege capabilities are named; sensitive table "
                       "elements excluded with type_mismatch / unreachable_update",
        "sample": "c04_overprivilege_exclusions.json",
        "fn": case_c04,
    },
}


def run_case(case_id):
    spec = CASES.get(case_id)
    if spec is None:
        return None
    rec = Recorder()
    sample = spec["sample"]
    try:
        sample = spec["fn"](rec)
    except Exception as exc:  # an unexpected error is a failed assertion
        rec.check("case executes without internal error", False,
                  expected="no exception", observed=f"{type(exc).__name__}: {exc}")
    if not rec.results:
        rec.check("case records at least one assertion", False,
                  expected=">=1 assertion", observed=0)
    record = {
        "case": case_id,
        "description": spec["description"],
        "sample": f"samples/{sample}",
        "assertions": len(rec.results),
        "passed": rec.passed,
        "failed": rec.failed,
        "skipped": 0,
        "results": rec.results,
    }
    record["ok"] = record["failed"] == 0 and record["assertions"] > 0
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="list available cases")
    group.add_argument("--case", help="run one case (C01..C04) or ALL")
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps({
            "cases": [
                {"case": cid, "description": spec["description"],
                 "sample": f"samples/{spec['sample']}"}
                for cid, spec in CASES.items()
            ]
        }, indent=2))
        return 0

    if args.case == "ALL":
        records = [run_case(cid) for cid in CASES]
        ok = all(r["ok"] for r in records)
        print(json.dumps({"cases": records, "ok": ok}, indent=2))
        return 0 if ok else 1

    record = run_case(args.case)
    if record is None:
        print(json.dumps({"error": f"unknown case {args.case!r}",
                          "known_cases": sorted(CASES)}))
        return 2
    print(json.dumps(record, indent=2))
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
