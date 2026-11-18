"""测试夹具工厂：用简短的关键字参数快速构建判定输入。"""

from __future__ import annotations

from datetime import timedelta

from release_gate.intervals import Interval, parse_dt
from release_gate.models import (
    ControlQuota,
    EvaluationBundle,
    Exemption,
    GateEvent,
    NewApplication,
)

WINDOW_START = "2026-11-01T00:00:00Z"
WINDOW_END = "2026-11-11T00:00:00Z"
DEFAULT_AS_OF = "2026-11-05T12:00:00Z"


def dt(value: str):
    return parse_dt(value)


def iv(start: str, end: str) -> Interval:
    return Interval(dt(start), dt(end))


def quota(env_id="prod", control_id="C-01", duration="PT4H") -> ControlQuota:
    from release_gate.intervals import parse_duration

    return ControlQuota(
        env_id=env_id,
        control_id=control_id,
        max_open_duration=parse_duration(duration),
    )


def exemption(
    exemption_id="EX-1",
    env_id="prod",
    control_id="C-01",
    start="2026-11-02T00:00:00Z",
    end="2026-11-09T00:00:00Z",
) -> Exemption:
    return Exemption(
        exemption_id=exemption_id,
        env_id=env_id,
        control_id=control_id,
        effective_start=dt(start),
        effective_end=dt(end),
    )


def event(
    event_id,
    exemption_id,
    action,
    occurred_at,
    env_id="prod",
    control_id="C-01",
) -> GateEvent:
    return GateEvent(
        event_id=event_id,
        exemption_id=exemption_id,
        env_id=env_id,
        control_id=control_id,
        action=action,
        occurred_at=dt(occurred_at),
    )


def application(
    application_id="APP-1",
    env_id="prod",
    control_id="C-01",
    intervals=(),
) -> NewApplication:
    return NewApplication(
        application_id=application_id,
        env_id=env_id,
        control_id=control_id,
        projected_intervals=tuple(intervals),
    )


def make_bundle(
    environments=("prod",),
    quotas=(),
    exemptions=(),
    events=(),
    applications=(),
    as_of=DEFAULT_AS_OF,
    window_start=WINDOW_START,
    window_end=WINDOW_END,
) -> EvaluationBundle:
    return EvaluationBundle(
        window=iv(window_start, window_end),
        environments=list(environments),
        quotas=list(quotas),
        exemptions=list(exemptions),
        events=list(events),
        applications=list(applications),
        as_of=dt(as_of) if as_of else None,
    )


def hours(value: float) -> timedelta:
    return timedelta(hours=value)
