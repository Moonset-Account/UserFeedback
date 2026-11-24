"""Eligibility, recusal, laboratory separation, and diagnostic helpers."""

from __future__ import annotations

from .models import (
    CandidatePairStatus,
    Exhibit,
    Reviewer,
    ReviewerQualificationGap,
    UnassignedReport,
)

QUALIFICATION_MODES = ("each", "collective")


def recusal_reason(exhibit: Exhibit, reviewer: Reviewer) -> str | None:
    """Return the recusal reason, or ``None`` when no conflict is known."""

    if exhibit.source_group is not None and (
        exhibit.source_group in reviewer.exposed_source_groups
    ):
        return "same_source_contact"
    if exhibit.exhibit_id in reviewer.recused_exhibit_ids:
        return "prior_exhibit_contact"
    return None


def missing_qualifications(exhibit: Exhibit, reviewer: Reviewer) -> tuple[str, ...]:
    return tuple(sorted(exhibit.required_qualifications - reviewer.qualifications))


def is_available(exhibit: Exhibit, reviewer: Reviewer) -> bool:
    """Active and not recused; capacity and laboratory pairing are not checked."""

    return reviewer.active and recusal_reason(exhibit, reviewer) is None


def pair_qualifies(
    exhibit: Exhibit,
    first: Reviewer,
    second: Reviewer,
    qualification_mode: str = "each",
) -> bool:
    """Check qualification rules for a proposed pair."""

    if qualification_mode == "each":
        return not missing_qualifications(exhibit, first) and not (
            missing_qualifications(exhibit, second)
        )
    if qualification_mode == "collective":
        combined = first.qualifications | second.qualifications
        return exhibit.required_qualifications <= combined
    raise ValueError(f"unknown qualification_mode: {qualification_mode}")


def structural_pair_allowed(
    exhibit: Exhibit,
    first: Reviewer,
    second: Reviewer,
    qualification_mode: str = "each",
) -> bool:
    """Eligibility excluding capacity.

    Capacity is deliberately excluded so diagnostics can distinguish a skill /
    conflict / laboratory gap from a quota bottleneck.
    """

    if first.reviewer_id == second.reviewer_id:
        return False
    if not first.active or not second.active:
        return False
    if recusal_reason(exhibit, first) is not None:
        return False
    if recusal_reason(exhibit, second) is not None:
        return False
    if first.lab == second.lab:
        return False
    return pair_qualifies(exhibit, first, second, qualification_mode)


def pair_options(
    exhibit: Exhibit,
    reviewers: tuple[Reviewer, ...],
    qualification_mode: str = "each",
) -> tuple[tuple[int, int], ...]:
    """Return all structurally legal ``(lower_index, higher_index)`` pairs."""

    options: list[tuple[int, int]] = []
    for left in range(len(reviewers)):
        for right in range(left + 1, len(reviewers)):
            if structural_pair_allowed(
                exhibit, reviewers[left], reviewers[right], qualification_mode
            ):
                options.append((left, right))
    return tuple(options)


def _qualification_compatible_pairs_ignoring_lab(
    exhibit: Exhibit,
    reviewers: tuple[Reviewer, ...],
    qualification_mode: str,
) -> list[tuple[Reviewer, Reviewer]]:
    result: list[tuple[Reviewer, Reviewer]] = []
    for i, first in enumerate(reviewers):
        if not first.active or recusal_reason(exhibit, first) is not None:
            continue
        for second in reviewers[i + 1 :]:
            if not second.active or recusal_reason(exhibit, second) is not None:
                continue
            if pair_qualifies(exhibit, first, second, qualification_mode):
                result.append((first, second))
    return result


def build_unassigned_report(
    exhibit: Exhibit,
    reviewers: tuple[Reviewer, ...],
    structural_pairs: tuple[tuple[int, int], ...],
    loads: dict[str, int],
    qualification_mode: str = "each",
) -> UnassignedReport:
    """Create a structured qualification/recusal/lab/capacity explanation."""

    by_id = {reviewer.reviewer_id: reviewer for reviewer in reviewers}
    recused = tuple(
        reviewer.reviewer_id
        for reviewer in reviewers
        if recusal_reason(exhibit, reviewer) is not None
    )
    inactive = tuple(
        reviewer.reviewer_id for reviewer in reviewers if not reviewer.active
    )
    available = tuple(
        reviewer for reviewer in reviewers if is_available(exhibit, reviewer)
    )

    held_qualifications: set[str] = set()
    for reviewer in available:
        held_qualifications.update(reviewer.qualifications)
    global_missing = tuple(
        sorted(exhibit.required_qualifications - held_qualifications)
    )

    gaps = tuple(
        ReviewerQualificationGap(
            reviewer_id=reviewer.reviewer_id,
            missing_qualifications=missing_qualifications(exhibit, reviewer),
        )
        for reviewer in available
        if missing_qualifications(exhibit, reviewer)
    )

    if qualification_mode == "each":
        eligible = tuple(
            reviewer.reviewer_id
            for reviewer in available
            if not missing_qualifications(exhibit, reviewer)
        )
    else:
        # In collective mode an available reviewer can contribute as long as
        # some available partner can complete the required qualification set.
        eligible_ids: list[str] = []
        for reviewer in available:
            partner_found = any(
                other.reviewer_id != reviewer.reviewer_id
                and pair_qualifies(exhibit, reviewer, other, qualification_mode)
                for other in available
            )
            if partner_found:
                eligible_ids.append(reviewer.reviewer_id)
        eligible = tuple(eligible_ids)

    candidate_pairs: list[CandidatePairStatus] = []
    for left_index, right_index in structural_pairs:
        left = reviewers[left_index]
        right = reviewers[right_index]
        left_remaining = left.capacity - loads[left.reviewer_id]
        right_remaining = right.capacity - loads[right.reviewer_id]
        blockers = tuple(
            reviewer.reviewer_id
            for reviewer, remaining in (
                (left, left_remaining),
                (right, right_remaining),
            )
            if remaining <= 0
        )
        candidate_pairs.append(
            CandidatePairStatus(
                reviewer_ids=(left.reviewer_id, right.reviewer_id),
                labs=(left.lab, right.lab),
                remaining_capacities=(left_remaining, right_remaining),
                saturated_blocker_ids=blockers,
            )
        )

    reason_codes: list[str] = []
    primary_reason: str
    message: str

    if structural_pairs:
        primary_reason = "capacity_constraint"
        reason_codes.append("capacity_constraint")
        if all(pair.saturated_blocker_ids for pair in candidate_pairs):
            reason_codes.append("all_candidate_pairs_have_saturated_reviewer")
        min_extra = min(
            len(pair.saturated_blocker_ids) for pair in candidate_pairs
        )
        saturated = sorted(
            {
                blocker
                for pair in candidate_pairs
                for blocker in pair.saturated_blocker_ids
            }
        )
        message = (
            "存在满足资质、回避和异实验室要求的复核人组合，但最优覆盖下这些组合均受名额约束；"
            f"饱和候选复核人：{', '.join(saturated) or '无'}。"
        )
        return UnassignedReport(
            exhibit_id=exhibit.exhibit_id,
            primary_reason=primary_reason,
            reason_codes=tuple(reason_codes),
            message=message,
            eligible_reviewer_ids=eligible,
            recused_reviewer_ids=recused,
            inactive_reviewer_ids=inactive,
            missing_qualifications=global_missing,
            reviewer_qualification_gaps=gaps,
            cross_lab_pair_count=len(structural_pairs),
            candidate_pairs=tuple(candidate_pairs),
            minimum_additional_slots=min_extra,
        )

    # No structural pair. Diagnose qualification, recusal, and laboratory factors.
    min_extra = None

    if global_missing:
        reason_codes.append("unheld_qualification")
    if qualification_mode == "each" and len(eligible) < 2:
        reason_codes.append("insufficient_qualified_reviewers")
    elif qualification_mode == "collective" and len(available) >= 2:
        lab_ignoring_pairs = _qualification_compatible_pairs_ignoring_lab(
            exhibit, reviewers, qualification_mode
        )
        if not lab_ignoring_pairs:
            reason_codes.append("no_qualification_complete_pair")

    if len(available) < 2:
        reason_codes.append("insufficient_available_reviewers")

    # Would a pair exist if same-source/prior-contact recusal were ignored?
    # Inactive reviewers remain excluded because activity is not a conflict.
    active_reviewers = tuple(r for r in reviewers if r.active)
    pair_without_recusal = any(
        first.reviewer_id != second.reviewer_id
        and first.lab != second.lab
        and pair_qualifies(exhibit, first, second, qualification_mode)
        for i, first in enumerate(active_reviewers)
        for second in active_reviewers[i + 1 :]
    )
    if recused and pair_without_recusal:
        reason_codes.append("recusal_conflict")

    qualification_pairs_any_lab = _qualification_compatible_pairs_ignoring_lab(
        exhibit, reviewers, qualification_mode
    )
    if qualification_pairs_any_lab and not structural_pairs:
        reason_codes.append("same_lab_constraint")

    if inactive and len(available) < 2:
        reason_codes.append("inactive_reviewers")

    qualification_blocker = (
        "unheld_qualification" in reason_codes
        or "no_qualification_complete_pair" in reason_codes
        or (
            "insufficient_qualified_reviewers" in reason_codes
            and bool(gaps)
        )
    )
    if qualification_blocker:
        primary_reason = "qualification_gap"
        if global_missing:
            message = (
                "资质缺口：以下所需资质没有任何可参与且未回避的复核人持有："
                + ", ".join(global_missing)
                + "。"
            )
        else:
            message = (
                "资质缺口：没有足够的、分别满足该证物全部资质要求且未回避的复核人；"
                "详见 reviewer_qualification_gaps。"
            )
    elif "recusal_conflict" in reason_codes:
        primary_reason = "recusal_conflict"
        message = (
            "回避约束导致没有合法的异实验室双人组合；须回避复核人："
            + ", ".join(recused)
            + "。"
        )
    elif "same_lab_constraint" in reason_codes:
        primary_reason = "lab_constraint"
        labs = sorted({reviewer.lab for reviewer in available})
        message = (
            "实验室约束：满足资质和回避要求的复核人无法组成来自不同实验室的两人组合；"
            f"相关实验室：{', '.join(labs)}。"
        )
    else:
        primary_reason = "no_eligible_pair"
        reason_codes.append("no_eligible_pair")
        message = "不存在同时满足资质、回避、在岗和异实验室要求的两位复核人。"

    return UnassignedReport(
        exhibit_id=exhibit.exhibit_id,
        primary_reason=primary_reason,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        message=message,
        eligible_reviewer_ids=eligible,
        recused_reviewer_ids=recused,
        inactive_reviewer_ids=inactive,
        missing_qualifications=global_missing,
        reviewer_qualification_gaps=gaps,
        cross_lab_pair_count=0,
        candidate_pairs=(),
        minimum_additional_slots=min_extra,
    )
