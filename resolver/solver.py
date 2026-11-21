"""Cross-compilation dependency solver.

Given a finite universe of candidate package versions, two assembly domains
(host / target) with their runnable platforms, and root requirements that
name a domain each, the solver produces a set of ``(domain, package) ->
version`` selections that assembles successfully.

Placement rules
---------------

* **Build dependencies go to the host domain.**  They are build tooling that
  runs on the build machine.
* **Run dependencies follow the referrer.**  A package selected in a domain
  pulls its run dependencies into the same domain.
* The host and target domains are therefore independent slot spaces.  The
  same package name may be selected at different versions in each domain,
  and neither selection constrains the other.  A host generator and a target
  runtime library may legitimately be different major versions.

Hard constraints
----------------

* One version per slot, where a slot is ``(domain, package name)``.
* A candidate version is usable only on a platform listed in its
  ``platforms`` set (an empty set means universal) for the domain it is
  selected in.  Note that the host domain is judged against
  ``host_platform`` and the target domain against ``target_platform``.
* A dependency edge only contributes a constraint when the platform of its
  *destination* domain is listed in the edge's ``platforms`` set (empty means
  always active).
* Every version constraint on a slot must be satisfied simultaneously.
* Only slots reachable from a root requirement through active edges are
  selected.

Objective and tie-break
-----------------------

1. Minimise *re-versioned locked slots*: slots present in both the old lock
   and the solution whose version changed.  Locked slots that are not
   selected at all are free.  Slots absent from the lock are free.
2. On a tie, compare version vectors slot by slot in fixed slot order
   (host before target, then package name) and take the lexicographically
   greatest vector at the first differing slot.  A slot that is not selected
   ranks below every selected version.

The search is a deterministic exhaustive depth-first search with constraint
propagation and backtracking, so a constraint discovered deeper in the graph
can force a previously selected version to be re-chosen.

Failure
-------

When no assembly exists, the result carries a :class:`ConflictReport` whose
chains each start at a root requirement and end at the constraint that
participates in the contradiction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .model import (
    DOMAIN_RANK,
    ChainLink,
    ConflictReport,
    Domain,
    PackageVersion,
    ResolutionRequest,
    ResolutionResult,
    RootRequirement,
    Selection,
    dependency_target_domain,
)
from .versions import Version, VersionRange

Slot = Tuple[Domain, str]

#: Sorting marker for "slot not selected".  Selected entries are prefixed
#: with a strictly larger marker so an absent slot ranks below every version.
_ABSENT = (0,)
_PRESENT = (1,)


class ResolutionLimitError(RuntimeError):
    """Raised when the search exceeds the caller-supplied node budget."""


def slot_sort_key(slot: Slot) -> Tuple[int, str]:
    """Fixed slot order: domain rank first, then package name."""
    return (DOMAIN_RANK[slot[0]], slot[1])


@dataclass(frozen=True)
class _Constraint:
    """A version range imposed on a slot, plus the chain that justifies it."""

    range: VersionRange
    chain: Tuple[ChainLink, ...]


@dataclass
class _State:
    constraints: Dict[Slot, List[_Constraint]] = field(default_factory=dict)
    assigned: Dict[Slot, PackageVersion] = field(default_factory=dict)
    why_assigned: Dict[Slot, Tuple[ChainLink, ...]] = field(default_factory=dict)

    def copy(self) -> "_State":
        return _State(
            constraints={slot: list(items) for slot, items in self.constraints.items()},
            assigned=dict(self.assigned),
            why_assigned=dict(self.why_assigned),
        )


class _Solver:
    def __init__(self, request: ResolutionRequest, max_nodes: Optional[int]) -> None:
        self.request = request
        self.max_nodes = max_nodes
        self.nodes = 0
        self._by_name: Dict[str, List[PackageVersion]] = {}
        for candidate in request.candidates:
            self._by_name.setdefault(candidate.name, []).append(candidate)
        for versions in self._by_name.values():
            # Descending order gives the lexicographically greatest first,
            # which is both the tie-break default and a good search order.
            versions.sort(key=lambda pkg: pkg.version, reverse=True)
        self._lock: Dict[Slot, Version] = {
            entry.slot: entry.version for entry in request.lock
        }
        self._best_key: Optional[Tuple] = None
        self._best: Optional[Tuple[Selection, ...]] = None
        self._best_changed: Optional[int] = None
        self._conflicts: List[Tuple[Tuple[Slot, str, Tuple[ChainLink, ...]]]] = []

    # -- platforms --------------------------------------------------------
    def _platform(self, domain: Domain) -> str:
        if domain is Domain.HOST:
            return self.request.host_platform
        return self.request.target_platform

    def _candidate_runs_on(self, candidate: PackageVersion, domain: Domain) -> bool:
        platforms = candidate.platforms
        return not platforms or self._platform(domain) in platforms

    def _edge_active(self, platforms, destination: Domain) -> bool:
        return not platforms or self._platform(destination) in platforms

    # -- constraint accumulation -----------------------------------------
    def _merged_range(self, state: _State, slot: Slot) -> Optional[VersionRange]:
        merged = VersionRange.any()
        for constraint in state.constraints.get(slot, ()):
            merged = merged.intersect(constraint.range)
            if merged is None:
                return None
        return merged

    def _viable(self, state: _State, slot: Slot) -> Tuple[PackageVersion, ...]:
        merged = self._merged_range(state, slot)
        if merged is None:
            return ()
        domain = slot[0]
        return tuple(
            candidate
            for candidate in self._by_name.get(slot[1], ())
            if self._candidate_runs_on(candidate, domain)
            and merged.allows(candidate.version)
        )

    # -- conflict recording ----------------------------------------------
    def _record(self, slot: Slot, reason: str, chains: Iterable[Tuple[ChainLink, ...]]) -> None:
        unique: List[Tuple[ChainLink, ...]] = []
        for chain in chains:
            if chain and chain not in unique:
                unique.append(chain)
        self._conflicts.append((slot, reason, tuple(unique)))

    def _first_disjoint_pair(
        self, constraints: List[_Constraint]
    ) -> Optional[Tuple[_Constraint, _Constraint]]:
        for i in range(len(constraints)):
            for j in range(i + 1, len(constraints)):
                if constraints[i].range.intersect(constraints[j].range) is None:
                    return constraints[i], constraints[j]
        return None

    def _check_slot(self, state: _State, slot: Slot) -> bool:
        """Return ``True`` when ``slot`` is already in conflict."""
        constraints = state.constraints.get(slot, [])
        if not constraints:
            return False

        merged = self._merged_range(state, slot)
        if merged is None:
            pair = self._first_disjoint_pair(constraints)
            if pair is not None:
                detail = " and ".join(
                    f"{c.chain[-1].target} {c.range.describe()}" for c in pair
                )
                self._record(
                    slot,
                    f"version constraints have no common value ({detail})",
                    (pair[0].chain, pair[1].chain),
                )
            else:
                self._record(
                    slot,
                    "version constraints have no common value",
                    (c.chain for c in constraints),
                )
            return True

        chosen = state.assigned.get(slot)
        if chosen is not None:
            if merged.allows(chosen.version):
                return False
            offenders = [
                c for c in constraints if not c.range.allows(chosen.version)
            ]
            chains = [c.chain for c in offenders]
            who = state.why_assigned.get(slot)
            if who is not None:
                chains.append(who)
            self._record(
                slot,
                (
                    f"selected {slot[1]}@{chosen.version} outside "
                    f"{merged.describe()}"
                ),
                chains,
            )
            return True

        if not self._viable(state, slot):
            if not self._by_name.get(slot[1]):
                self._record(
                    slot,
                    f"no candidate version of {slot[1]} exists",
                    (c.chain for c in constraints),
                )
            else:
                self._record(
                    slot,
                    (
                        f"no candidate version of {slot[1]} satisfies "
                        f"{merged.describe()} on platform "
                        f"{self._platform(slot[0])!r}"
                    ),
                    (c.chain for c in constraints),
                )
            return True
        return False

    def _explain(self) -> ConflictReport:
        if not self._conflicts:
            root = self.request.roots[0] if self.request.roots else RootRequirement(
                name="<none>", domain=Domain.HOST
            )
            chain = (self._root_link(root),)
            return ConflictReport(
                domain=root.domain,
                package=root.name,
                reason="no satisfying assembly exists",
                chains=(chain,),
            )
        # Prefer the explanation with the most distinct chains: it shows more
        # of the contradiction.  Ties keep the first recorded (shallowest)
        # conflict, which is the one closest to the roots.
        slot, reason, chains = max(
            self._conflicts, key=lambda item: len(item[2])
        )
        return ConflictReport(
            domain=slot[0], package=slot[1], reason=reason, chains=chains
        )

    # -- objective ---------------------------------------------------------
    def _changed_locked(self, selections: Dict[Slot, Version]) -> int:
        return sum(
            1
            for slot, locked in self._lock.items()
            if slot in selections and selections[slot] != locked
        )

    def _tiebreak_key(self, selections: Dict[Slot, Version]) -> Tuple:
        # The slot universe is every (domain, name) that appears as a
        # candidate; domains and names are ordered as required.
        universe = sorted(
            {(domain, candidate.name) for candidate in self.request.candidates
             for domain in (Domain.HOST, Domain.TARGET)},
            key=slot_sort_key,
        )
        key = []
        for slot in universe:
            if slot in selections:
                key.append(_PRESENT + selections[slot])
            else:
                key.append(_ABSENT)
        return tuple(key)

    def _offer(self, state: _State) -> None:
        selections = {
            slot: candidate.version for slot, candidate in state.assigned.items()
        }
        changed = self._changed_locked(selections)
        key = (-changed, self._tiebreak_key(selections))
        if self._best_key is None or key > self._best_key:
            self._best_key = key
            self._best_changed = changed
            self._best = tuple(
                Selection(domain=slot[0], name=slot[1], version=state.assigned[slot].version)
                for slot in sorted(selections, key=slot_sort_key)
            )

    # -- search ------------------------------------------------------------
    @staticmethod
    def _root_link(root: RootRequirement) -> ChainLink:
        return ChainLink(
            domain=root.domain,
            package=None,
            version=None,
            kind=None,
            target_domain=root.domain,
            target=root.name,
            constraint=root.range,
        )

    def _seed(self) -> _State:
        state = _State()
        for root in self.request.roots:
            slot = (root.domain, root.name)
            state.constraints.setdefault(slot, []).append(
                _Constraint(root.range, (self._root_link(root),))
            )
        return state

    @staticmethod
    def _shortest_chain(state: _State, slot: Slot) -> Tuple[ChainLink, ...]:
        return min(
            (c.chain for c in state.constraints.get(slot, ())),
            key=len,
            default=(),
        )

    def _assign(self, state: _State, slot: Slot, candidate: PackageVersion) -> _State:
        new_state = state.copy()
        new_state.assigned[slot] = candidate
        new_state.why_assigned[slot] = self._shortest_chain(state, slot)
        referrer_domain = slot[0]
        for dependency in candidate.dependencies:
            destination = dependency_target_domain(dependency.kind, referrer_domain)
            if not self._edge_active(dependency.platforms, destination):
                continue
            target_slot = (destination, dependency.name)
            link = ChainLink(
                domain=referrer_domain,
                package=candidate.name,
                version=candidate.version,
                kind=dependency.kind,
                target_domain=destination,
                target=dependency.name,
                constraint=dependency.range,
            )
            new_state.constraints.setdefault(target_slot, []).append(
                _Constraint(
                    dependency.range,
                    new_state.why_assigned[slot] + (link,),
                )
            )
        return new_state

    def _partial_changed(self, state: _State) -> int:
        return self._changed_locked(
            {slot: candidate.version for slot, candidate in state.assigned.items()}
        )

    def _ordered_candidates(self, state: _State, slot: Slot) -> List[PackageVersion]:
        locked = self._lock.get(slot)
        candidates = list(self._viable(state, slot))
        # Keep the locked version first when it is viable, then descending
        # version order (``_by_name`` is already sorted that way).
        candidates.sort(
            key=lambda candidate: (
                0 if locked is not None and candidate.version == locked else 1,
                tuple(-part for part in candidate.version),
            )
        )
        return candidates

    def _search(self, state: _State) -> None:
        self.nodes += 1
        if self.max_nodes is not None and self.nodes > self.max_nodes:
            raise ResolutionLimitError(
                f"dependency search exceeded {self.max_nodes} node budget"
            )

        slots = sorted(state.constraints, key=slot_sort_key)
        for slot in slots:
            if self._check_slot(state, slot):
                return

        pending = [slot for slot in slots if slot not in state.assigned]
        if not pending:
            self._offer(state)
            return

        # Fail-fast: resolve the most constrained slot first.  Ties fall back
        # to the fixed slot order so the search is deterministic.
        pending.sort(
            key=lambda slot: (len(self._viable(state, slot)), slot_sort_key(slot))
        )
        slot = pending[0]

        for candidate in self._ordered_candidates(state, slot):
            if self._best_changed is not None:
                extra = 1 if self._lock.get(slot, candidate.version) != candidate.version else 0
                if self._partial_changed(state) + extra > self._best_changed:
                    continue
            self._search(self._assign(state, slot, candidate))

    def solve(self) -> ResolutionResult:
        self._search(self._seed())
        if self._best is not None:
            selections = {(s.domain, s.name): s.version for s in self._best}
            return ResolutionResult(
                selections=self._best,
                changed_locked_slots=self._changed_locked(selections),
            )
        return ResolutionResult(
            selections=(), changed_locked_slots=0, failure=self._explain()
        )


def resolve(
    request: ResolutionRequest, *, max_nodes: Optional[int] = None
) -> ResolutionResult:
    """Resolve ``request``.

    ``max_nodes`` caps the number of search nodes visited; exceeding it raises
    :class:`ResolutionLimitError` rather than returning a partial answer.
    """
    if not isinstance(request, ResolutionRequest):
        raise TypeError("resolve() expects a ResolutionRequest")
    return _Solver(request, max_nodes).solve()
