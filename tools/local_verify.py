#!/usr/bin/env python3
"""Local verification wrapper for the secret reference resolver.

This wrapper is the single reproducible entry point used by review.  It does
two independent things and reports each result honestly:

1. Runs the real unittest suite under ``tests/`` via
   ``unittest.defaultTestLoader.discover`` and refuses to claim success when
   zero tests were discovered, zero actually ran, everything was skipped, or
   any test failed.
2. Executes the delivered sample and boundary assertions one by one, keeping
   the actual observed value next to the expected value for every case.

Exit code is 0 only when both parts pass with real, non-skipped evidence.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TIME_BUDGET_SECONDS = 60
START = time.monotonic()

SAMPLES = ROOT / "samples" / "prod"


class RecordingResult(unittest.TextTestResult):
    """TextTestResult that remembers per-test skip state."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.skipped_details: list[tuple[str, str]] = []

    def addSkip(self, test, reason) -> None:  # noqa: N802 - unittest API
        self.skipped_details.append((test.id(), reason))
        super().addSkip(test, reason)


def _deadline_handler(signum, frame):  # noqa: ARG001
    raise TimeoutError(f"local verification exceeded {TIME_BUDGET_SECONDS}s budget")


def check(records: list[dict], case_id: str, description: str, expected, actual, passed: bool) -> None:
    records.append(
        {
            "case_id": case_id,
            "description": description,
            "expected": expected,
            "actual": actual,
            "passed": bool(passed),
        }
    )


def run_unit_tests() -> dict:
    """Discover and run tests; return an evidence dict."""

    loader = unittest.defaultTestLoader
    suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    discovered = suite.countTestCases()
    evidence: dict = {"discovered": discovered}

    if discovered < 1:
        evidence.update(
            {
                "ran": 0,
                "skipped": 0,
                "executed_non_skipped": 0,
                "failures": 0,
                "errors": 0,
                "passed": False,
                "reason": "discovered zero tests",
            }
        )
        return evidence

    stream = sys.stdout
    runner = unittest.TextTestRunner(
        stream=stream, verbosity=2, resultclass=RecordingResult
    )
    started = time.monotonic()
    result = runner.run(suite)
    elapsed = round(time.monotonic() - started, 3)

    ran = result.testsRun
    skipped = len(result.skipped_details)
    non_skipped = ran - skipped
    failures = len(result.failures)
    errors = len(result.errors)
    reasons = []
    if ran < 1:
        reasons.append("no tests actually ran")
    if non_skipped < 1:
        reasons.append("every executed test was skipped")
    if failures:
        reasons.append(f"{failures} failing test(s)")
    if errors:
        reasons.append(f"{errors} erroring test(s)")

    evidence.update(
        {
            "ran": ran,
            "skipped": skipped,
            "skipped_details": result.skipped_details,
            "executed_non_skipped": non_skipped,
            "failures": failures,
            "errors": errors,
            "elapsed_seconds": elapsed,
            "passed": not reasons,
            "reason": "; ".join(reasons) if reasons else "",
        }
    )
    return evidence


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "app.py"), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _service(payload: dict, name: str) -> dict:
    for item in payload["services"]:
        if item["service"] == name:
            return item
    raise AssertionError(f"service {name} not present in plan")


def _variable(service: dict, name: str) -> dict:
    for item in service["variables"]:
        if item["variable"] == name:
            return item
    raise AssertionError(f"variable {name} not present in service {service['service']}")


def run_sample_cases() -> list[dict]:
    """Delivered sample plus boundary assertions against the shipped inputs."""

    records: list[dict] = []
    env_path = str(SAMPLES / "environment.json")
    svc_path = str(SAMPLES / "services.json")
    cat_path = str(SAMPLES / "catalog.json")

    result = _cli("--environment", env_path, "--services", svc_path, "--catalog", cat_path)
    check(
        records,
        "SAMPLE-EXIT",
        "prod sample exits 0 (all services executable)",
        {"returncode": 0},
        {"returncode": result.returncode, "stderr": result.stderr.strip()},
        result.returncode == 0,
    )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        check(records, "SAMPLE-JSON", "prod sample emits valid JSON", "parseable", str(exc), False)
        return records

    check(records, "SAMPLE-JSON", "prod sample emits valid JSON", "parseable", "parseable", True)

    api = _service(payload, "api")
    worker = _service(payload, "worker")
    billing = _service(payload, "billing")

    # Environment layer overrides common for every service.
    public_db = {
        name: {
            "layer": _variable(svc, "DB_PASSWORD")["source_layer"],
            "version": _variable(svc, "DB_PASSWORD")["version"],
        }
        for name, svc in (("api", api), ("billing", billing))
    }
    check(
        records,
        "SAMPLE-ENV-OVERRIDE",
        "common DB_PASSWORD ref is overridden by the environment layer",
        {"api": {"layer": "environment", "version": 3},
         "billing": {"layer": "environment", "version": 3}},
        public_db,
        public_db == {"api": {"layer": "environment", "version": 3},
                      "billing": {"layer": "environment", "version": 3}},
    )

    # Service layer beats environment for worker.
    worker_db = {
        "layer": _variable(worker, "DB_PASSWORD")["source_layer"],
        "version": _variable(worker, "DB_PASSWORD")["version"],
    }
    check(
        records,
        "SAMPLE-SVC-OVERRIDE",
        "service layer overrides the environment layer for worker DB_PASSWORD",
        {"layer": "service", "version": 4},
        worker_db,
        worker_db == {"layer": "service", "version": 4},
    )

    # Two-hop alias chain: legacy -> v2 -> base, at catalog current version.
    signing = _variable(api, "API_SIGNING_KEY")
    signing_actual = {
        "resolved_ref": signing["resolved_ref"],
        "version": signing["version"],
        "chain": [step["ref"] for step in signing["resolution_chain"]],
    }
    check(
        records,
        "SAMPLE-ALIAS-CHAIN",
        "api signing key follows a two-hop alias to the base reference",
        {"resolved_ref": "api-signing-key", "version": 2,
         "chain": ["api-signing-key-legacy", "api-signing-key-v2", "api-signing-key"]},
        signing_actual,
        signing_actual == {
            "resolved_ref": "api-signing-key",
            "version": 2,
            "chain": ["api-signing-key-legacy", "api-signing-key-v2", "api-signing-key"],
        },
    )

    # Every promised field is present for a resolved variable.
    fields = {
        "variable": signing["variable"],
        "resolved_ref": signing["resolved_ref"],
        "version": signing["version"],
        "source_layer": signing["source_layer"],
    }
    check(
        records,
        "SAMPLE-OUTPUT-FIELDS",
        "each injected variable carries name, resolved ref, version and source layer",
        {"variable": "API_SIGNING_KEY", "resolved_ref": "api-signing-key",
         "version": 2, "source_layer": "service"},
        fields,
        fields == {"variable": "API_SIGNING_KEY", "resolved_ref": "api-signing-key",
                   "version": 2, "source_layer": "service"},
    )

    # Same-name report: LOG_SINK_KEY differs between api (v2) and worker (v1).
    collisions = {entry["variable"]: entry for entry in payload["same_name_collisions"]}
    log_entry = collisions.get("LOG_SINK_KEY", {})
    check(
        records,
        "SAMPLE-SAME-NAME",
        "LOG_SINK_KEY is flagged as same name from different sources",
        {"uniform": False, "distinct_targets": 2},
        {"uniform": log_entry.get("uniform"),
         "distinct_targets": log_entry.get("distinct_targets")},
        "LOG_SINK_KEY" in collisions
        and collisions["LOG_SINK_KEY"]["uniform"] is False
        and collisions["LOG_SINK_KEY"]["distinct_targets"] == 2,
    )

    # DB_PASSWORD is one ref name for every service, but worker's service-layer
    # pin selects version 4 while api/billing use version 3.
    db_entry = collisions.get("DB_PASSWORD", {})
    db_versions = {item["service"]: item["version"] for item in db_entry.get("services", [])}
    check(
        records,
        "SAMPLE-SAME-NAME-DIFF-VERSION",
        "DB_PASSWORD shares one ref but worker is pinned to a different version",
        {"uniform": False, "versions": {"api": 3, "billing": 3, "worker": 4}},
        {"uniform": db_entry.get("uniform"), "versions": db_versions},
        "DB_PASSWORD" in collisions
        and collisions["DB_PASSWORD"]["uniform"] is False
        and db_versions == {"api": 3, "billing": 3, "worker": 4},
    )

    records.extend(run_boundary_cases())
    return records


def run_boundary_cases() -> list[dict]:
    """Boundary behaviour on synthetic inputs, each kept as a real assertion."""

    from secretrefs import build_plan
    from secretrefs.loader import load_inputs

    records: list[dict] = []

    def plan(environment: dict, services: dict, catalog: dict) -> dict:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = tmp / "environment.json"
            svc = tmp / "services.json"
            cat = tmp / "catalog.json"
            env.write_text(json.dumps(environment), encoding="utf-8")
            svc.write_text(json.dumps(services), encoding="utf-8")
            cat.write_text(json.dumps(catalog), encoding="utf-8")
            return build_plan(load_inputs(env, svc, cat))

    base_catalog = {
        "environment": "prod",
        "services": ["svc"],
        "references": [
            {"service": "svc", "ref": "base", "versions": [1, 2, 3], "current": 2},
            {"service": "svc", "ref": "hop1", "alias_of": {"ref": "base"}},
            {"service": "svc", "ref": "hop2", "alias_of": {"ref": "hop1"}},
        ],
    }
    chain_plan = plan(
        {"environment": "prod",
         "common": [{"variable": "V", "ref": "hop2"}]},
        {"services": ["svc"]},
        base_catalog,
    )
    resolved = _variable(_service(chain_plan, "svc"), "V")
    chain_actual = {
        "resolved_ref": resolved["resolved_ref"],
        "version": resolved["version"],
        "chain": [step["ref"] for step in resolved["resolution_chain"]],
    }
    check(
        records,
        "EDGE-ALIAS-CHAIN",
        "two-hop alias resolves to base at catalog current version",
        {"resolved_ref": "base", "version": 2, "chain": ["hop2", "hop1", "base"]},
        chain_actual,
        chain_actual == {"resolved_ref": "base", "version": 2,
                         "chain": ["hop2", "hop1", "base"]},
    )

    cycle_catalog = {
        "environment": "prod",
        "services": ["svc"],
        "references": [
            {"service": "svc", "ref": "a", "alias_of": {"ref": "b"}},
            {"service": "svc", "ref": "b", "alias_of": {"ref": "c"}},
            {"service": "svc", "ref": "c", "alias_of": {"ref": "a"}},
        ],
    }
    cycle_plan = plan(
        {"environment": "prod", "common": [{"variable": "V", "ref": "a"}]},
        {"services": ["svc"]},
        cycle_catalog,
    )
    cycle_service = _service(cycle_plan, "svc")
    cycle_item = cycle_service["unresolved"][0]
    cycle_actual = {
        "reason": cycle_item["reason"],
        "chain": [step["ref"] for step in cycle_item["resolution_chain"]],
        "executable": cycle_service["executable"],
    }
    check(
        records,
        "EDGE-ALIAS-CYCLE",
        "alias cycle reports a closed chain spanning every node",
        {"reason": "alias_cycle", "chain": ["a", "b", "c", "a"], "executable": False},
        cycle_actual,
        cycle_actual == {"reason": "alias_cycle", "chain": ["a", "b", "c", "a"],
                         "executable": False},
    )

    cross_catalog = {
        "environment": "prod",
        "services": ["svc"],
        "references": [
            {"service": "svc", "ref": "here",
             "alias_of": {"environment": "staging", "ref": "there"}},
            {"environment": "staging", "service": "svc", "ref": "there",
             "versions": [1], "current": 1},
        ],
    }
    cross_plan = plan(
        {"environment": "prod", "common": [{"variable": "V", "ref": "here"}]},
        {"services": ["svc"]},
        cross_catalog,
    )
    cross_item = _service(cross_plan, "svc")["unresolved"][0]
    cross_actual = {
        "reason": cross_item["reason"],
        "chain": [f"{s['environment']}/{s['service']}/{s['ref']}"
                  for s in cross_item["resolution_chain"]],
    }
    check(
        records,
        "EDGE-CROSS-ENV",
        "alias leaving the deployment environment names both endpoints",
        {"reason": "cross_environment",
         "chain": ["prod/svc/here", "staging/svc/there"]},
        cross_actual,
        cross_actual == {"reason": "cross_environment",
                         "chain": ["prod/svc/here", "staging/svc/there"]},
    )

    dup_plan = plan(
        {"environment": "prod",
         "common": [
             {"variable": "DUP", "ref": "base", "version": 1},
             {"variable": "DUP", "ref": "base", "version": 2},
         ]},
        {"services": ["svc", "ok"]},
        {
            "environment": "prod",
            "services": ["svc", "ok"],
            "references": [
                {"service": "svc", "ref": "base", "versions": [1, 2], "current": 1},
                {"service": "ok", "ref": "base", "versions": [1, 2], "current": 1},
            ],
        },
    )
    dup_actual = {
        "svc_executable": _service(dup_plan, "svc")["executable"],
        "ok_executable": _service(dup_plan, "ok")["executable"],
        "conflict_kind": _service(dup_plan, "svc")["conflicts"][0]["kind"],
        "conflict_layer": _service(dup_plan, "svc")["conflicts"][0]["layer"],
    }
    check(
        records,
        "EDGE-LAYER-CONFLICT",
        "same-layer duplicate blocks every affected service",
        {"svc_executable": False, "ok_executable": False,
         "conflict_kind": "layer_conflict", "conflict_layer": "common"},
        dup_actual,
        dup_actual == {"svc_executable": False, "ok_executable": False,
                       "conflict_kind": "layer_conflict", "conflict_layer": "common"},
    )

    shadow_plan = plan(
        {"environment": "prod",
         "common": [
             {"variable": "DUP", "ref": "base", "version": 1},
             {"variable": "DUP", "ref": "base", "version": 2},
         ]},
        {"services": ["svc"],
         "service_overrides": {
             "svc": [{"variable": "DUP", "ref": "base", "version": 3}]
         }},
        {
            "environment": "prod",
            "services": ["svc"],
            "references": [
                {"service": "svc", "ref": "base", "versions": [1, 2, 3], "current": 1}
            ],
        },
    )
    shadow_service = _service(shadow_plan, "svc")
    shadow_actual = {
        "executable": shadow_service["executable"],
        "version": _variable(shadow_service, "DUP")["version"],
        "shadowed_conflicts": shadow_service["summary"]["shadowed_conflicts"],
    }
    check(
        records,
        "EDGE-SHADOWED-CONFLICT",
        "a shadowed lower-layer duplicate is reported but does not block",
        {"executable": True, "version": 3, "shadowed_conflicts": 1},
        shadow_actual,
        shadow_actual == {"executable": True, "version": 3, "shadowed_conflicts": 1},
    )

    independent_plan = plan(
        {"environment": "prod", "common": []},
        {"services": ["broken", "healthy"],
         "service_overrides": {
             "broken": [{"variable": "MISSING", "ref": "ghost"}],
             "healthy": [{"variable": "OK", "ref": "base", "version": 3}],
         }},
        {
            "environment": "prod",
            "services": ["broken", "healthy"],
            "references": [
                {"service": "healthy", "ref": "base", "versions": [1, 2, 3], "current": 1},
            ],
        },
    )
    healthy = _service(independent_plan, "healthy")
    independent_actual = {
        "broken": _service(independent_plan, "broken")["executable"],
        "healthy": healthy["executable"],
        "healthy_variables": [v["variable"] for v in healthy["variables"]],
        "healthy_OK_version": _variable(healthy, "OK")["version"],
    }
    check(
        records,
        "EDGE-SERVICE-INDEPENDENCE",
        "a blocked service does not erase another service's resolved variables",
        {"broken": False, "healthy": True, "healthy_variables": ["OK"],
         "healthy_OK_version": 3},
        independent_actual,
        independent_actual == {"broken": False, "healthy": True,
                               "healthy_variables": ["OK"], "healthy_OK_version": 3},
    )

    optional_plan = plan(
        {"environment": "prod",
         "common": [
             {"variable": "REQ", "ref": "base"},
             {"variable": "OPT", "ref": "ghost", "required": False},
         ]},
        {"services": ["svc"]},
        {
            "environment": "prod",
            "services": ["svc"],
            "references": [
                {"service": "svc", "ref": "base", "versions": [1], "current": 1}
            ],
        },
    )
    optional_service = _service(optional_plan, "svc")
    optional_actual = {
        "executable": optional_service["executable"],
        "unresolved": [(item["variable"], item["reason"], item["blocking"])
                       for item in optional_service["unresolved"]],
    }
    check(
        records,
        "EDGE-OPTIONAL-UNRESOLVED",
        "an unresolvable non-required binding does not block the service",
        {"executable": True, "unresolved": [["OPT", "not_registered", False]]},
        {"executable": optional_service["executable"],
         "unresolved": [list(self_) for self_ in optional_actual["unresolved"]]},
        optional_actual == {"executable": True,
                            "unresolved": [("OPT", "not_registered", False)]},
    )

    invalid_exit = _cli(
        "--environment", "/nonexistent/environment.json",
        "--services", "/nonexistent/services.json",
        "--catalog", "/nonexistent/catalog.json",
    )
    invalid_actual = {
        "returncode": invalid_exit.returncode,
        "error": json.loads(invalid_exit.stdout).get("error")
        if invalid_exit.stdout.strip() else None,
    }
    check(
        records,
        "EDGE-INPUT-ERROR",
        "unreadable input exits 2 with an invalid_input payload",
        {"returncode": 2, "error": "invalid_input"},
        invalid_actual,
        invalid_actual == {"returncode": 2, "error": "invalid_input"},
    )

    return records


def main() -> int:
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _deadline_handler)
        signal.alarm(TIME_BUDGET_SECONDS)

    print("=" * 72)
    print("PART 1: unittest discovery and execution")
    print("=" * 72)
    try:
        unit_evidence = run_unit_tests()
    except TimeoutError as exc:
        print(f"\nTIMEOUT: {exc}")
        return 3
    except Exception as exc:  # noqa: BLE001 - the wrapper must not mask errors
        print(f"\nDISCOVERY/RUN ERROR: {type(exc).__name__}: {exc}")
        return 3

    print("\nunittest evidence:")
    for key in ("discovered", "ran", "skipped", "executed_non_skipped",
                "failures", "errors", "elapsed_seconds", "passed"):
        print(f"  {key}: {unit_evidence.get(key)}")
    if unit_evidence.get("reason"):
        print(f"  reason: {unit_evidence['reason']}")

    print()
    print("=" * 72)
    print("PART 2: delivered sample and boundary assertions")
    print("=" * 72)
    try:
        records = run_sample_cases()
    except TimeoutError as exc:
        print(f"\nTIMEOUT: {exc}")
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"\nSAMPLE CHECK ERROR: {type(exc).__name__}: {exc}")
        return 3

    for record in records:
        status = "PASS" if record["passed"] else "FAIL"
        print(f"\n[{status}] {record['case_id']}")
        print(f"  description: {record['description']}")
        print(f"  expected:    {json.dumps(record['expected'], sort_keys=True)}")
        print(f"  actual:      {json.dumps(record['actual'], sort_keys=True)}")

    sample_failures = [record for record in records if not record["passed"]]
    elapsed = round(time.monotonic() - START, 3)
    print()
    print("=" * 72)
    print(
        f"unit evidence: passed={unit_evidence.get('passed')} "
        f"ran={unit_evidence.get('ran')} "
        f"non_skipped={unit_evidence.get('executed_non_skipped')}"
    )
    print(f"sample cases: total={len(records)} "
          f"passed={len(records) - len(sample_failures)} failed={len(sample_failures)}")
    print(f"elapsed: {elapsed}s (budget {TIME_BUDGET_SECONDS}s)")
    print("=" * 72)

    if hasattr(signal, "SIGALRM"):
        signal.alarm(0)

    if (
        not records
        or not unit_evidence.get("passed")
        or unit_evidence.get("executed_non_skipped", 0) < 1
        or sample_failures
    ):
        print("RESULT: FAILED")
        return 1
    print("RESULT: PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
