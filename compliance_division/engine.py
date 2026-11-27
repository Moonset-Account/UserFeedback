"""Incremental relational division for version-interval capability requirements.

The operator keeps a universe of device models, a mutable set of requirement
rows, and a multiset of capability reports.  A model is selected when **every**
requirement row is satisfied for it.  A requirement row is satisfied by a
model when at least one single report of that model has the same capability
and covers the whole requested version interval.

Reports are identified by a globally unique source id.  Two reports may carry
identical payloads (same model, capability and interval); they are distinct
reports with distinct source ids, but identical payloads never change coverage
by merely being duplicated, and removing one of them keeps coverage as long as
another copy remains.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple, Union

ENTER = "enter"
LEAVE = "leave"

_FACT_ADDED = "fact_added"
_FACT_REMOVED = "fact_removed"
_REQUIREMENT_ADDED = "requirement_added"
_REQUIREMENT_REMOVED = "requirement_removed"


@dataclass(frozen=True, slots=True)
class Requirement:
    """One required capability over a closed version interval."""

    requirement_id: str
    capability: str
    version_from: int
    version_to: int

    def __post_init__(self) -> None:
        if self.version_from > self.version_to:
            raise ValueError(
                f"requirement {self.requirement_id!r}: version_from must be "
                f"<= version_to, got [{self.version_from}, {self.version_to}]"
            )


@dataclass(frozen=True, slots=True)
class Fact:
    """One capability report with a unique source id."""

    source_id: str
    model: str
    capability: str
    version_from: int
    version_to: int

    def __post_init__(self) -> None:
        if self.version_from > self.version_to:
            raise ValueError(
                f"fact {self.source_id!r}: version_from must be <= version_to, "
                f"got [{self.version_from}, {self.version_to}]"
            )


@dataclass(frozen=True, slots=True)
class SelectionChange:
    """A model actually entering or leaving the current result set."""

    kind: str  # ENTER or LEAVE
    model: str
    cause: str = ""


@dataclass
class _ModelState:
    # capability -> (version_from, version_to) -> number of live reports
    fact_counts: Dict[str, Dict[Tuple[int, int], int]] = field(default_factory=dict)
    # requirement ids currently not satisfied by any single report
    unmet: Set[str] = field(default_factory=set)

    def covers(self, capability: str, version_from: int, version_to: int) -> bool:
        """True iff one report interval contains the requested interval."""
        groups = self.fact_counts.get(capability)
        if not groups:
            return False
        return any(
            start <= version_from and version_to <= end
            for start, end in groups
        )


class Subscription:
    """Handle returned by :meth:`DivisionEngine.subscribe`."""

    __slots__ = ("_engine", "_listener_id", "_closed")

    def __init__(self, engine: "DivisionEngine", listener_id: int) -> None:
        self._engine = engine
        self._listener_id = listener_id
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._engine._remove_listener(self._listener_id)
            self._closed = True

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class DivisionEngine:
    """Incremental relational-division operator with change subscriptions.

    Parameters
    ----------
    models:
        The device universe.  Duplicates are collapsed; insertion order is kept
        for deterministic notification order.
    requirements, facts:
        Optional initial rows.  They are applied before any listener can
        subscribe, so their effect on the initial selection is visible through
        :meth:`current_result` but is not replayed as change events.
    """

    def __init__(
        self,
        models: Iterable[str],
        requirements: Iterable[Requirement] = (),
        facts: Iterable[Fact] = (),
    ) -> None:
        unique_models: Dict[str, None] = {}
        for model in models:
            unique_models[model] = None
        self._models: Dict[str, _ModelState] = {
            model: _ModelState() for model in unique_models
        }
        self._requirements: Dict[str, Requirement] = {}
        self._requirements_by_capability: Dict[str, Dict[str, Requirement]] = {}
        self._facts: Dict[str, Fact] = {}

        self._listeners: Dict[int, Callable[[SelectionChange], None]] = {}
        self._next_listener_id = 0
        self._pending_changes: deque = deque()
        self._dispatching = False

        for requirement in requirements:
            self._apply_add_requirement(requirement, cause=_REQUIREMENT_ADDED, dispatch=False)
        for fact in facts:
            self._apply_add_fact(fact, cause=_FACT_ADDED, dispatch=False)

    # ------------------------------------------------------------------
    # Read-only views
    # ------------------------------------------------------------------
    @property
    def models(self) -> Tuple[str, ...]:
        return tuple(self._models)

    @property
    def requirement_count(self) -> int:
        return len(self._requirements)

    @property
    def fact_count(self) -> int:
        return len(self._facts)

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    def current_result(self) -> frozenset:
        """Models currently selected by the division."""
        return frozenset(
            model for model, state in self._models.items() if not state.unmet
        )

    def is_selected(self, model: str) -> bool:
        return not self._state(model).unmet

    def requirements_in_order(self) -> Tuple[Requirement, ...]:
        """Current requirement rows in insertion order."""
        return tuple(self._requirements.values())

    def uncovered_requirements(self, model: str) -> Tuple[Requirement, ...]:
        """Requirement rows not covered by a single same-model report."""
        state = self._state(model)
        return tuple(
            requirement
            for requirement_id, requirement in self._requirements.items()
            if requirement_id in state.unmet
        )

    def first_uncovered_requirement(self, model: str) -> Optional[Requirement]:
        """First still-uncovered requirement for ``model``, or ``None``.

        Requirements are considered in insertion order.  A selected model has
        no uncovered requirement and yields ``None``.
        """
        state = self._state(model)
        for requirement_id, requirement in self._requirements.items():
            if requirement_id in state.unmet:
                return requirement
        return None

    def facts(self) -> Tuple[Fact, ...]:
        """Live reports in insertion order."""
        return tuple(self._facts.values())

    def has_fact(self, source_id: str) -> bool:
        return source_id in self._facts

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def add_requirement(
        self,
        requirement: Union[Requirement, str],
        capability: Optional[str] = None,
        version_from: Optional[int] = None,
        version_to: Optional[int] = None,
    ) -> Requirement:
        """Add a requirement row.

        Accepts either a :class:`Requirement` or the four plain fields
        ``requirement_id, capability, version_from, version_to``.
        """
        row = self._coerce_requirement(
            requirement, capability, version_from, version_to
        )
        self._apply_add_requirement(row, cause=_REQUIREMENT_ADDED, dispatch=True)
        return row

    def remove_requirement(self, requirement_id: str) -> Requirement:
        requirement = self._requirements.get(requirement_id)
        if requirement is None:
            raise KeyError(f"unknown requirement id: {requirement_id!r}")
        self._apply_remove_requirement(requirement)
        return requirement

    def add_fact(
        self,
        fact: Union[Fact, str],
        model: Optional[str] = None,
        capability: Optional[str] = None,
        version_from: Optional[int] = None,
        version_to: Optional[int] = None,
    ) -> Fact:
        """Add a capability report with a globally unique source id.

        Accepts either a :class:`Fact` or the five plain fields
        ``source_id, model, capability, version_from, version_to``.
        """
        row = self._coerce_fact(fact, model, capability, version_from, version_to)
        self._apply_add_fact(row, cause=_FACT_ADDED, dispatch=True)
        return row

    def remove_fact(self, source_id: str) -> Fact:
        """Withdraw one report by source id.

        Withdrawing one of several identical reports leaves coverage intact.
        """
        fact = self._facts.get(source_id)
        if fact is None:
            raise KeyError(f"unknown fact source id: {source_id!r}")
        self._apply_remove_fact(fact)
        return fact

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------
    def subscribe(self, listener: Callable[[SelectionChange], None]) -> Subscription:
        """Register a listener receiving only actual result-set changes.

        The listener is called once per model entering or leaving the result
        set.  It is never called for a model that was already in the set (or
        already outside it), and identical duplicate reports cause no call.
        """
        if not callable(listener):
            raise TypeError("listener must be callable")
        listener_id = self._next_listener_id
        self._next_listener_id += 1
        self._listeners[listener_id] = listener
        return Subscription(self, listener_id)

    def _remove_listener(self, listener_id: int) -> None:
        self._listeners.pop(listener_id, None)

    # ------------------------------------------------------------------
    # Internal application of changes
    # ------------------------------------------------------------------
    def _state(self, model: str) -> _ModelState:
        try:
            return self._models[model]
        except KeyError:
            raise ValueError(f"model is not in the device universe: {model!r}") from None

    def _apply_add_requirement(
        self, requirement: Requirement, cause: str, dispatch: bool
    ) -> None:
        if requirement.requirement_id in self._requirements:
            raise ValueError(
                f"duplicate requirement id: {requirement.requirement_id!r}"
            )

        changes: List[SelectionChange] = []
        self._requirements[requirement.requirement_id] = requirement
        self._requirements_by_capability.setdefault(
            requirement.capability, {}
        )[requirement.requirement_id] = requirement

        for model, state in self._models.items():
            if state.covers(
                requirement.capability,
                requirement.version_from,
                requirement.version_to,
            ):
                continue
            was_selected = not state.unmet
            state.unmet.add(requirement.requirement_id)
            if was_selected:
                changes.append(SelectionChange(LEAVE, model, cause))

        if dispatch:
            self._dispatch(changes)

    def _apply_remove_requirement(self, requirement: Requirement) -> None:
        self._requirements.pop(requirement.requirement_id)
        capability_rows = self._requirements_by_capability.get(
            requirement.capability
        )
        if capability_rows is not None:
            capability_rows.pop(requirement.requirement_id, None)
            if not capability_rows:
                del self._requirements_by_capability[requirement.capability]

        changes: List[SelectionChange] = []
        for model, state in self._models.items():
            if requirement.requirement_id not in state.unmet:
                continue
            state.unmet.discard(requirement.requirement_id)
            if not state.unmet:
                changes.append(SelectionChange(ENTER, model, _REQUIREMENT_REMOVED))
        self._dispatch(changes)

    def _apply_add_fact(self, fact: Fact, cause: str, dispatch: bool) -> None:
        if fact.source_id in self._facts:
            raise ValueError(f"duplicate fact source id: {fact.source_id!r}")
        state = self._state(fact.model)

        payload = (fact.version_from, fact.version_to)
        capability_counts = state.fact_counts.setdefault(fact.capability, {})
        previous_count = capability_counts.get(payload, 0)
        capability_counts[payload] = previous_count + 1
        self._facts[fact.source_id] = fact

        changes: List[SelectionChange] = []
        was_selected = not state.unmet
        if previous_count == 0:
            # Only a new distinct interval can satisfy anything; an exact
            # duplicate report cannot increase coverage.
            for requirement_id, requirement in self._requirements_by_capability.get(
                fact.capability, {}
            ).items():
                if (
                    requirement_id in state.unmet
                    and state.covers(
                        fact.capability,
                        requirement.version_from,
                        requirement.version_to,
                    )
                ):
                    state.unmet.discard(requirement_id)
            if not was_selected and not state.unmet:
                changes.append(SelectionChange(ENTER, fact.model, cause))

        if dispatch:
            self._dispatch(changes)

    def _apply_remove_fact(self, fact: Fact) -> None:
        self._facts.pop(fact.source_id)
        state = self._models[fact.model]
        payload = (fact.version_from, fact.version_to)
        capability_counts = state.fact_counts[fact.capability]
        remaining = capability_counts[payload] - 1
        if remaining > 0:
            # Another identical report still carries the same payload.
            capability_counts[payload] = remaining
            return
        del capability_counts[payload]
        if not capability_counts:
            del state.fact_counts[fact.capability]

        changes: List[SelectionChange] = []
        was_selected = not state.unmet
        for requirement_id, requirement in self._requirements_by_capability.get(
            fact.capability, {}
        ).items():
            if (
                requirement_id not in state.unmet
                and not state.covers(
                    fact.capability,
                    requirement.version_from,
                    requirement.version_to,
                )
            ):
                state.unmet.add(requirement_id)
        if was_selected and state.unmet:
            changes.append(SelectionChange(LEAVE, fact.model, _FACT_REMOVED))
        self._dispatch(changes)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _coerce_requirement(
        requirement: Union[Requirement, str],
        capability: Optional[str],
        version_from: Optional[int],
        version_to: Optional[int],
    ) -> Requirement:
        if isinstance(requirement, Requirement):
            if any(
                value is not None
                for value in (capability, version_from, version_to)
            ):
                raise TypeError(
                    "do not pass extra fields when adding a Requirement object"
                )
            return requirement
        if capability is None or version_from is None or version_to is None:
            raise TypeError(
                "add_requirement expects a Requirement or "
                "(requirement_id, capability, version_from, version_to)"
            )
        return Requirement(requirement, capability, version_from, version_to)

    @staticmethod
    def _coerce_fact(
        fact: Union[Fact, str],
        model: Optional[str],
        capability: Optional[str],
        version_from: Optional[int],
        version_to: Optional[int],
    ) -> Fact:
        if isinstance(fact, Fact):
            if any(
                value is not None
                for value in (model, capability, version_from, version_to)
            ):
                raise TypeError("do not pass extra fields when adding a Fact object")
            return fact
        if (
            model is None
            or capability is None
            or version_from is None
            or version_to is None
        ):
            raise TypeError(
                "add_fact expects a Fact or "
                "(source_id, model, capability, version_from, version_to)"
            )
        return Fact(fact, model, capability, version_from, version_to)

    def _dispatch(self, changes: List[SelectionChange]) -> None:
        if not self._listeners or not changes:
            return
        self._pending_changes.extend(changes)
        if self._dispatching:
            # A listener re-entered the engine.  Its changes are delivered
            # after the current notification, keeping listener calls ordered
            # and complete.
            return
        self._dispatching = True
        try:
            while self._pending_changes:
                change = self._pending_changes.popleft()
                for listener in tuple(self._listeners.values()):
                    listener(change)
        except BaseException:
            # A failing listener aborts the batch.  Discard the undelivered
            # changes rather than replaying them inside an unrelated later
            # mutation; listeners resynchronise from current_result().
            self._pending_changes.clear()
            raise
        finally:
            self._dispatching = False
