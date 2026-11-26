"""Public retention issuance API.

``build_plan`` validates the request, runs the exact lexicographic solver and
turns the result into a :class:`RetentionPlan` that an archive task can execute
directly.
"""

from __future__ import annotations

from .model import (
    CaseOutcome,
    CaseSpec,
    EvidenceChoice,
    LogRecord,
    NormalisedModel,
    RetentionPlan,
    RetentionRequest,
    RetainedLog,
    RetainedSource,
    UnmetEvidence,
    normalise,
)
from .solver import SolveResult, solve


def _retained_log(log: LogRecord) -> RetainedLog:
    return RetainedLog(
        log_id=log.log_id,
        source_id=log.source_id,
        timestamp=log.timestamp,
        size=log.size,
        copied_from=log.copied_from,
    )


def _required_logs(model: NormalisedModel, chosen: set[str]) -> set[str]:
    """Chosen evidence records plus the originals of any chosen receipts."""
    needed = set(chosen)
    for log_id in list(chosen):
        original = model.logs[log_id].copied_from
        while original is not None and original not in needed:
            needed.add(original)
            original = model.logs[original].copied_from
    return needed


def _assemble_sources(
    model: NormalisedModel, selected_sources: frozenset[str], chosen: set[str]
) -> tuple[tuple[RetainedSource, ...], tuple[RetainedLog, ...], tuple[RetainedLog, ...]]:
    needed = _required_logs(model, chosen)
    sources: list[RetainedSource] = []
    kept: list[RetainedLog] = []
    dropped: list[RetainedLog] = []

    for source_id in sorted(selected_sources):
        record = model.sources[source_id]
        kept_ids = [lid for lid in record.log_ids if lid in needed]
        dropped_ids = [lid for lid in record.log_ids if lid not in needed]
        sources.append(
            RetainedSource(
                source_id=source_id,
                size=record.size,
                log_ids=record.log_ids,
                kept_log_ids=tuple(kept_ids),
                dropped_copy_ids=tuple(dropped_ids),
            )
        )
        kept.extend(_retained_log(model.logs[lid]) for lid in kept_ids)
        dropped.extend(_retained_log(model.logs[lid]) for lid in dropped_ids)

    kept.sort(key=lambda r: (r.timestamp, r.log_id))
    dropped.sort(key=lambda r: (r.timestamp, r.log_id))
    return tuple(sources), tuple(kept), tuple(dropped)


def _unmet_for_case(
    model: NormalisedModel,
    case: CaseSpec,
    result: SolveResult,
    selected_cases: set[str],
) -> tuple[UnmetEvidence, ...]:
    if case.case_id in selected_cases:
        return ()
    combos = result.combos_by_case[case.case_id]
    items: list[UnmetEvidence] = []
    for req in case.requirements:
        count = len(model.candidates.get((case.case_id, req.role), ()))
        if count == 0:
            reason = "no_candidate"
        elif not combos:
            reason = "no_feasible_combination"
        elif not result.optimal:
            reason = "not_retained"
        else:
            reason = "budget_insufficient"
        items.append(
            UnmetEvidence(
                case_id=case.case_id,
                role=req.role,
                reason=reason,
                candidate_count=count,
            )
        )
    return tuple(items)


def build_plan(request: RetentionRequest) -> RetentionPlan:
    """Return the retention issuance for ``request``.

    Raises :class:`~retention.model.RetentionInputError` (and its subclasses)
    when the request is malformed or too large to enumerate exactly.
    """
    model = normalise(request)
    result = solve(model)

    selected_cases = {case_id for case_id, _choices in result.selected}
    chosen: set[str] = set()
    for _case_id, choices in result.selected:
        chosen.update(choice.log_id for choice in choices)

    outcomes: list[CaseOutcome] = []
    unmet: list[UnmetEvidence] = []
    by_case = dict(result.selected)
    for case in model.cases:
        choices = by_case.get(case.case_id)
        if choices is not None:
            outcomes.append(
                CaseOutcome(
                    case_id=case.case_id,
                    tier=case.tier,
                    satisfied=True,
                    evidence=choices,
                    unmet=(),
                )
            )
            continue
        unmet_items = _unmet_for_case(model, case, result, selected_cases)
        unmet.extend(unmet_items)
        outcomes.append(
            CaseOutcome(
                case_id=case.case_id,
                tier=case.tier,
                satisfied=False,
                evidence=(),
                unmet=unmet_items,
            )
        )

    sources, kept, dropped = _assemble_sources(model, result.sources, chosen)

    tier_counts: dict[int, int] = {}
    for case in model.cases:
        tier_counts.setdefault(case.tier, 0)
    for outcome in outcomes:
        if outcome.satisfied:
            tier_counts[outcome.tier] += 1
    satisfied_by_tier = tuple(sorted(tier_counts.items(), key=lambda kv: -kv[0]))

    return RetentionPlan(
        budget=model.budget,
        total_size=result.total_size,
        sources=sources,
        logs=kept,
        droppable_copies=dropped,
        cases=tuple(outcomes),
        unmet_evidence=tuple(unmet),
        satisfied_by_tier=satisfied_by_tier,
        optimal=result.optimal,
        search_nodes=result.search_nodes,
    )


__all__ = ["build_plan", "RetentionPlan", "EvidenceChoice"]
