"""Injected-clock demonstration scenarios.

Each scenario is self-checking: it records the observed value and the value the
requirement demands, then reports ``passed``. ``app.py demo`` exits non-zero if
any scenario fails, so the demo is evidence, not an illustration.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from .config import LayerConfig, QuotaConfig
from .engine import QuotaAdjudicator


def _check(checks: list[dict[str, Any]], name: str, expected: Any, actual: Any) -> None:
    checks.append({"name": name, "expected": expected, "actual": actual, "ok": expected == actual})


def _config(account_limit: int, account_window: float, source_limit: int, source_window: float, ttl: float = 60.0) -> QuotaConfig:
    return QuotaConfig(
        account=LayerConfig(limit=account_limit, window_seconds=account_window),
        source=LayerConfig(limit=source_limit, window_seconds=source_window),
        dedup_ttl_seconds=ttl,
    )


def scenario_window_boundary() -> dict[str, Any]:
    """A slot frees at exactly ``admitted_at + window``; one tick earlier is denied."""

    adjudicator = QuotaAdjudicator(_config(2, 10, 99, 10))
    checks: list[dict[str, Any]] = []

    first = adjudicator.adjudicate(tenant="t1", account="alice", source="203.0.113.7", request_id="r1", now=0.0)
    second = adjudicator.adjudicate(tenant="t1", account="alice", source="203.0.113.7", request_id="r2", now=5.0)
    just_before = adjudicator.adjudicate(tenant="t1", account="alice", source="203.0.113.7", request_id="r3", now=9.5)
    at_boundary = adjudicator.adjudicate(tenant="t1", account="alice", source="203.0.113.7", request_id="r4", now=10.0)

    _check(checks, "t=0 admitted", True, first.allowed)
    _check(checks, "t=5 admitted", True, second.allowed)
    _check(checks, "t=9.5 denied (window not yet released)", False, just_before.allowed)
    _check(checks, "t=9.5 blocked layer", ["account"], list(just_before.blocked_layers))
    _check(checks, "t=9.5 retry_at == first admission + window", 10.0, just_before.retry_at)
    _check(checks, "t=9.5 account used", 2, just_before.layer("account").used)
    _check(checks, "t=10.0 admitted (slot freed at the boundary)", True, at_boundary.allowed)
    _check(checks, "t=10.0 account used == 2 (denial charged nothing)", 2, at_boundary.layer("account").used)

    return {
        "case_id": "demo-window-boundary",
        "requirement": "sliding window boundary releases a slot at exactly admitted_at + window",
        "passed": all(c["ok"] for c in checks),
        "checks": checks,
    }


def scenario_retry_after_denial() -> dict[str, Any]:
    """A denial reports the earliest retry instant, and retrying there succeeds."""

    adjudicator = QuotaAdjudicator(_config(1, 30, 99, 30, ttl=120))
    checks: list[dict[str, Any]] = []

    first = adjudicator.adjudicate(tenant="t1", account="bob", source="198.51.100.4", request_id="r1", now=100.0)
    denied = adjudicator.adjudicate(tenant="t1", account="bob", source="198.51.100.4", request_id="r2", now=101.0)
    early_retry = adjudicator.adjudicate(tenant="t1", account="bob", source="198.51.100.4", request_id="r3", now=129.5)
    on_time_retry = adjudicator.adjudicate(tenant="t1", account="bob", source="198.51.100.4", request_id="r4", now=130.0)

    _check(checks, "first admitted", True, first.allowed)
    _check(checks, "second denied", False, denied.allowed)
    _check(checks, "denial names the account layer", ["account"], list(denied.blocked_layers))
    _check(checks, "denial reason", "account_rate_limited", denied.reason)
    _check(checks, "earliest retry instant", 130.0, denied.retry_at)
    _check(checks, "retry half a unit early is denied", False, early_retry.allowed)
    _check(checks, "retry exactly at retry_at is admitted", True, on_time_retry.allowed)

    return {
        "case_id": "demo-retry-advice",
        "requirement": "a denial reports the first instant a retry can succeed, and it does",
        "passed": all(c["ok"] for c in checks),
        "checks": checks,
    }


def scenario_two_layer_gate() -> dict[str, Any]:
    """Both layers must have room; a denial on one layer consumes nothing on either."""

    adjudicator = QuotaAdjudicator(_config(5, 60, 2, 60))
    checks: list[dict[str, Any]] = []

    a = adjudicator.adjudicate(tenant="t1", account="carol", source="192.0.2.10", request_id="r1", now=0.0)
    b = adjudicator.adjudicate(tenant="t1", account="dave", source="192.0.2.10", request_id="r2", now=0.0)
    c = adjudicator.adjudicate(tenant="t1", account="erin", source="192.0.2.10", request_id="r3", now=0.0)

    _check(checks, "first two admitted (account has room, source=2)", [True, True], [a.allowed, b.allowed])
    _check(checks, "third denied", False, c.allowed)
    _check(checks, "third blocked layer is source only", ["source"], list(c.blocked_layers))
    _check(checks, "third retry_at", 60.0, c.retry_at)
    _check(checks, "denied account was not charged", 0, c.layer("account").used)
    _check(checks, "denied source stays at limit", 2, c.layer("source").used)

    follow_up = adjudicator.adjudicate(tenant="t1", account="erin", source="192.0.2.11", request_id="r4", now=1.0)
    _check(checks, "untouched account charged once (denial charged none)", 1, follow_up.layer("account").used)

    return {
        "case_id": "demo-two-layer-gate",
        "requirement": "admission needs both layers; a blocked layer charges neither",
        "passed": all(c["ok"] for c in checks),
        "checks": checks,
    }


def scenario_dedup_replay() -> dict[str, Any]:
    """The same request id inside the TTL returns the first decision and charges nothing."""

    adjudicator = QuotaAdjudicator(_config(1, 60, 99, 60, ttl=10))
    checks: list[dict[str, Any]] = []

    first = adjudicator.adjudicate(tenant="t1", account="frank", source="198.51.100.9", request_id="fixed", now=0.0)
    replay = adjudicator.adjudicate(tenant="t1", account="frank", source="198.51.100.9", request_id="fixed", now=1.0)
    after_ttl = adjudicator.adjudicate(tenant="t1", account="frank", source="198.51.100.9", request_id="fixed", now=10.0)

    _check(checks, "first admitted", True, first.allowed)
    _check(checks, "replay is flagged", True, replay.deduplicated)
    _check(checks, "replay repeats the allow", True, replay.allowed)
    _check(checks, "replay keeps the first decision time", 0.0, replay.decided_at)
    _check(checks, "replay charged nothing", 1, replay.layer("account").used)
    _check(checks, "id reused after the TTL is a fresh decision", False, after_ttl.deduplicated)
    _check(checks, "fresh decision after TTL is denied (budget spent)", False, after_ttl.allowed)

    return {
        "case_id": "demo-dedup-replay",
        "requirement": "duplicate request ids reuse the first decision inside the TTL",
        "passed": all(c["ok"] for c in checks),
        "checks": checks,
    }


def scenario_last_unit_race() -> dict[str, Any]:
    """Two threads racing for the last unit: exactly one wins, the limit holds."""

    adjudicator = QuotaAdjudicator(_config(1, 60, 1, 60, ttl=60))
    start = threading.Barrier(2)
    results: list[Any] = []
    errors: list[BaseException] = []

    def attempt(request_id: str) -> None:
        try:
            start.wait(timeout=5)
            results.append(
                adjudicator.adjudicate(
                    tenant="t1", account="gloria", source="203.0.113.50", request_id=request_id, now=7.0
                )
            )
        except BaseException as exc:  # pragma: no cover - surfaced as a failed check
            errors.append(exc)

    threads = [threading.Thread(target=attempt, args=(f"race-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    checks: list[dict[str, Any]] = []
    allowed = [d for d in results if d.allowed]
    _check(checks, "no thread raised", 0, len(errors))
    _check(checks, "both threads produced a decision", 2, len(results))
    _check(checks, "exactly one admission", 1, len(allowed))
    _check(checks, "exactly one denial", 1, len(results) - len(allowed))

    return {
        "case_id": "demo-last-unit-race",
        "requirement": "concurrent callers cannot both spend the final unit of a layer",
        "passed": all(c["ok"] for c in checks),
        "checks": checks,
    }


def scenario_tenant_isolation() -> dict[str, Any]:
    """Counters are per tenant; one tenant's exhaustion does not leak."""

    adjudicator = QuotaAdjudicator(_config(1, 60, 1, 60, ttl=60))
    checks: list[dict[str, Any]] = []

    a1 = adjudicator.adjudicate(tenant="tenant-a", account="henry", source="192.0.2.1", request_id="r1", now=0.0)
    a2 = adjudicator.adjudicate(tenant="tenant-a", account="henry", source="192.0.2.1", request_id="r2", now=1.0)
    b1 = adjudicator.adjudicate(tenant="tenant-b", account="henry", source="192.0.2.1", request_id="r1", now=1.0)

    _check(checks, "tenant-a first admitted", True, a1.allowed)
    _check(checks, "tenant-a second denied", False, a2.allowed)
    _check(checks, "tenant-b unaffected by tenant-a", True, b1.allowed)

    return {
        "case_id": "demo-tenant-isolation",
        "requirement": "accounts and sources are counted separately per tenant",
        "passed": all(c["ok"] for c in checks),
        "checks": checks,
    }


SCENARIOS: list[tuple[str, Callable[[], dict[str, Any]]]] = [
    ("demo-window-boundary", scenario_window_boundary),
    ("demo-retry-advice", scenario_retry_after_denial),
    ("demo-two-layer-gate", scenario_two_layer_gate),
    ("demo-dedup-replay", scenario_dedup_replay),
    ("demo-last-unit-race", scenario_last_unit_race),
    ("demo-tenant-isolation", scenario_tenant_isolation),
]


def run_scenarios() -> dict[str, dict[str, Any]]:
    report: dict[str, dict[str, Any]] = {}
    for case_id, factory in SCENARIOS:
        try:
            body = factory()
        except Exception as exc:  # a crashing scenario is a failing scenario
            body = {
                "case_id": case_id,
                "passed": False,
                "checks": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        report[case_id] = body
    return report
