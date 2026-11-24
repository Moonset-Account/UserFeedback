"""Tick-by-tick execution and verification for generated cache plans."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .errors import PlanVerificationError
from .model import (
    ActiveWritebackCheckpoint,
    Checkpoint,
    ForegroundCheckpoint,
    ResidentCheckpoint,
    _UNSET,
    coerce_requests,
)
from .plan import (
    AdvanceEvent,
    BeginRequestEvent,
    EvictEvent,
    GenerationRecord,
    OccupancySample,
    Plan,
    PlanEvent,
    ResourceInterval,
    StartWritebackEvent,
    WritebackAction,
)


def sym_value(page: str, generation: int) -> str:
    return f"{page}#gen{generation}"


@dataclass
class _Resident:
    generation: int
    dirty: bool
    ready: bool
    value: Any


@dataclass
class _Active:
    job_id: int
    page: str
    generation: int
    remaining: int
    duration: int
    byte_count: int
    value: Any
    started_at: int


@dataclass
class _Foreground:
    index: int
    op: str
    page: str | None
    remaining: int
    total: int


@dataclass
class TraceSample:
    tick: int
    phase: str
    used_bytes: int
    capacity_bytes: int
    residents: tuple[str, ...]
    active_writeback: tuple[str, int, int] | None
    foreground_remaining: int | None


@dataclass
class VerificationReport:
    ok: bool
    makespan_ticks: int
    peak_used_bytes: int
    violations: tuple[str, ...]
    trace: tuple[TraceSample, ...]
    final_persistent_values: dict[str, Any]
    writebacks: tuple[WritebackAction, ...]


class TickInterpreter:
    """Executes plan events one cache tick at a time and checks invariants."""

    def __init__(self, planner: Any, requests: Sequence[Any], checkpoint: Checkpoint | None = None) -> None:
        self.planner = planner
        self.pages = planner.pages
        self.capacity = planner.capacity
        self.requests = coerce_requests(requests)
        if checkpoint is not None:
            self._init_from_checkpoint(checkpoint)
        else:
            self._init_from_planner()
        self.latest_values = self._current_latest_values()
        self.trace: list[TraceSample] = []
        self.violations: list[str] = []
        self.writeback_records: list[WritebackAction] = []
        self.generation_records: list[GenerationRecord] = []
        self.timeline: list[ResourceInterval] = []
        self.queued_actions: list[dict[str, Any]] = []
        self._wb_partial: dict[int, dict[str, Any]] = {}
        if checkpoint is not None and self.active is not None:
            a = self.active
            self._wb_partial[a.job_id] = {
                "page": a.page,
                "generation": a.generation,
                "byte_count": a.byte_count,
                "duration": a.duration,
                "value": a.value,
                "started_at": a.started_at,
            }
        self._channel_intervals: list[tuple[int, int]] = []
        self.historical_completed_bytes = checkpoint.bytes_already_written if checkpoint else 0
        self.next_job_id = checkpoint.next_job_id if checkpoint else 1
        self._record_generations_at_start(checkpoint)
        self._sample("initial")

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------
    def _init_from_planner(self) -> None:
        self.now = 0
        self.cursor = 0
        self.residents: dict[str, _Resident] = {}
        self.generation: dict[str, int] = {}
        self.stable_generation: dict[str, int] = {}
        self.pins: dict[str, int] = {}
        self.persistent: dict[str, Any] = {}
        self.active: _Active | None = None
        self.foreground: _Foreground | None = None

        for pid, page in self.pages.items():
            spec = self.planner.initial_cache.get(pid)
            gen = spec.generation if spec else 0
            stable = spec.stable_generation if spec else 0
            self.generation[pid] = gen
            self.stable_generation[pid] = stable  # type: ignore[assignment]
            if spec and spec.pins:
                self.pins[pid] = spec.pins
            initial_page_value = (
                page.initial_value if page.initial_value is not _UNSET else sym_value(pid, 0)
            )
            if spec and spec.persistent_value is not _UNSET:
                persistent_value = spec.persistent_value
            elif stable == 0:
                persistent_value = initial_page_value
            else:
                persistent_value = sym_value(pid, stable)
            self.persistent[pid] = persistent_value
            if pid in self.planner.initial_cache and spec is not None:
                if spec.value is not _UNSET:
                    cache_value = spec.value
                elif not spec.dirty and gen == stable:
                    cache_value = persistent_value
                else:
                    cache_value = sym_value(pid, gen)
                self.residents[pid] = _Resident(gen, spec.dirty, spec.ready, cache_value)

    def _init_from_checkpoint(self, cp: Checkpoint) -> None:
        self.now = cp.at_tick
        self.cursor = cp.cursor
        self.generation = dict(cp.generation)
        self.stable_generation = dict(cp.stable_generation)
        self.pins = {p: n for p, n in cp.pins.items() if n}
        self.persistent = dict(cp.persistent_values)
        for pid in self.pages:
            self.generation.setdefault(pid, 0)
            self.stable_generation.setdefault(pid, 0)
            self.persistent.setdefault(pid, sym_value(pid, self.stable_generation[pid]))
        self.residents = {
            pid: _Resident(rc.generation, rc.dirty, rc.ready, rc.value)
            for pid, rc in cp.residents.items()
        }
        self.active = None
        if cp.active_writeback:
            aw = cp.active_writeback
            self.active = _Active(
                aw.job_id,
                aw.page,
                aw.generation,
                aw.remaining_ticks,
                aw.duration_ticks,
                aw.byte_count,
                aw.value,
                aw.started_at_tick if aw.started_at_tick is not None else cp.at_tick,
            )
        self.foreground = None
        if cp.foreground:
            f = cp.foreground
            self.foreground = _Foreground(
                f.request_index, f.kind, f.page, f.remaining_ticks, f.total_ticks
            )

    def _record_generations_at_start(self, cp: Checkpoint | None) -> None:
        seen: set[tuple[str, int]] = set()
        for pid, gen in self.generation.items():
            for g in range(0, gen + 1):
                if (pid, g) in seen:
                    continue
                seen.add((pid, g))
                value = self.persistent.get(pid) if g == self.stable_generation[pid] else None
                if g == gen and pid in self.residents:
                    value = self.residents[pid].value
                if value is None:
                    value = sym_value(pid, g)
                self.generation_records.append(
                    GenerationRecord(pid, g, self.now if g == gen else 0, None, value)
                )

    # ------------------------------------------------------------------
    # State helpers and invariant checking
    # ------------------------------------------------------------------
    @property
    def used_bytes(self) -> int:
        return sum(self.pages[pid].size for pid in self.residents)

    def _current_latest_values(self) -> dict[str, Any]:
        latest: dict[str, Any] = {}
        for pid in self.pages:
            gen = self.generation.get(pid, 0)
            resident = self.residents.get(pid)
            if resident is not None and resident.generation == gen:
                latest[pid] = resident.value
            else:
                latest[pid] = self.persistent.get(pid, sym_value(pid, gen))
        return latest

    def _sample(self, phase: str) -> None:
        self.trace.append(
            TraceSample(
                tick=self.now,
                phase=phase,
                used_bytes=self.used_bytes,
                capacity_bytes=self.capacity,
                residents=tuple(sorted(self.residents)),
                active_writeback=None
                if self.active is None
                else (self.active.page, self.active.generation, self.active.remaining),
                foreground_remaining=None if self.foreground is None else self.foreground.remaining,
            )
        )

    def _fail(self, message: str) -> None:
        self.violations.append(f"tick {self.now}: {message}")

    def check_invariants(self, context: str = "") -> None:
        prefix = f"{context}: " if context else ""
        used = self.used_bytes
        if used < 0 or used > self.capacity:
            self._fail(f"{prefix}occupancy {used} outside [0,{self.capacity}]")
        for pid, count in self.pins.items():
            if count < 0:
                self._fail(f"{prefix}pin count for {pid} is negative")
        if self.active is not None:
            a = self.active
            if a.page not in self.residents:
                self._fail(f"{prefix}active writeback page {a.page} is not resident")
            else:
                r = self.residents[a.page]
                if a.generation > r.generation:
                    self._fail(f"{prefix}active writeback generation {a.generation} newer than resident {r.generation}")
                if a.remaining <= 0:
                    self._fail(f"{prefix}active writeback has non-positive remaining ticks")
        if self.foreground is not None:
            f = self.foreground
            if f.remaining <= 0:
                self._fail(f"{prefix}foreground job has non-positive remaining ticks")
            if f.op == "read_miss" and f.page is not None:
                r = self.residents.get(f.page)
                if r is None:
                    self._fail(f"{prefix}read-miss target {f.page} is absent")
                elif r.ready:
                    self._fail(f"{prefix}read-miss target {f.page} became ready before completion")
        for pid, r in self.residents.items():
            if not r.ready and (self.foreground is None or self.foreground.page != pid):
                self._fail(f"{prefix}resident {pid} is not ready without a foreground fill")

    # ------------------------------------------------------------------
    # Event execution
    # ------------------------------------------------------------------
    def execute_event(self, event: PlanEvent) -> None:
        if isinstance(event, BeginRequestEvent):
            self._begin(event)
        elif isinstance(event, EvictEvent):
            self._evict(event)
        elif isinstance(event, StartWritebackEvent):
            self._start_writeback(event)
        elif isinstance(event, AdvanceEvent):
            if event.from_tick != self.now:
                raise PlanVerificationError(
                    f"advance starts at tick {event.from_tick}, interpreter is at {self.now}"
                )
            for _ in range(event.ticks):
                self.advance_one_tick()
        else:  # pragma: no cover - exhaustive union
            raise PlanVerificationError(f"unknown event {event!r}")
        self.check_invariants(type(event).__name__)

    def _require_tick(self, event_tick: int, name: str) -> None:
        if event_tick != self.now:
            raise PlanVerificationError(f"{name} scheduled at tick {event_tick}, interpreter at {self.now}")

    def _begin(self, event: BeginRequestEvent) -> None:
        self._require_tick(event.tick, "begin event")
        if self.foreground is not None:
            raise PlanVerificationError("cannot begin a request while foreground is busy")
        if event.request_index != self.cursor:
            raise PlanVerificationError(
                f"expected request {self.cursor}, event names {event.request_index}"
            )
        req = self.requests[event.request_index]
        kind = req.__class__.__name__
        if kind == "Pin":
            self.pins[req.page] = self.pins.get(req.page, 0) + 1
            self.cursor += 1
            self.timeline.append(
                ResourceInterval("request", f"pin:{req.page}", event.tick, event.tick, "pin")
            )
            return
        if kind == "Unpin":
            if self.pins.get(req.page, 0) <= 0:
                raise PlanVerificationError(f"unpin of unpinned page {req.page}")
            self.pins[req.page] -= 1
            if self.pins[req.page] == 0:
                self.pins.pop(req.page)
            self.cursor += 1
            self.timeline.append(
                ResourceInterval("request", f"unpin:{req.page}", event.tick, event.tick, "unpin")
            )
            return

        pid = req.page
        if kind == "Read":
            if pid not in self.residents:
                size = self.pages[pid].size
                if self.used_bytes + size > self.capacity:
                    raise PlanVerificationError(
                        f"insufficient cache space to allocate {pid}: "
                        f"{self.used_bytes}+{size}>{self.capacity}"
                    )
                gen = self.stable_generation.get(pid, 0)
                value = self.persistent.get(pid, sym_value(pid, gen))
                self.residents[pid] = _Resident(gen, False, event.duration_ticks == 0, value)
            self.timeline.append(
                ResourceInterval(
                    "foreground",
                    f"req{event.request_index}:read:{pid}",
                    event.tick,
                    event.tick + event.duration_ticks,
                    "read miss" if event.op == "read_miss" else "read hit",
                )
            )
            if event.duration_ticks == 0:
                r = self.residents[pid]
                r.ready = True
                if event.op == "read_miss":
                    # A zero-latency miss loads the stable version.  A read hit
                    # must not clear an existing dirty bit.
                    r.dirty = False
                    r.generation = self.stable_generation.get(pid, r.generation)
                    r.value = self.persistent.get(pid, r.value)
                self.cursor += 1
            else:
                self.foreground = _Foreground(
                    event.request_index, event.op, pid, event.duration_ticks, event.duration_ticks
                )
            return

        # Write / write-allocate.
        size = self.pages[pid].size
        if pid not in self.residents:
            if self.used_bytes + size > self.capacity:
                raise PlanVerificationError(f"insufficient cache space to write-allocate {pid}")
        new_gen = self.generation.get(pid, self.stable_generation.get(pid, 0)) + 1
        self.generation[pid] = new_gen
        if getattr(req, "value_provided", False):
            value = req.value
        else:
            value = sym_value(pid, new_gen)
        self.residents[pid] = _Resident(new_gen, True, True, value)
        self.latest_values[pid] = value
        self.generation_records.append(
            GenerationRecord(pid, new_gen, event.tick, event.request_index, value)
        )
        self.timeline.append(
            ResourceInterval(
                "foreground",
                f"req{event.request_index}:write:{pid}:g{new_gen}",
                event.tick,
                event.tick + event.duration_ticks,
                "write",
            )
        )
        if event.duration_ticks == 0:
            self.cursor += 1
        else:
            self.foreground = _Foreground(
                event.request_index, "write", pid, event.duration_ticks, event.duration_ticks
            )

    def _evict(self, event: EvictEvent) -> None:
        self._require_tick(event.tick, "eviction")
        r = self.residents.get(event.page)
        if r is None:
            raise PlanVerificationError(f"cannot evict absent page {event.page}")
        if self.pins.get(event.page, 0) > 0:
            raise PlanVerificationError(f"cannot evict pinned page {event.page}")
        if r.dirty:
            raise PlanVerificationError(f"cannot evict dirty page {event.page}")
        if not r.ready:
            raise PlanVerificationError(f"cannot evict page {event.page} during fill")
        if self.active and self.active.page == event.page:
            raise PlanVerificationError(f"cannot evict {event.page} during writeback")
        if self.foreground and self.foreground.page == event.page:
            raise PlanVerificationError(f"cannot evict {event.page} while its foreground op is active")
        actual_size = self.pages[event.page].size
        if event.byte_count != actual_size:
            raise PlanVerificationError("eviction event has wrong byte count")
        del self.residents[event.page]
        self.queued_actions.append(
            {
                "tick": event.tick,
                "queue": "cache_invalidation",
                "action": "evict_clean",
                "page": event.page,
                "byte_count": event.byte_count,
            }
        )

    def _start_writeback(self, event: StartWritebackEvent) -> None:
        self._require_tick(event.tick, "writeback start")
        if self.active is not None:
            raise PlanVerificationError("writeback channel already active")
        r = self.residents.get(event.page)
        if r is None:
            raise PlanVerificationError(f"cannot write back absent page {event.page}")
        if not r.dirty:
            raise PlanVerificationError(f"page {event.page} is not dirty")
        if not r.ready:
            raise PlanVerificationError(f"page {event.page} is not ready")
        if event.generation != r.generation:
            raise PlanVerificationError(
                f"writeback starts gen {event.generation}, resident is gen {r.generation}"
            )
        if event.byte_count != self.pages[event.page].size:
            raise PlanVerificationError("writeback byte count must equal page size")
        if any(not (event.end_tick <= s or event.tick >= e) for s, e in self._channel_intervals):
            raise PlanVerificationError("writeback channel intervals overlap")
        self.active = _Active(
            event.job_id,
            event.page,
            event.generation,
            event.duration_ticks,
            event.duration_ticks,
            event.byte_count,
            event.value,
            event.tick,
        )
        self.next_job_id = max(self.next_job_id, event.job_id + 1)
        self._channel_intervals.append((event.tick, event.end_tick))
        self.timeline.append(
            ResourceInterval(
                "writeback_channel",
                f"wb{event.job_id}:{event.page}:g{event.generation}",
                event.tick,
                event.end_tick,
                f"{event.byte_count} bytes",
            )
        )
        self.queued_actions.append(
            {
                "tick": event.tick,
                "queue": "writeback_channel",
                "action": "write_generation",
                "job_id": event.job_id,
                "page": event.page,
                "generation": event.generation,
                "byte_count": event.byte_count,
                "queued_at_tick": event.tick,
                "started_at_tick": event.tick,
                "duration_ticks": event.duration_ticks,
                "ends_at_tick": event.end_tick,
            }
        )
        self._wb_partial[event.job_id] = {
            "page": event.page,
            "generation": event.generation,
            "byte_count": event.byte_count,
            "duration": event.duration_ticks,
            "value": event.value,
            "started_at": event.tick,
        }

    def advance_one_tick(self) -> None:
        """Advance both foreground and background resources by exactly one tick."""

        if self.foreground is None and self.active is None:
            raise PlanVerificationError("cannot advance a tick with no active resource")

        # Background completion.  Persist the snapshotted generation/value even
        # if a newer write has already created another generation.
        if self.active is not None:
            a = self.active
            a.remaining -= 1
            if a.remaining == 0:
                self.persistent[a.page] = a.value
                self.stable_generation[a.page] = a.generation
                r = self.residents[a.page]
                stale = r.generation != a.generation
                if not stale:
                    r.dirty = False
                clean_after = not r.dirty
                partial = self._wb_partial.pop(a.job_id)
                completed_at = self.now + 1
                self.writeback_records.append(
                    WritebackAction(
                        job_id=a.job_id,
                        page=a.page,
                        generation=a.generation,
                        byte_count=a.byte_count,
                        queued_at_tick=partial["started_at"],
                        started_at_tick=partial["started_at"],
                        completed_at_tick=completed_at,
                        duration_ticks=a.duration,
                        value=a.value,
                        stale_completion=stale,
                        clean_after_completion=clean_after,
                    )
                )
                self.active = None

        # Foreground completion.
        if self.foreground is not None:
            f = self.foreground
            f.remaining -= 1
            if f.remaining == 0:
                if f.op == "read_miss" and f.page is not None:
                    r = self.residents[f.page]
                    r.ready = True
                    r.dirty = False
                    r.generation = self.stable_generation.get(f.page, r.generation)
                    r.value = self.persistent.get(f.page, r.value)
                self.foreground = None
                self.cursor = f.index + 1

        self.now += 1
        self.check_invariants("tick completion")
        self._sample("cycle_end")

    # ------------------------------------------------------------------
    # Running and reports
    # ------------------------------------------------------------------
    def run_events(self, events: Sequence[PlanEvent]) -> None:
        for event in events:
            self.execute_event(event)
        self.check_invariants("final")
        if self.violations:
            raise PlanVerificationError("; ".join(self.violations))

    def run_all(self, plan: Plan) -> VerificationReport:
        self.run_events(plan.events)
        return self.report()

    def assert_goal(self, request_count: int | None = None) -> None:
        n = len(self.requests) if request_count is None else request_count
        problems = list(self.violations)
        if self.cursor != n:
            problems.append(f"cursor {self.cursor} != {n}")
        if self.foreground is not None:
            problems.append("foreground remains busy")
        if self.active is not None:
            problems.append("writeback remains active")
        dirty = [pid for pid, r in self.residents.items() if r.dirty]
        if dirty:
            problems.append(f"dirty residents remain: {dirty}")
        for pid, latest_gen in self.generation.items():
            if self.stable_generation.get(pid, 0) != latest_gen:
                problems.append(
                    f"page {pid} stable gen {self.stable_generation.get(pid)} != latest gen {latest_gen}"
                )
            expected_value = self.latest_values.get(pid, sym_value(pid, latest_gen))
            if pid in self.persistent and self.persistent[pid] != expected_value:
                problems.append(f"page {pid} final persistent value is {self.persistent[pid]!r}, expected {expected_value!r}")
        if problems:
            raise PlanVerificationError("; ".join(problems))

    def report(self) -> VerificationReport:
        peak = max((s.used_bytes for s in self.trace), default=self.used_bytes)
        return VerificationReport(
            ok=not self.violations,
            makespan_ticks=self.now,
            peak_used_bytes=peak,
            violations=tuple(self.violations),
            trace=tuple(self.trace),
            final_persistent_values=dict(self.persistent),
            writebacks=tuple(sorted(self.writeback_records, key=lambda w: w.job_id)),
        )

    def checkpoint(self) -> Checkpoint:
        return Checkpoint(
            at_tick=self.now,
            cursor=self.cursor,
            residents={
                pid: ResidentCheckpoint(r.generation, r.dirty, r.ready, r.value)
                for pid, r in self.residents.items()
            },
            generation=dict(self.generation),
            stable_generation=dict(self.stable_generation),
            persistent_values=dict(self.persistent),
            pins=dict(self.pins),
            active_writeback=None
            if self.active is None
            else ActiveWritebackCheckpoint(
                page=self.active.page,
                generation=self.active.generation,
                remaining_ticks=self.active.remaining,
                duration_ticks=self.active.duration,
                byte_count=self.active.byte_count,
                value=self.active.value,
                job_id=self.active.job_id,
                started_at_tick=self.active.started_at,
            ),
            foreground=None
            if self.foreground is None
            else ForegroundCheckpoint(
                request_index=self.foreground.index,
                kind=self.foreground.op,
                page=self.foreground.page,
                remaining_ticks=self.foreground.remaining,
                total_ticks=self.foreground.total,
            ),
            bytes_already_written=(
                self.historical_completed_bytes
                + sum(w.byte_count for w in self.writeback_records)
            ),
            next_job_id=self.next_job_id,
        )

    def build_plan(self, events: Sequence[PlanEvent], *, search_states: int) -> Plan:
        self.run_events(events)
        self.assert_goal()
        report = self.report()
        # Occupancy for each tick in which time advances, followed by the final
        # tick boundary.  Trace also contains pre-action/end-of-cycle detail.
        occupancy: list[OccupancySample] = []
        seen_during: set[int] = set()
        for sample in self.trace:
            if sample.phase == "initial":
                occupancy.append(
                    OccupancySample(
                        sample.tick, sample.used_bytes, sample.capacity_bytes, sample.residents
                    )
                )
            elif sample.phase == "cycle_end":
                occupancy.append(
                    OccupancySample(
                        sample.tick, sample.used_bytes, sample.capacity_bytes, sample.residents
                    )
                )
        # De-duplicate identical tick entries while preserving final boundary.
        dedup: list[OccupancySample] = []
        for sample in occupancy:
            if dedup and dedup[-1].tick == sample.tick:
                dedup[-1] = sample
            else:
                dedup.append(sample)
        queued = tuple(self.queued_actions)
        writebacks = tuple(sorted(self.writeback_records, key=lambda w: (w.started_at_tick, w.job_id)))
        total_writeback_bytes = self.historical_completed_bytes + sum(
            w.byte_count for w in writebacks
        )
        timeline = tuple(sorted(self.timeline, key=lambda x: (x.start_tick, x.resource, x.item_id)))
        return Plan(
            makespan_ticks=self.now,
            writeback_bytes=total_writeback_bytes,
            events=tuple(events),
            writebacks=writebacks,
            generations=tuple(self.generation_records),
            queued_actions=queued,
            timeline=timeline,
            occupancy=tuple(dedup),
            final_persistent_values=report.final_persistent_values,
            optimal=True,
            search_states=search_states,
        )

    # ------------------------------------------------------------------
    # Prefix helper for resume tests/tools
    # ------------------------------------------------------------------
    def run_prefix_until_tick(self, events: Sequence[PlanEvent], target_tick: int) -> Checkpoint:
        """Run planned events until ``target_tick``, stopping inside an advance."""

        if target_tick < self.now:
            raise PlanVerificationError("target tick is before interpreter tick")
        i = 0
        while i < len(events):
            event = events[i]
            if isinstance(event, AdvanceEvent):
                if event.from_tick >= target_tick:
                    break
                steps = min(event.ticks, target_tick - self.now)
                for _ in range(steps):
                    self.advance_one_tick()
                self.check_invariants("prefix")
                if self.now == target_tick:
                    return self.checkpoint()
                i += 1
            else:
                if event.tick >= target_tick:
                    break
                self.execute_event(event)
                i += 1
        while self.now < target_tick:
            if self.foreground is None and self.active is None:
                raise PlanVerificationError("prefix has no active resource to advance")
            self.advance_one_tick()
        return self.checkpoint()


def verify_plan(planner: Any, requests: Sequence[Any], plan: Plan) -> VerificationReport:
    interp = TickInterpreter(planner, requests)
    return interp.run_all(plan)
