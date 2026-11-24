"""Executable plan events and public result records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BeginRequestEvent:
    tick: int
    request_index: int
    op: str
    page: str
    duration_ticks: int


@dataclass(frozen=True)
class EvictEvent:
    tick: int
    page: str
    byte_count: int


@dataclass(frozen=True)
class StartWritebackEvent:
    tick: int
    job_id: int
    page: str
    generation: int
    byte_count: int
    duration_ticks: int
    value: Any = None

    @property
    def end_tick(self) -> int:
        return self.tick + self.duration_ticks


@dataclass(frozen=True)
class AdvanceEvent:
    from_tick: int
    ticks: int

    @property
    def to_tick(self) -> int:
        return self.from_tick + self.ticks


PlanEvent = BeginRequestEvent | EvictEvent | StartWritebackEvent | AdvanceEvent


@dataclass(frozen=True)
class WritebackAction:
    job_id: int
    page: str
    generation: int
    byte_count: int
    queued_at_tick: int
    started_at_tick: int
    completed_at_tick: int
    duration_ticks: int
    value: Any
    stale_completion: bool
    clean_after_completion: bool


@dataclass(frozen=True)
class GenerationRecord:
    page: str
    generation: int
    first_seen_tick: int
    request_index: int | None
    value: Any


@dataclass(frozen=True)
class ResourceInterval:
    resource: str
    item_id: str
    start_tick: int
    end_tick: int
    detail: str = ""


@dataclass(frozen=True)
class OccupancySample:
    tick: int
    used_bytes: int
    capacity_bytes: int
    residents: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    makespan_ticks: int
    writeback_bytes: int
    events: tuple[PlanEvent, ...]
    writebacks: tuple[WritebackAction, ...]
    generations: tuple[GenerationRecord, ...]
    queued_actions: tuple[dict[str, Any], ...]
    timeline: tuple[ResourceInterval, ...]
    occupancy: tuple[OccupancySample, ...]
    final_persistent_values: dict[str, Any]
    optimal: bool = True
    search_states: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "makespan_ticks": self.makespan_ticks,
            "writeback_bytes": self.writeback_bytes,
            "writeback_count": len(self.writebacks),
            "event_count": len(self.events),
            "optimal": self.optimal,
            "search_states": self.search_states,
            "final_persistent_values": dict(self.final_persistent_values),
        }
