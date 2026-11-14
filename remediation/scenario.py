"""Scenario (JSON event stream) loading and deterministic execution."""

from __future__ import annotations

from datetime import datetime

from .clock import Clock
from .engine import Action, RemediationEngine


def parse_iso(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"timestamp must be a string, got {type(value).__name__}")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        from datetime import timezone

        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_window(value):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("exemption_window must be null or [start_iso, end_iso]")
    return parse_iso(value[0]), parse_iso(value[1])


def _build_action(raw: dict) -> Action:
    return Action(
        resource_id=raw["resource_id"],
        team=raw["team"],
        environment=raw["environment"],
        impact_points=raw["impact_points"],
        exemption_window=_parse_window(raw.get("exemption_window")),
    )


def build_demo_scenario() -> dict:
    """Built-in cross-day scenario exercising every rule branch."""
    return {
        "budgets": {"platform": 10, "data": 8},
        "start_time": "2026-11-14T09:00:00+00:00",
        "events": [
            {
                "type": "report",
                "anomaly_id": "A1",
                "team": "platform",
                "environment": "staging",
                "severity": "high",
                "estimated_impact_points": 6,
                "exemption_window": None,
            },
            {
                "type": "report",
                "anomaly_id": "A2",
                "team": "platform",
                "environment": "staging",
                "severity": "medium",
                "estimated_impact_points": 5,
                "exemption_window": None,
            },
            {
                "type": "report",
                "anomaly_id": "A3",
                "team": "platform",
                "environment": "production",
                "severity": "critical",
                "estimated_impact_points": 9,
                "exemption_window": None,
            },
            {
                "type": "report",
                "anomaly_id": "A4",
                "team": "data",
                "environment": "dev",
                "severity": "low",
                "estimated_impact_points": 3,
                "exemption_window": [
                    "2026-11-14T08:00:00+00:00",
                    "2026-11-14T12:00:00+00:00",
                ],
            },
            {
                "type": "report",
                "anomaly_id": "A5",
                "team": "data",
                "environment": "dev",
                "severity": "high",
                "estimated_impact_points": 0,
                "exemption_window": None,
                "actions": [
                    {
                        "resource_id": "r-5a",
                        "team": "data",
                        "environment": "dev",
                        "impact_points": 4,
                        "exemption_window": None,
                    },
                    {
                        "resource_id": "r-5b",
                        "team": "data",
                        "environment": "dev",
                        "impact_points": 4,
                        "exemption_window": None,
                    },
                ],
            },
            {"type": "report", "anomaly_id": "A1", "note": "duplicate report reuses A1"},
            {"type": "cancel", "anomaly_id": "A1"},
            {
                "type": "report",
                "anomaly_id": "A6",
                "team": "platform",
                "environment": "staging",
                "severity": "medium",
                "estimated_impact_points": 7,
                "exemption_window": None,
            },
            {"type": "advance", "hours": 25},
            {
                "type": "report",
                "anomaly_id": "A7",
                "team": "platform",
                "environment": "staging",
                "severity": "low",
                "estimated_impact_points": 9,
                "exemption_window": None,
            },
            {"type": "cancel", "anomaly_id": "A6"},
        ],
    }


def run_scenario(scenario: dict) -> dict:
    if not isinstance(scenario, dict):
        raise ValueError("scenario root must be a JSON object")
    budgets = scenario.get("budgets")
    if not isinstance(budgets, dict) or not budgets:
        raise ValueError("scenario.budgets must be a non-empty team -> points object")

    start = scenario.get("start_time")
    clock = Clock(parse_iso(start) if start else None)
    engine = RemediationEngine(clock, budgets)

    events = scenario.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("scenario.events must be a non-empty list")

    trace: list[dict] = []
    for index, raw in enumerate(events):
        etype = raw.get("type")
        if etype == "advance":
            if "at" in raw:
                clock.set_time(parse_iso(raw["at"]))
            else:
                clock.advance(**{k: v for k, v in raw.items() if k not in ("type", "note")})
            trace.append(
                {"index": index, "type": "advance", "clock": clock.now().isoformat(), "ok": True}
            )
            continue

        if etype == "report":
            anomaly_id = raw["anomaly_id"]
            if anomaly_id in {p.anomaly_id for p in engine.list_plans()}:
                plan = engine.report(
                    anomaly_id=anomaly_id,
                    team=raw.get("team", ""),
                    environment=raw.get("environment", ""),
                    severity=raw.get("severity", ""),
                    estimated_impact_points=raw.get("estimated_impact_points", 0),
                    exemption_window=_parse_window(raw.get("exemption_window")),
                )
            else:
                actions = (
                    [_build_action(a) for a in raw["actions"]] if raw.get("actions") else None
                )
                plan = engine.report(
                    anomaly_id=anomaly_id,
                    team=raw["team"],
                    environment=raw["environment"],
                    severity=raw["severity"],
                    estimated_impact_points=raw["estimated_impact_points"],
                    exemption_window=_parse_window(raw.get("exemption_window")),
                    actions=actions,
                )
            trace.append(
                {
                    "index": index,
                    "type": "report",
                    "anomaly_id": anomaly_id,
                    "clock": clock.now().isoformat(),
                    "plan": plan.to_dict(),
                }
            )
            continue

        if etype == "cancel":
            plan = engine.cancel(raw["anomaly_id"])
            trace.append(
                {
                    "index": index,
                    "type": "cancel",
                    "anomaly_id": raw["anomaly_id"],
                    "clock": clock.now().isoformat(),
                    "plan": plan.to_dict(),
                }
            )
            continue

        raise ValueError(f"event[{index}] has unknown type {etype!r}")

    return {
        "events": trace,
        "final_plans": [p.to_dict() for p in engine.list_plans()],
        "budget_snapshot": engine.budget_snapshot(),
    }
