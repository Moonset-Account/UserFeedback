"""Exact lexicographic planner for at most six pages.

The search is an event-level Dijkstra search over cache residency, generations,
pin counts, the single writeback channel, and foreground request progress.
Time is the first objective and total writeback bytes the tie-breaker.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Sequence

from .errors import InfeasiblePlanError, InvalidInputError, InvalidRequestError
from .model import (
    Checkpoint,
    InitialResident,
    Page,
    Request,
    Write,
    _UNSET,
    coerce_requests,
)
from .plan import (
    AdvanceEvent,
    BeginRequestEvent,
    EvictEvent,
    Plan,
    StartWritebackEvent,
)

_INF = math.inf


@dataclass(frozen=True)
class ActiveWritebackState:
    job_id: int
    page: str
    generation: int
    remaining: int
    duration: int
    byte_count: int


@dataclass(frozen=True)
class ForegroundState:
    index: int
    op: str
    page: str | None
    remaining: int
    total: int


@dataclass(frozen=True)
class PlanningState:
    """Immutable search state.  ``now`` is intentionally absent from identity."""

    now: int
    cursor: int
    residents: tuple[tuple[str, int, bool, bool], ...]
    generations: tuple[tuple[str, int], ...]
    stable: tuple[tuple[str, int], ...]
    pins: tuple[tuple[str, int], ...]
    active: ActiveWritebackState | None
    foreground: ForegroundState | None
    next_job_id: int

    def maps(self) -> tuple[dict[str, tuple[int, bool, bool]], dict[str, int], dict[str, int], dict[str, int]]:
        residents = {p: (g, dirty, ready) for p, g, dirty, ready in self.residents}
        generations = dict(self.generations)
        stable = dict(self.stable)
        pins = dict(self.pins)
        return residents, generations, stable, pins

    def key(self) -> tuple[Any, ...]:
        return (
            self.cursor,
            self.residents,
            self.generations,
            self.stable,
            self.pins,
            None
            if self.active is None
            else (
                self.active.page,
                self.active.generation,
                self.active.remaining,
                self.active.duration,
                self.active.byte_count,
            ),
            None
            if self.foreground is None
            else (
                self.foreground.index,
                self.foreground.op,
                self.foreground.page,
                self.foreground.remaining,
                self.foreground.total,
            ),
        )


@dataclass
class _Node:
    state: PlanningState
    parent: int
    event: Any
    bytes_written: int


class CachePlanner:
    """Plan an optimal offline schedule for a small block cache.

    Parameters
    ----------
    pages:
        At most six :class:`~blockcache.model.Page` objects.
    capacity:
        Cache capacity in the same byte units as :attr:`Page.size`.
    read_miss_ticks:
        Fixed foreground occupancy/latency for a read miss.
    writeback_ticks:
        Fixed duration, page-id mapping, or ``Page -> ticks`` callable.
    bytes_per_tick:
        Optional channel bandwidth.  When supplied, duration is
        ``ceil(page.size / bytes_per_tick)``.
    """

    MAX_PAGES = 6

    def __init__(
        self,
        pages: Sequence[Page] | Mapping[str, Page],
        capacity: int,
        *,
        read_miss_ticks: int = 5,
        writeback_ticks: int | Mapping[str, int] | Callable[[Page], int] = 5,
        read_hit_ticks: int = 0,
        write_ticks: int = 0,
        bytes_per_tick: int | None = None,
        initial_cache: Mapping[str, InitialResident | Mapping[str, Any]] | None = None,
        max_search_states: int = 500_000,
    ) -> None:
        if isinstance(pages, Mapping):
            self.pages = dict(pages)
        else:
            page_list = list(pages)
            if len({p.id for p in page_list}) != len(page_list):
                raise InvalidInputError("page ids must be unique")
            self.pages = {p.id: p for p in page_list}
        if len(self.pages) > self.MAX_PAGES:
            raise InvalidInputError(f"this exact planner supports at most {self.MAX_PAGES} pages")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise InvalidInputError("capacity must be a positive integer")
        self.capacity = capacity
        self.read_miss_ticks = self._positive(read_miss_ticks, "read_miss_ticks")
        self.read_hit_ticks = self._nonneg(read_hit_ticks, "read_hit_ticks")
        self.write_ticks = self._nonneg(write_ticks, "write_ticks")
        self.writeback_ticks = writeback_ticks
        if bytes_per_tick is not None:
            self.bytes_per_tick = self._positive(bytes_per_tick, "bytes_per_tick")
        else:
            self.bytes_per_tick = None
        self.max_search_states = self._positive(max_search_states, "max_search_states")
        self.initial_cache = self._normalize_initial_cache(initial_cache or {})
        self._validate_initial_cache()

    @staticmethod
    def _positive(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise InvalidInputError(f"{name} must be a positive integer")
        return value

    @staticmethod
    def _nonneg(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidInputError(f"{name} must be a non-negative integer")
        return value

    def _normalize_initial_cache(
        self, raw: Mapping[str, InitialResident | Mapping[str, Any]]
    ) -> dict[str, InitialResident]:
        out: dict[str, InitialResident] = {}
        for pid, spec in raw.items():
            if pid not in self.pages:
                raise InvalidInputError(f"initial cache references unknown page {pid!r}")
            if isinstance(spec, InitialResident):
                out[pid] = spec
            elif isinstance(spec, Mapping):
                out[pid] = InitialResident(**spec)
            else:
                raise InvalidInputError(f"invalid initial cache spec for {pid!r}")
        return out

    def _validate_initial_cache(self) -> None:
        used = 0
        for pid, spec in self.initial_cache.items():
            page = self.pages[pid]
            if spec.pins < 0:
                raise InvalidInputError(f"negative pin count for {pid!r}")
            if not spec.ready and spec.pins == 0:
                # A resumed foreground fill is represented explicitly; ordinary
                # initial_cache entries should be usable.
                pass
            used += page.size
            if page.size > self.capacity:
                raise InvalidInputError(f"page {pid!r} is larger than cache capacity")
        if used > self.capacity:
            raise InvalidInputError("initial cache occupancy exceeds capacity")

    def writeback_duration(self, page_id: str) -> int:
        page = self.pages[page_id]
        if self.bytes_per_tick is not None:
            return max(1, math.ceil(page.size / self.bytes_per_tick))
        if page.writeback_ticks is not None:
            return page.writeback_ticks
        wb = self.writeback_ticks
        if isinstance(wb, int):
            return self._positive(wb, "writeback_ticks")
        if isinstance(wb, Mapping):
            if page_id not in wb:
                raise InvalidInputError(f"no writeback duration configured for page {page_id!r}")
            return self._positive(wb[page_id], f"writeback_ticks[{page_id!r}]")
        if callable(wb):
            value = wb(page)
            return self._positive(value, f"writeback_ticks({page_id!r})")
        raise InvalidInputError("writeback_ticks must be an int, mapping, or callable")

    # ------------------------------------------------------------------
    # Initial state and version values
    # ------------------------------------------------------------------
    def initial_state(self, checkpoint: Checkpoint | None = None) -> tuple[PlanningState, dict[tuple[str, int], Any], int]:
        values: dict[tuple[str, int], Any] = {}
        if checkpoint is not None:
            return self._checkpoint_state(checkpoint, values)

        generations: dict[str, int] = {}
        stable: dict[str, int] = {}
        pins: dict[str, int] = {}
        residents: list[tuple[str, int, bool, bool]] = []
        for pid, page in self.pages.items():
            spec = self.initial_cache.get(pid, InitialResident())
            generations[pid] = spec.generation
            stable[pid] = spec.stable_generation  # type: ignore[assignment]
            pins[pid] = spec.pins
            if pid in self.initial_cache:
                residents.append((pid, spec.generation, spec.dirty, spec.ready))
            if spec.value is not _UNSET:
                values[(pid, spec.generation)] = spec.value
            elif pid in self.initial_cache:
                values[(pid, spec.generation)] = f"{pid}#gen{spec.generation}"
            else:
                values[(pid, 0)] = (
                    page.initial_value
                    if page.initial_value is not _UNSET
                    else f"{pid}#gen0"
                )
            if spec.persistent_value is not _UNSET:
                values[(pid, stable[pid])] = spec.persistent_value
            elif stable[pid] == 0:
                values.setdefault(
                    (pid, 0),
                    page.initial_value
                    if page.initial_value is not _UNSET
                    else f"{pid}#gen0",
                )
        state = PlanningState(
            now=0,
            cursor=0,
            residents=tuple(sorted(residents)),
            generations=tuple(sorted(generations.items())),
            stable=tuple(sorted(stable.items())),
            pins=tuple(sorted((p, n) for p, n in pins.items() if n)),
            active=None,
            foreground=None,
            next_job_id=1,
        )
        return state, values, 0

    def _checkpoint_state(
        self, cp: Checkpoint, values: dict[tuple[str, int], Any]
    ) -> tuple[PlanningState, dict[tuple[str, int], Any], int]:
        for pid in self.pages:
            if pid not in cp.generation or pid not in cp.stable_generation:
                raise InvalidInputError(f"checkpoint lacks generation data for {pid!r}")
            values[(pid, cp.stable_generation[pid])] = cp.persistent_values.get(pid)
        residents = []
        for pid, rc in cp.residents.items():
            if pid not in self.pages:
                raise InvalidInputError(f"checkpoint references unknown page {pid!r}")
            residents.append((pid, rc.generation, rc.dirty, rc.ready))
            values[(pid, rc.generation)] = rc.value
        active = None
        if cp.active_writeback is not None:
            aw = cp.active_writeback
            if aw.page not in cp.residents:
                raise InvalidInputError("active writeback page must remain resident")
            active = ActiveWritebackState(
                aw.job_id,
                aw.page,
                aw.generation,
                aw.remaining_ticks,
                aw.duration_ticks,
                aw.byte_count,
            )
            values[(aw.page, aw.generation)] = aw.value
        fg = None
        if cp.foreground is not None:
            f = cp.foreground
            fg = ForegroundState(f.request_index, f.kind, f.page, f.remaining_ticks, f.total_ticks)
        state = PlanningState(
            now=cp.at_tick,
            cursor=cp.cursor,
            residents=tuple(sorted(residents)),
            generations=tuple(sorted(cp.generation.items())),
            stable=tuple(sorted(cp.stable_generation.items())),
            pins=tuple(sorted((p, n) for p, n in cp.pins.items() if n)),
            active=active,
            foreground=fg,
            next_job_id=cp.next_job_id,
        )
        self._assert_state_capacity(state)
        # ``bytes_already_written`` counts completed writebacks.  An active
        # job's whole byte count is already committed to the channel and is
        # unavoidable in the total lexicographic objective.
        committed_bytes = cp.bytes_already_written
        if cp.active_writeback is not None:
            committed_bytes += cp.active_writeback.byte_count
        return state, values, committed_bytes

    def _assert_state_capacity(self, state: PlanningState) -> None:
        used = sum(self.pages[p].size for p, _, _, _ in state.residents)
        if used > self.capacity:
            raise InfeasiblePlanError(f"state uses {used} bytes > capacity {self.capacity}")

    # ------------------------------------------------------------------
    # Public planning API
    # ------------------------------------------------------------------
    def plan(
        self,
        requests: Sequence[Request | tuple[Any, ...] | Mapping[str, Any]],
        checkpoint: Checkpoint | None = None,
    ) -> Plan:
        reqs = coerce_requests(requests)
        self._validate_requests(reqs, checkpoint)
        root, version_values, initial_bytes = self.initial_state(checkpoint)
        # Request-provided version values override symbolic defaults.
        gen_by_page = dict(root.generations)
        for i, req in enumerate(reqs):
            if isinstance(req, Write):
                # Only future requests should advance versions; the root may be
                # a checkpoint after some writes, so simulate the known counter
                # from root while collecting values.
                if checkpoint is None:
                    gen_by_page[req.page] = gen_by_page.get(req.page, 0) + 1
                    g = gen_by_page[req.page]
                    if req.value_provided:
                        version_values[(req.page, g)] = req.value
                    else:
                        version_values[(req.page, g)] = f"{req.page}#gen{g}"
                else:
                    # For resume, root.generation already counts writes before
                    # cp.cursor. A foreground write at cp.cursor has also already
                    # created its generation; only unstarted indices advance.
                    first_unstarted = root.cursor + (
                        1 if checkpoint.foreground is not None else 0
                    )
                    if i >= first_unstarted:
                        gen_by_page[req.page] = gen_by_page.get(req.page, 0) + 1
                        g = gen_by_page[req.page]
                        if req.value_provided:
                            version_values[(req.page, g)] = req.value
                        else:
                            version_values.setdefault((req.page, g), f"{req.page}#gen{g}")

        node_id, nodes, settled_count = self._search(root, reqs, version_values, initial_bytes)
        events = self._reconstruct(node_id, nodes)
        # Verification also constructs timeline/occupancy/result records.
        from .interpreter import TickInterpreter

        return TickInterpreter(self, reqs, checkpoint=checkpoint).build_plan(
            events, search_states=settled_count
        )

    def resume(
        self,
        checkpoint: Checkpoint,
        requests: Sequence[Request | tuple[Any, ...] | Mapping[str, Any]],
    ) -> Plan:
        """Plan the suffix from :class:`Checkpoint` and verify it tick-by-tick."""

        return self.plan(requests, checkpoint=checkpoint)

    def _validate_requests(self, reqs: tuple[Request, ...], cp: Checkpoint | None) -> None:
        start = cp.cursor if cp is not None else 0
        if cp is not None and (start < 0 or start > len(reqs)):
            raise InvalidInputError("checkpoint cursor is outside requests")
        if cp is not None and cp.foreground is not None:
            if cp.foreground.request_index != cp.cursor:
                raise InvalidInputError("foreground checkpoint index must equal cursor")
            if start >= len(reqs):
                raise InvalidInputError("checkpoint has foreground work but cursor is at end")
        balances = {pid: spec.pins for pid, spec in self.initial_cache.items()}
        if cp is not None:
            balances = dict(cp.pins)
        for i, req in enumerate(reqs):
            pid = getattr(req, "page", None)
            if pid is not None and pid not in self.pages:
                raise InvalidInputError(f"request {i} references unknown page {pid!r}")
            if i < start:
                # Full-sequence validation for a normal call; for resume the
                # checkpoint already embodies the prefix.  Still validate pin
                # balance over suffix starting from checkpoint counts.
                continue
            if req.__class__.__name__ == "Unpin":
                if balances.get(req.page, 0) <= 0:
                    raise InvalidRequestError(f"unpin at request {i} would make pin count negative")
                balances[req.page] -= 1
            elif req.__class__.__name__ == "Pin":
                balances[req.page] = balances.get(req.page, 0) + 1
        for req in reqs[start:]:
            if req.__class__.__name__ in {"Read", "Write"}:
                if self.pages[req.page].size > self.capacity:
                    raise InfeasiblePlanError(f"page {req.page!r} is larger than cache capacity")

    # ------------------------------------------------------------------
    # Dijkstra search
    # ------------------------------------------------------------------
    def _search(
        self,
        root: PlanningState,
        reqs: tuple[Request, ...],
        values: dict[tuple[str, int], Any],
        initial_bytes: int,
    ) -> tuple[int, list[_Node], int]:
        nodes: list[_Node] = [_Node(root, -1, None, initial_bytes)]
        heap: list[tuple[int, int, int, int]] = [(root.now, initial_bytes, 0, 0)]
        best: dict[tuple[Any, ...], tuple[int, int]] = {}
        settled: set[tuple[Any, ...]] = set()
        expansions = 0

        while heap:
            now, bytes_so_far, _, nid = heapq.heappop(heap)
            node = nodes[nid]
            state = node.state
            key = state.key()
            if key in settled:
                continue
            settled.add(key)
            expansions += 1
            if expansions > self.max_search_states:
                raise InfeasiblePlanError(
                    f"search exceeded max_search_states={self.max_search_states}; "
                    "reduce workload or raise the bound"
                )
            if self._is_goal(state, len(reqs)):
                return nid, nodes, expansions

            for event, child, added_bytes in self._successors(state, reqs, values):
                ckey = child.key()
                cost = (child.now, bytes_so_far + added_bytes)
                if ckey in settled:
                    continue
                old = best.get(ckey)
                if old is not None and old <= cost:
                    continue
                best[ckey] = cost
                child_id = len(nodes)
                nodes.append(_Node(child, nid, event, cost[1]))
                heapq.heappush(heap, (cost[0], cost[1], child_id, child_id))

        raise InfeasiblePlanError("no legal schedule exists for the supplied workload and checkpoint")

    @staticmethod
    def _is_goal(state: PlanningState, request_count: int) -> bool:
        return (
            state.cursor == request_count
            and state.foreground is None
            and state.active is None
            and not any(dirty for _, _, dirty, _ in state.residents)
        )

    def _reconstruct(self, goal_id: int, nodes: list[_Node]) -> tuple[Any, ...]:
        events: list[Any] = []
        nid = goal_id
        while nid > 0:
            node = nodes[nid]
            if node.event is None:
                raise PlanConstructionError("non-root node lacks event")
            events.append(node.event)
            nid = node.parent
        events.reverse()
        return tuple(events)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    def _successors(
        self,
        state: PlanningState,
        reqs: tuple[Request, ...],
        values: dict[tuple[str, int], Any],
    ):
        yield from self._begin_successor(state, reqs)
        yield from self._evict_successors(state)
        yield from self._writeback_successors(state, values)
        advanced = self._advance_successor(state)
        if advanced is not None:
            yield advanced

    def _used_bytes(self, residents: Sequence[tuple[str, int, bool, bool]]) -> int:
        return sum(self.pages[pid].size for pid, _, _, _ in residents)

    def _begin_successor(self, state: PlanningState, reqs: tuple[Request, ...]):
        if state.foreground is not None or state.cursor >= len(reqs):
            return
        req = reqs[state.cursor]
        kind = req.__class__.__name__
        residents_d, gens, stable, pins = state.maps()
        residents = set(state.residents)

        if kind == "Pin":
            pins[req.page] = pins.get(req.page, 0) + 1
            child = replace(
                state,
                cursor=state.cursor + 1,
                pins=tuple(sorted(pins.items())),
            )
            event = BeginRequestEvent(state.now, state.cursor, "pin", req.page, 0)
            yield event, child, 0
            return
        if kind == "Unpin":
            if pins.get(req.page, 0) <= 0:
                return
            pins[req.page] -= 1
            if pins[req.page] == 0:
                pins.pop(req.page)
            child = replace(
                state,
                cursor=state.cursor + 1,
                pins=tuple(sorted(pins.items())),
            )
            event = BeginRequestEvent(state.now, state.cursor, "unpin", req.page, 0)
            yield event, child, 0
            return

        pid = req.page
        size = self.pages[pid].size
        op = "read" if kind == "Read" else "write"

        if kind == "Read":
            if pid in residents_d:
                duration = req.hit_ticks if req.hit_ticks is not None else self.read_hit_ticks
                duration = self._nonneg(duration, "read hit duration")
                event = BeginRequestEvent(state.now, state.cursor, "read_hit", pid, duration)
                if duration == 0:
                    child = replace(state, cursor=state.cursor + 1)
                else:
                    fg = ForegroundState(state.cursor, "read_hit", pid, duration, duration)
                    child = replace(state, foreground=fg)
                yield event, child, 0
                return

            used = self._used_bytes(state.residents)
            if used + size > self.capacity:
                return
            duration = req.miss_ticks if req.miss_ticks is not None else self.read_miss_ticks
            duration = self._nonneg(duration, "read miss duration")
            gen = stable.get(pid, 0)
            residents.add((pid, gen, False, duration == 0))
            event = BeginRequestEvent(state.now, state.cursor, "read_miss", pid, duration)
            if duration == 0:
                child = replace(
                    state,
                    cursor=state.cursor + 1,
                    residents=tuple(sorted(residents)),
                    stable=tuple(sorted(stable.items())),
                )
            else:
                fg = ForegroundState(state.cursor, "read_miss", pid, duration, duration)
                child = replace(
                    state,
                    residents=tuple(sorted(residents)),
                    foreground=fg,
                    stable=tuple(sorted(stable.items())),
                )
            yield event, child, 0
            return

        # Write request: write-allocate a missing page.
        if pid not in residents_d:
            used = self._used_bytes(state.residents)
            if used + size > self.capacity:
                return
        new_gen = gens.get(pid, stable.get(pid, 0)) + 1
        gens[pid] = new_gen
        # Replace any old resident tuple for this page.
        residents = {r for r in residents if r[0] != pid}
        residents.add((pid, new_gen, True, True))
        duration = req.ticks if req.ticks is not None else self.write_ticks
        duration = self._nonneg(duration, "write duration")
        event = BeginRequestEvent(state.now, state.cursor, "write", pid, duration)
        base = dict(
            residents=tuple(sorted(residents)),
            generations=tuple(sorted(gens.items())),
        )
        if duration == 0:
            child = replace(state, cursor=state.cursor + 1, foreground=None, **base)
        else:
            fg = ForegroundState(state.cursor, "write", pid, duration, duration)
            child = replace(state, foreground=fg, cursor=state.cursor, **base)
        yield event, child, 0

    def _evict_successors(self, state: PlanningState):
        fg_pid = state.foreground.page if state.foreground else None
        active_pid = state.active.page if state.active else None
        for pid, gen, dirty, ready in state.residents:
            if pid == fg_pid or pid == active_pid:
                continue
            if dirty or not ready:
                continue
            if dict(state.pins).get(pid, 0) > 0:
                continue
            residents = tuple(r for r in state.residents if r[0] != pid)
            child = replace(state, residents=residents)
            event = EvictEvent(state.now, pid, self.pages[pid].size)
            yield event, child, 0

    def _writeback_successors(self, state: PlanningState, values: Mapping[tuple[str, int], Any]):
        if state.active is not None:
            return
        for pid, gen, dirty, ready in state.residents:
            if not dirty or not ready:
                continue
            duration = self.writeback_duration(pid)
            size = self.pages[pid].size
            job_id = state.next_job_id
            active = ActiveWritebackState(job_id, pid, gen, duration, duration, size)
            child = replace(state, active=active, next_job_id=job_id + 1)
            value = values.get((pid, gen), f"{pid}#gen{gen}")
            event = StartWritebackEvent(state.now, job_id, pid, gen, size, duration, value)
            yield event, child, size

    def _advance_successor(self, state: PlanningState):
        if state.foreground is None and state.active is None:
            return None
        dt_values = []
        if state.foreground is not None:
            dt_values.append(state.foreground.remaining)
        if state.active is not None:
            dt_values.append(state.active.remaining)
        dt = min(dt_values)
        if dt <= 0:
            raise InvalidInputError("planner state contains a zero-duration active operation")

        residents = list(state.residents)
        stable = dict(state.stable)
        active = state.active
        fg = state.foreground
        cursor = state.cursor

        if active is not None:
            remaining = active.remaining - dt
            if remaining == 0:
                stable[active.page] = active.generation
                for idx, (pid, gen, _dirty, ready) in enumerate(residents):
                    if pid == active.page:
                        clean = gen == active.generation
                        residents[idx] = (pid, gen, not clean, ready)
                        break
                active = None
            else:
                active = replace(active, remaining=remaining)

        if fg is not None:
            remaining = fg.remaining - dt
            if remaining == 0:
                if fg.op == "read_miss":
                    for idx, (pid, gen, _dirty, _ready) in enumerate(residents):
                        if pid == fg.page:
                            residents[idx] = (pid, gen, False, True)
                            break
                fg = None
                cursor = state.cursor + 1
            else:
                fg = replace(fg, remaining=remaining)

        child = replace(
            state,
            now=state.now + dt,
            cursor=cursor,
            residents=tuple(sorted(residents)),
            stable=tuple(sorted(stable.items())),
            active=active,
            foreground=fg,
        )
        return AdvanceEvent(state.now, dt), child, 0


class PlanConstructionError(RuntimeError):
    pass
