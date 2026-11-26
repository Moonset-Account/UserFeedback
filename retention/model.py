"""Data model, validation and normalisation for the audit retention planner.

The planner works on three kinds of input:

* :class:`LogRecord` -- one concrete log/receipt with its original source,
  timestamp and occupancy (``size``).
* :class:`CaseSpec` -- a filed incident and the evidence roles it must prove
  (by default: initiation, independent approval and actual effect).
* :class:`EvidenceLink` -- the event relation "this log can serve as this
  role for this case".

:func:`normalise` validates the request and turns it into an immutable,
deterministically ordered internal model that the solver consumes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

Timestamp = int | float


class RetentionInputError(ValueError):
    """The retention request is structurally invalid."""


class RetentionCapacityError(RetentionInputError):
    """A case has more candidate evidence combinations than can be enumerated."""


@dataclass(frozen=True)
class EvidenceRequirement:
    """One evidence item a case needs.

    ``after`` places this role at or after another role of the same case
    (strictly after when ``strict_after`` is true).
    ``distinct_source_from`` requires the chosen log's original source to
    differ from the referenced role's source -- this is how an approval is
    kept independent from the initiation even when a receipt copy of the same
    original record is linked as a candidate.
    """

    role: str
    after: str | None = None
    strict_after: bool = False
    distinct_source_from: str | None = None


#: The default evidence combination: one initiation, one approval that is
#: independent of the initiation and not earlier than it, and one effect that
#: is not earlier than the approval.
DEFAULT_REQUIREMENTS: tuple[EvidenceRequirement, ...] = (
    EvidenceRequirement("initiation"),
    EvidenceRequirement(
        "approval", after="initiation", distinct_source_from="initiation"
    ),
    EvidenceRequirement("effect", after="approval"),
)


@dataclass(frozen=True)
class LogRecord:
    """A single log or receipt.

    ``source_id`` identifies the original record.  Several logs may share one
    ``source_id``; they are counted (and charged) as a single source.
    ``copied_from`` optionally records that this log is a receipt copied from
    another log; a receipt must resolve to the same ``source_id`` as its
    original.
    """

    log_id: str
    source_id: str
    timestamp: Timestamp
    size: int
    copied_from: str | None = None


@dataclass(frozen=True)
class CaseSpec:
    """A filed incident that must be provable with an evidence combination."""

    case_id: str
    tier: int
    requirements: tuple[EvidenceRequirement, ...] = DEFAULT_REQUIREMENTS


@dataclass(frozen=True)
class EvidenceLink:
    """Event relation: ``log_id`` may serve as ``role`` of ``case_id``."""

    case_id: str
    role: str
    log_id: str


@dataclass(frozen=True)
class RetentionRequest:
    """Complete input of one retention issuance."""

    logs: Sequence[LogRecord]
    cases: Sequence[CaseSpec]
    links: Sequence[EvidenceLink]
    budget: int
    source_costs: Mapping[str, int] | None = None
    max_search_nodes: int = 200_000
    max_combo_nodes: int = 500_000


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceChoice:
    """The log selected for one evidence role of a satisfied case."""

    role: str
    log_id: str
    source_id: str
    timestamp: Timestamp
    size: int


@dataclass(frozen=True)
class UnmetEvidence:
    """One evidence item that could not be retained, with a reason code.

    Reason codes:

    ``no_candidate``
        No log is linked as this role for the case.
    ``no_feasible_combination``
        Candidates exist but ordering/independence constraints leave no
        feasible combination for the case.
    ``budget_insufficient``
        A feasible combination exists but does not fit the remaining budget
        of the (optimal) plan.
    ``not_retained``
        Feasible, but not selected by the plan; used when the solver hit its
        search limit and optimality is not proven.
    """

    case_id: str
    role: str
    reason: str
    candidate_count: int


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    tier: int
    satisfied: bool
    evidence: tuple[EvidenceChoice, ...]
    unmet: tuple[UnmetEvidence, ...]


@dataclass(frozen=True)
class RetainedSource:
    """A source selected for retention, charged once regardless of copies."""

    source_id: str
    size: int
    log_ids: tuple[str, ...]
    kept_log_ids: tuple[str, ...] = ()
    dropped_copy_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetainedLog:
    """A concrete log belonging to a retained source."""

    log_id: str
    source_id: str
    timestamp: Timestamp
    size: int
    copied_from: str | None


@dataclass(frozen=True)
class RetentionPlan:
    """Retention issuance returned for the archive task to execute."""

    budget: int
    total_size: int
    sources: tuple[RetainedSource, ...]
    logs: tuple[RetainedLog, ...]
    droppable_copies: tuple[RetainedLog, ...]
    cases: tuple[CaseOutcome, ...]
    unmet_evidence: tuple[UnmetEvidence, ...]
    satisfied_by_tier: tuple[tuple[int, int], ...]
    optimal: bool
    search_nodes: int

    @property
    def remaining_budget(self) -> int:
        return self.budget - self.total_size

    @property
    def satisfied_case_ids(self) -> tuple[str, ...]:
        return tuple(c.case_id for c in self.cases if c.satisfied)


# ---------------------------------------------------------------------------
# Internal normalised model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    size: int
    log_ids: tuple[str, ...]


@dataclass(frozen=True)
class NormalisedModel:
    logs: Mapping[str, LogRecord]
    sources: Mapping[str, SourceRecord]
    cases: tuple[CaseSpec, ...]
    requirements: Mapping[str, tuple[EvidenceRequirement, ...]]
    candidates: Mapping[tuple[str, str], tuple[str, ...]]
    budget: int
    max_search_nodes: int
    max_combo_nodes: int


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RetentionInputError(f"{label} must be a non-empty string, got {value!r}")
    return value


def _check_size(value: object, label: str) -> int:
    if not _is_int(value) or value < 0:
        raise RetentionInputError(
            f"{label} must be a non-negative integer, got {value!r}"
        )
    return value  # type: ignore[return-value]


def _check_timestamp(value: object, label: str) -> Timestamp:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RetentionInputError(
            f"{label} must be an int or float epoch-style timestamp, got {value!r}"
        )
    try:
        finite = math.isfinite(float(value))
    except OverflowError:
        finite = False
    if not finite:
        raise RetentionInputError(f"{label} must be finite, got {value!r}")
    return value  # type: ignore[return-value]


def _check_sequence(value: object, label: str) -> Sequence:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RetentionInputError(f"{label} must be a sequence, got {type(value).__name__}")
    return value


def _check_requirement_cycles(
    case_id: str, requirements: tuple[EvidenceRequirement, ...]
) -> None:
    edges: dict[str, list[str]] = {req.role: [] for req in requirements}
    for req in requirements:
        if req.after is not None:
            edges[req.role].append(req.after)

    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> None:
        if node in done:
            return
        if node in visiting:
            raise RetentionInputError(
                f"case {case_id!r} has cyclic ordering constraints around role {node!r}"
            )
        visiting.add(node)
        for nxt in edges[node]:
            visit(nxt)
        visiting.remove(node)
        done.add(node)

    for node in edges:
        visit(node)


def _normalise_case(case: object, index: int) -> CaseSpec:
    if not isinstance(case, CaseSpec):
        raise RetentionInputError(
            f"cases[{index}] must be a CaseSpec, got {type(case).__name__}"
        )
    case_id = _check_id(case.case_id, f"cases[{index}].case_id")
    if not _is_int(case.tier):
        raise RetentionInputError(
            f"case {case_id!r} tier must be an integer, got {case.tier!r}"
        )
    raw_requirements = _check_sequence(
        case.requirements, f"case {case_id!r} requirements"
    )
    if not raw_requirements:
        raise RetentionInputError(f"case {case_id!r} must declare at least one role")

    requirements: list[EvidenceRequirement] = []
    roles: list[str] = []
    for j, req in enumerate(raw_requirements):
        if not isinstance(req, EvidenceRequirement):
            raise RetentionInputError(
                f"case {case_id!r} requirement {j} must be an EvidenceRequirement"
            )
        role = _check_id(req.role, f"case {case_id!r} requirement role")
        if role in roles:
            raise RetentionInputError(
                f"case {case_id!r} declares role {role!r} more than once"
            )
        if req.after is None and req.strict_after:
            raise RetentionInputError(
                f"case {case_id!r} role {role!r} sets strict_after without after"
            )
        if req.after == role:
            raise RetentionInputError(
                f"case {case_id!r} role {role!r} cannot be after itself"
            )
        if req.distinct_source_from == role:
            raise RetentionInputError(
                f"case {case_id!r} role {role!r} cannot require a source distinct from itself"
            )
        roles.append(role)
        requirements.append(req)

    for req in requirements:
        if req.after is not None and req.after not in roles:
            raise RetentionInputError(
                f"case {case_id!r} role {req.role!r} refers to unknown role {req.after!r} in after"
            )
        if (
            req.distinct_source_from is not None
            and req.distinct_source_from not in roles
        ):
            raise RetentionInputError(
                f"case {case_id!r} role {req.role!r} refers to unknown role "
                f"{req.distinct_source_from!r} in distinct_source_from"
            )

    normalised = tuple(requirements)
    _check_requirement_cycles(case_id, normalised)
    return CaseSpec(case_id=case_id, tier=case.tier, requirements=normalised)


def _normalise_logs(raw_logs: Sequence) -> dict[str, LogRecord]:
    logs: dict[str, LogRecord] = {}
    for i, log in enumerate(raw_logs):
        if not isinstance(log, LogRecord):
            raise RetentionInputError(
                f"logs[{i}] must be a LogRecord, got {type(log).__name__}"
            )
        log_id = _check_id(log.log_id, f"logs[{i}].log_id")
        if log_id in logs:
            raise RetentionInputError(f"duplicate log id {log_id!r}")
        _check_id(log.source_id, f"log {log_id!r} source_id")
        _check_timestamp(log.timestamp, f"log {log_id!r} timestamp")
        _check_size(log.size, f"log {log_id!r} size")
        if log.copied_from is not None:
            copied_from = _check_id(log.copied_from, f"log {log_id!r} copied_from")
            if copied_from == log_id:
                raise RetentionInputError(f"log {log_id!r} cannot be copied from itself")
        logs[log_id] = log

    for log in logs.values():
        if log.copied_from is None:
            continue
        original = logs.get(log.copied_from)
        if original is None:
            raise RetentionInputError(
                f"log {log.log_id!r} is copied from unknown log {log.copied_from!r}"
            )
        if original.source_id != log.source_id:
            raise RetentionInputError(
                f"log {log.log_id!r} claims source {log.source_id!r} but its original "
                f"{original.log_id!r} belongs to source {original.source_id!r}"
            )

    _check_copy_cycles(logs)
    return logs


def _check_copy_cycles(logs: Mapping[str, LogRecord]) -> None:
    state: dict[str, int] = {}

    for start in logs:
        if state.get(start) == 2:
            continue
        chain: list[str] = []
        node: str | None = start
        while node is not None and state.get(node, 0) == 0:
            state[node] = 1
            chain.append(node)
            node = logs[node].copied_from
        if node is not None and state.get(node) == 1:
            raise RetentionInputError(
                f"copied_from relation contains a cycle involving log {node!r}"
            )
        for visited in chain:
            state[visited] = 2


def _normalise_cases(raw_cases: Sequence) -> tuple[CaseSpec, ...]:
    cases: list[CaseSpec] = []
    seen: set[str] = set()
    for i, case in enumerate(raw_cases):
        normalised = _normalise_case(case, i)
        if normalised.case_id in seen:
            raise RetentionInputError(f"duplicate case id {normalised.case_id!r}")
        seen.add(normalised.case_id)
        cases.append(normalised)
    cases.sort(key=lambda c: (-c.tier, c.case_id))
    return tuple(cases)


def _normalise_links(
    raw_links: Sequence,
    cases: tuple[CaseSpec, ...],
    requirements: Mapping[str, tuple[EvidenceRequirement, ...]],
    logs: Mapping[str, LogRecord],
) -> tuple[EvidenceLink, ...]:
    links: list[EvidenceLink] = []
    seen: set[tuple[str, str, str]] = set()
    for i, link in enumerate(raw_links):
        if not isinstance(link, EvidenceLink):
            raise RetentionInputError(
                f"links[{i}] must be an EvidenceLink, got {type(link).__name__}"
            )
        case_id = _check_id(link.case_id, f"links[{i}].case_id")
        role = _check_id(link.role, f"links[{i}].role")
        log_id = _check_id(link.log_id, f"links[{i}].log_id")
        if case_id not in requirements:
            raise RetentionInputError(f"link refers to unknown case {case_id!r}")
        if role not in {req.role for req in requirements[case_id]}:
            raise RetentionInputError(
                f"link refers to unknown role {role!r} of case {case_id!r}"
            )
        if log_id not in logs:
            raise RetentionInputError(f"link refers to unknown log {log_id!r}")
        key = (case_id, role, log_id)
        if key in seen:
            # Duplicate relation; keep a single copy.
            continue
        seen.add(key)
        links.append(EvidenceLink(case_id, role, log_id))
    return tuple(links)


def _normalise_source_costs(
    raw_costs: Mapping[str, int] | None, logs: Mapping[str, LogRecord]
) -> dict[str, int]:
    if raw_costs is None:
        return {}
    if not isinstance(raw_costs, Mapping):
        raise RetentionInputError(
            f"source_costs must be a mapping, got {type(raw_costs).__name__}"
        )
    known_sources = {log.source_id for log in logs.values()}
    costs: dict[str, int] = {}
    for source_id, size in raw_costs.items():
        _check_id(source_id, "source_costs key")
        if source_id not in known_sources:
            raise RetentionInputError(
                f"source_costs refers to unknown source {source_id!r}"
            )
        costs[source_id] = _check_size(size, f"source_costs[{source_id!r}]")
    return costs


def normalise(request: RetentionRequest) -> NormalisedModel:
    """Validate ``request`` and build the internal planning model."""

    if not isinstance(request, RetentionRequest):
        raise RetentionInputError(
            f"request must be a RetentionRequest, got {type(request).__name__}"
        )

    budget = _check_size(request.budget, "budget")
    max_search_nodes = request.max_search_nodes
    if not _is_int(max_search_nodes) or max_search_nodes < 1:
        raise RetentionInputError(
            f"max_search_nodes must be a positive integer, got {max_search_nodes!r}"
        )
    max_combo_nodes = request.max_combo_nodes
    if not _is_int(max_combo_nodes) or max_combo_nodes < 1:
        raise RetentionInputError(
            f"max_combo_nodes must be a positive integer, got {max_combo_nodes!r}"
        )

    logs = _normalise_logs(_check_sequence(request.logs, "logs"))
    cases = _normalise_cases(_check_sequence(request.cases, "cases"))
    requirements = {case.case_id: case.requirements for case in cases}
    links = _normalise_links(
        _check_sequence(request.links, "links"), cases, requirements, logs
    )
    overrides = _normalise_source_costs(request.source_costs, logs)

    # Group logs by their original source.  A source is charged once; without
    # an explicit override the largest declared occupancy in the family is
    # used (conservative and deterministic).
    family: dict[str, list[LogRecord]] = {}
    for log in logs.values():
        family.setdefault(log.source_id, []).append(log)

    sources: dict[str, SourceRecord] = {}
    for source_id, records in family.items():
        records.sort(key=lambda r: (r.timestamp, r.log_id))
        default_size = max(record.size for record in records)
        sources[source_id] = SourceRecord(
            source_id=source_id,
            size=overrides.get(source_id, default_size),
            log_ids=tuple(record.log_id for record in records),
        )

    candidates: dict[tuple[str, str], tuple[str, ...]] = {}
    for link in links:
        key = (link.case_id, link.role)
        candidates.setdefault(key, ())
        candidates[key] = candidates[key] + (link.log_id,)
    for key, log_ids in list(candidates.items()):
        candidates[key] = tuple(
            sorted(set(log_ids), key=lambda lid: (logs[lid].timestamp, lid))
        )

    return NormalisedModel(
        logs=logs,
        sources=sources,
        cases=cases,
        requirements=requirements,
        candidates=candidates,
        budget=budget,
        max_search_nodes=max_search_nodes,
        max_combo_nodes=max_combo_nodes,
    )
