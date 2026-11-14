"""Remediation planning engine with per-team per-day impact budget.

Business rules implemented here (see README for the full mapping):

* The module only emits *plans*; it never executes remediation.
* A production resource can only go to manual review (no budget is locked).
* Non-production actions may become ``pending_execution`` plans and lock
  impact points against their team's daily budget.
* All actions of one anomaly form an atomic group: if any action is exempt or
  the group does not fit the remaining budget, the whole group locks nothing.
* Cancelling a plan releases the quota still unused on the same day.
* Repeated reports of the same anomaly reuse the original plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .clock import Clock


class PlanStatus:
    PENDING_REVIEW = "pending_review"
    PENDING_EXECUTION = "pending_execution"
    EXEMPTED = "exempted"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"


class PlanReason:
    PRODUCTION_REVIEW_ONLY = "production_review_only"
    MANUAL_EXEMPTION_WINDOW = "manual_exemption_window"
    BUDGET_INSUFFICIENT = "budget_insufficient"
    LOCKED_ATOMIC_GROUP = "locked_atomic_group"
    NOT_LOCKED_REVIEW = "not_locked_review"
    NOT_LOCKED_EXEMPT = "not_locked_exempt"
    NOT_LOCKED_BUDGET = "not_locked_budget"
    CANCELLED_RELEASED = "cancelled_quota_released"
    CANCELLED_NOTHING_HELD = "cancelled_nothing_held"
    DUPLICATE_REUSED = "duplicate_report_reused_plan"
    ALREADY_CANCELLED = "already_cancelled"


PRODUCTION_ENV = "production"


def _validate_points(value, name: str) -> int:
    # bool is a subclass of int; reject it explicitly to avoid True == 1 bugs.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")
    return value


@dataclass
class Action:
    """One remediation item inside an anomaly's atomic group."""

    resource_id: str
    team: str
    environment: str
    impact_points: int
    exemption_window: Optional[tuple[datetime, datetime]] = None

    def __post_init__(self):
        if not isinstance(self.resource_id, str) or not self.resource_id.strip():
            raise ValueError("resource_id must be a non-empty string")
        if not isinstance(self.team, str) or not self.team.strip():
            raise ValueError("team must be a non-empty string")
        if not isinstance(self.environment, str) or not self.environment.strip():
            raise ValueError("environment must be a non-empty string")
        _validate_points(self.impact_points, "impact_points")
        if self.exemption_window is not None:
            start, end = self.exemption_window
            if not isinstance(start, datetime) or not isinstance(end, datetime):
                raise ValueError("exemption_window must contain datetime bounds")
            if end < start:
                raise ValueError("exemption_window end must not be before start")

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == PRODUCTION_ENV

    def is_exempt_at(self, moment: datetime) -> bool:
        """Inclusive window: at either bound the resource is still exempt."""
        if self.exemption_window is None:
            return False
        start, end = self.exemption_window
        return start <= moment <= end


@dataclass
class Plan:
    """The plan emitted for one reported anomaly (its whole atomic group)."""

    anomaly_id: str
    day: str
    status: str
    locked_points: int
    reason: str
    severity: str
    created_at: datetime
    actions: list[Action]
    team_locks: dict[str, int] = field(default_factory=dict)
    reused: bool = False
    cancelled_at: Optional[datetime] = None
    released_points: int = 0

    def to_dict(self) -> dict:
        return {
            "anomaly_id": self.anomaly_id,
            "day": self.day,
            "status": self.status,
            "locked_points": self.locked_points,
            "released_points": self.released_points,
            "reason": self.reason,
            "severity": self.severity,
            "reused": self.reused,
            "created_at": self.created_at.isoformat(),
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
            "team_locks": dict(self.team_locks),
            "actions": [
                {
                    "resource_id": a.resource_id,
                    "team": a.team,
                    "environment": a.environment,
                    "impact_points": a.impact_points,
                    "exempt_at_report_time": a.is_exempt_at(self.created_at),
                }
                for a in self.actions
            ],
        }


class _BudgetLedger:
    """Tracks locked impact points per (team, UTC day)."""

    def __init__(self, team_daily_budgets: dict[str, int]):
        self._budgets: dict[str, int] = {}
        for team, budget in team_daily_budgets.items():
            self._budgets[team] = _validate_points(budget, f"budget[{team}]")
        # (team, day) -> locked points
        self._locked: dict[tuple[str, str], int] = {}

    def check_team_known(self, team: str) -> None:
        if team not in self._budgets:
            raise ValueError(f"unknown team {team!r}; configure a daily budget first")

    def remaining(self, team: str, day: str) -> int:
        return self._budgets[team] - self._locked.get((team, day), 0)

    def fits(self, demand_by_team: dict[str, int], day: str) -> bool:
        return all(
            demand <= self.remaining(team, day) for team, demand in demand_by_team.items()
        )

    def lock(self, demand_by_team: dict[str, int], day: str) -> None:
        for team, demand in demand_by_team.items():
            key = (team, day)
            self._locked[key] = self._locked.get(key, 0) + demand

    def release(self, team_locks: dict[str, int], day: str) -> int:
        released = 0
        for team, points in team_locks.items():
            key = (team, day)
            current = self._locked.get(key, 0)
            if points > current:
                raise RuntimeError(
                    f"cannot release {points} points for {team} on {day}; only {current} locked"
                )
            self._locked[key] = current - points
            released += points
        return released

    def snapshot(self) -> dict[str, dict[str, int]]:
        """Return per-team per-day used/budget/remaining for reporting."""
        out: dict[str, dict[str, int]] = {}
        for team, budget in self._budgets.items():
            days = sorted(day for (t, day) in self._locked if t == team)
            out[team] = {
                "daily_budget": budget,
                "days": {
                    day: {
                        "locked": self._locked.get((team, day), 0),
                        "remaining": self.remaining(team, day),
                    }
                    for day in days
                },
            }
        return out


class RemediationEngine:
    def __init__(self, clock: Clock, team_daily_budgets: dict[str, int]):
        self.clock = clock
        self.ledger = _BudgetLedger(team_daily_budgets)
        self._plans: dict[str, Plan] = {}

    # ------------------------------------------------------------------ report
    def report(
        self,
        anomaly_id: str,
        team: str,
        environment: str,
        severity: str,
        estimated_impact_points: int,
        exemption_window: Optional[tuple[datetime, datetime]],
        actions: Optional[list[Action]] = None,
    ) -> Plan:
        """Report one anomaly and obtain a plan for its whole atomic group.

        A single-action group is built from the top-level fields when
        ``actions`` is omitted; otherwise the given actions are treated as one
        indivisible group (each carries its own team/environment/window).
        """
        if not isinstance(anomaly_id, str) or not anomaly_id.strip():
            raise ValueError("anomaly_id must be a non-empty string")
        if anomaly_id in self._plans:
            original = self._plans[anomaly_id]
            original.reused = True
            original.reason = PlanReason.DUPLICATE_REUSED
            return original

        if actions is None:
            actions = [
                Action(
                    resource_id=anomaly_id,
                    team=team,
                    environment=environment,
                    impact_points=estimated_impact_points,
                    exemption_window=exemption_window,
                )
            ]
        if not actions:
            raise ValueError("an anomaly must contain at least one action")
        for action in actions:
            if not isinstance(action, Action):
                raise ValueError("every group item must be an Action")
            self.ledger.check_team_known(action.team)

        moment = self.clock.now()
        day = self.clock.day_key()
        plan = self._evaluate(anomaly_id, severity, actions, moment, day)
        self._plans[anomaly_id] = plan
        return plan

    def _evaluate(
        self, anomaly_id: str, severity: str, actions: list[Action],
        moment: datetime, day: str,
    ) -> Plan:
        # Rule priority: production review -> manual exemption -> budget.
        if any(a.is_production for a in actions):
            return Plan(
                anomaly_id=anomaly_id,
                day=day,
                status=PlanStatus.PENDING_REVIEW,
                locked_points=0,
                reason=PlanReason.PRODUCTION_REVIEW_ONLY,
                severity=severity,
                created_at=moment,
                actions=actions,
            )

        if any(a.is_exempt_at(moment) for a in actions):
            return Plan(
                anomaly_id=anomaly_id,
                day=day,
                status=PlanStatus.EXEMPTED,
                locked_points=0,
                reason=PlanReason.MANUAL_EXEMPTION_WINDOW,
                severity=severity,
                created_at=moment,
                actions=actions,
            )

        demand_by_team: dict[str, int] = {}
        for action in actions:
            demand_by_team[action.team] = demand_by_team.get(action.team, 0) + action.impact_points

        if not self.ledger.fits(demand_by_team, day):
            return Plan(
                anomaly_id=anomaly_id,
                day=day,
                status=PlanStatus.BUDGET_EXCEEDED,
                locked_points=0,
                reason=PlanReason.BUDGET_INSUFFICIENT,
                severity=severity,
                created_at=moment,
                actions=actions,
                team_locks=dict(demand_by_team),
            )

        # All checks passed: lock the whole group atomically.
        self.ledger.lock(demand_by_team, day)
        return Plan(
            anomaly_id=anomaly_id,
            day=day,
            status=PlanStatus.PENDING_EXECUTION,
            locked_points=sum(demand_by_team.values()),
            reason=PlanReason.LOCKED_ATOMIC_GROUP,
            severity=severity,
            created_at=moment,
            actions=actions,
            team_locks=demand_by_team,
        )

    # ---------------------------------------------------------------- cancel
    def cancel(self, anomaly_id: str) -> Plan:
        plan = self._plans.get(anomaly_id)
        if plan is None:
            raise KeyError(f"no plan for anomaly {anomaly_id!r}")
        if plan.status == PlanStatus.CANCELLED:
            plan.reason = PlanReason.ALREADY_CANCELLED
            return plan

        moment = self.clock.now()
        plan.cancelled_at = moment
        plan.status = PlanStatus.CANCELLED

        if plan.locked_points <= 0:
            plan.reason = PlanReason.CANCELLED_NOTHING_HELD
            return plan

        if self.clock.day_key() == plan.day:
            # Same day: the quota was never consumed by execution, release it.
            released = self.ledger.release(plan.team_locks, plan.day)
            plan.released_points = released
            plan.locked_points = 0
            plan.reason = PlanReason.CANCELLED_RELEASED
        else:
            # Cross-day cancellation cannot restore yesterday's already-closed
            # daily budget; the lock simply expires with that day's ledger.
            plan.reason = PlanReason.CANCELLED_NOTHING_HELD
        return plan

    # ------------------------------------------------------------------ views
    def get_plan(self, anomaly_id: str) -> Plan:
        return self._plans[anomaly_id]

    def list_plans(self) -> list[Plan]:
        return list(self._plans.values())

    def budget_snapshot(self) -> dict:
        return self.ledger.snapshot()
