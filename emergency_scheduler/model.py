"""Core data model for the emergency repair scheduler.

Time is modelled on a discrete integer grid. A stage started at ``start`` with
``duration`` d occupies the half-open interval ``[start, start + d)``. Capacity
and resource checks are performed on every unit interval (every "intermediate
time point") inside that window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Stage:
    """One non-preemptive repair stage of a component.

    Attributes:
        stage_id: unique identifier of the stage.
        component_id: component this stage belongs to.
        duration: integer length, strictly positive. The stage cannot be split
            once started.
        personnel: personnel slots occupied for the whole stage interval.
        spare_needs: spare part id -> number occupied for the whole interval.
        spare_releases: spare part id -> number returned to the shared pool
            when the stage starts (before this stage's own needs are consumed).
            This models stripping a still-usable part from a failed unit so a
            different repair can proceed.
        stop_components: ids of redundant, still-working components that must
            be taken offline for the whole stage interval. Their capacity is
            unavailable on ``[start, end)`` and returns at ``end``.
    """

    stage_id: str
    component_id: str
    duration: int
    personnel: int = 0
    spare_needs: dict[str, int] = field(default_factory=dict)
    spare_releases: dict[str, int] = field(default_factory=dict)
    stop_components: tuple[str, ...] = ()


@dataclass
class Component:
    """A repairable component.

    ``service_capacity`` maps service id to the capacity this component
    contributes while it is online. A component with stages is assumed failed
    at time 0: it contributes no capacity until its *last* stage completes.
    """

    component_id: str
    service_capacity: dict[str, float]
    stages: list[Stage] = field(default_factory=list)


@dataclass(frozen=True)
class Service:
    """A service demand placed on the component fleet.

    Attributes:
        service_id: unique identifier.
        demand: required capacity at every time unit.
        min_capacity: hard lower bound on available capacity at every
            intermediate time point, even while repairs run.
        weight: multiplier applied to the integrated unmet demand.
    """

    service_id: str
    demand: float
    min_capacity: float = 0.0
    weight: float = 1.0


@dataclass
class Problem:
    """A complete scheduling instance inside a finite horizon."""

    components: list[Component]
    services: list[Service]
    horizon: int
    personnel_capacity: int
    initial_spares: dict[str, int] = field(default_factory=dict)


@dataclass
class Segment:
    """One scheduled repair stage in the returned plan."""

    stage_id: str
    component_id: str
    start: int
    end: int
    personnel: int
    spare_needs: dict[str, int]
    spare_releases: dict[str, int]
    stopped_components: tuple[str, ...]
    #: Integrated unmet demand per service observed over ``[start, end)``.
    #: When segments overlap, the same time unit is reported on every active
    #: segment; ``ScheduleResult.total_unmet`` remains the authoritative
    #: whole-horizon figure (counted once per time unit).
    unmet_demand: dict[str, float] = field(default_factory=dict)


@dataclass
class Failure:
    """Description of the first constraint that cannot be maintained."""

    #: Time unit (left endpoint of the interval) where the constraint breaks.
    time: int
    #: One of ``min_capacity``, ``personnel``, ``spare``, ``horizon``,
    #: ``precedence``.
    kind: str
    detail: str
    #: True when obtained by exhaustive reachability analysis; False when
    #: produced by the constructive fallback diagnostic.
    exact: bool = True
    related: dict[str, object] = field(default_factory=dict)


@dataclass
class ScheduleResult:
    feasible: bool
    segments: list[Segment] = field(default_factory=list)
    #: Integrated unmet demand per service over the whole horizon.
    total_unmet: dict[str, float] = field(default_factory=dict)
    #: Primary objective: sum of weight * unmet * duration.
    weighted_objective: float = 0.0
    #: Time at which every repair is finished (``None`` if infeasible).
    makespan: Optional[int] = None
    #: ``(completion_time, component_id)`` pairs ordered by time then id.
    completion_order: list[tuple[int, str]] = field(default_factory=list)
    failure: Optional[Failure] = None
