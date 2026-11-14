"""Comparison of a confirmed baseline export with the current build export.

Rules enforced here:

* Two records are comparable only when they share the same content identity
  **and** the same parse mode **and** the same environment.  If the same
  content appears in the baseline only under another mode/environment, it is
  emitted as a scope mismatch and is neither compared nor counted as a new
  failure or a vanished baseline row.
* A group of attempts is ``passed`` when every executed attempt passed,
  ``failed`` when it failed at least once and never passed, ``unstable`` when
  at least one attempt passed and at least one failed (the single pass + single
  fail case included), and ``not_executed`` when every attempt was skipped.
* Duplicate attempt numbers inside one group make that group's outcome
  ambiguous, so the group is emitted under ``needs_confirmation`` and is not
  used for new/fixed/persistent classification.
* Samples are matched across builds by content (or declared content hash), so a
  sample whose content is unchanged still matches its baseline entry after a
  rename; the row records the rename.
* Failure categories are compared as sets; categories unseen in the baseline
  are enumerated separately from pre-existing ones.  Timeouts are labelled with
  an explicit origin so a long-standing timeout is never mistaken for a newly
  introduced error class.
"""

import re
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

from .identity import comparison_environment, comparison_mode
from .models import FAILED, PASSED, SKIPPED, Report, TestRecord

STATE_PASSED = "passed"
STATE_FAILED = "failed"
STATE_UNSTABLE = "unstable"
STATE_NOT_EXECUTED = "not_executed"
STATE_ABSENT = "absent"

TIMEOUT = "timeout"

SECTION_NEW_FAILURE = "new_failures"
SECTION_FIXED = "fixed"
SECTION_PERSISTENT = "persistent_failures"
SECTION_UNSTABLE = "unstable"
SECTION_NOT_EXECUTED = "not_executed"
SECTION_MISSING = "missing_from_current"
SECTION_SCOPE_MISMATCH = "scope_mismatch"
SECTION_CONFLICT = "needs_confirmation"

SECTION_ORDER = (
    SECTION_NEW_FAILURE,
    SECTION_FIXED,
    SECTION_PERSISTENT,
    SECTION_UNSTABLE,
    SECTION_NOT_EXECUTED,
    SECTION_MISSING,
    SECTION_SCOPE_MISMATCH,
    SECTION_CONFLICT,
)

GroupKey = Tuple[str, str, str]  # identity, mode, environment


def _normalized_mode(record: TestRecord) -> str:
    return comparison_mode(record.parse_mode)


def _normalized_environment(record: TestRecord) -> str:
    return comparison_environment(record.environment)


def group_key(record: TestRecord) -> GroupKey:
    return (record.identity, _normalized_mode(record), _normalized_environment(record))


def _group_records(report: Report) -> Dict[GroupKey, List[TestRecord]]:
    groups: Dict[GroupKey, List[TestRecord]] = defaultdict(list)
    for record in report.records:
        groups[group_key(record)].append(record)
    return dict(groups)


def _duplicate_attempts(records: Sequence[TestRecord]) -> Dict[int, List[TestRecord]]:
    by_attempt: Dict[int, List[TestRecord]] = defaultdict(list)
    for record in records:
        by_attempt[record.attempt].append(record)
    return {attempt: items for attempt, items in by_attempt.items() if len(items) > 1}


def _sorted_attempts(records: Sequence[TestRecord]) -> List[TestRecord]:
    return sorted(
        records,
        key=lambda r: (r.attempt, r.status, r.failure_category or "", r.sample_name),
    )


def _outcome(records: Sequence[TestRecord]) -> Optional[str]:
    if not records:
        return None
    statuses = {record.status for record in records}
    if statuses == {SKIPPED}:
        return STATE_NOT_EXECUTED
    has_pass = PASSED in statuses
    has_fail = FAILED in statuses
    if has_pass and has_fail:
        return STATE_UNSTABLE
    if has_fail:
        return STATE_FAILED
    return STATE_PASSED


def _failure_categories(records: Sequence[TestRecord]) -> List[str]:
    categories = {
        record.failure_category
        for record in records
        if record.status == FAILED and record.failure_category
    }
    return sorted(categories)


_CATEGORY_HEAD = re.compile(r"[^a-z0-9]+")


def _is_timeout(category: str) -> bool:
    """A category counts as a timeout when its leading token is ``timeout``.

    Leading-token matching keeps ``timeout``, ``timeout:30s`` and
    ``timeout/read`` in the timeout family while leaving unrelated categories
    such as ``parser:timeout_inside_value`` out of it.
    """
    tokens = [token for token in _CATEGORY_HEAD.split(category.strip().casefold())
              if token]
    return bool(tokens) and tokens[0] == TIMEOUT


def _timeout_origin(current_categories: Sequence[str],
                    baseline_categories: Sequence[str]) -> str:
    current_timeout = any(_is_timeout(cat) for cat in current_categories)
    baseline_timeout = any(_is_timeout(cat) for cat in baseline_categories)
    if current_timeout and baseline_timeout:
        return "pre_existing_timeout"
    if current_timeout:
        return "new_timeout"
    if baseline_timeout:
        return "timeout_resolved"
    return "no_timeout"


def _attempt_view(records: Sequence[TestRecord]) -> List[dict]:
    return [
        {
            "attempt": record.attempt,
            "status": record.status,
            "failure_category": record.failure_category,
            "sample_name": record.sample_name,
        }
        for record in _sorted_attempts(records)
    ]


def _apply_rename(view: dict, current_records: Sequence[TestRecord],
                  baseline_records: Sequence[TestRecord]) -> None:
    current_names = sorted({record.sample_name for record in current_records})
    baseline_names = sorted({record.sample_name for record in baseline_records})
    if not current_names and not baseline_names:
        return
    display_names = current_names or baseline_names
    view["sample"] = display_names[0] if len(display_names) == 1 else display_names
    view["baseline_sample"] = (
        baseline_names[0] if len(baseline_names) == 1 else baseline_names
    )
    view["renamed"] = bool(current_names and baseline_names
                           and current_names != baseline_names)


def _common_view(key: GroupKey, current_records: Sequence[TestRecord],
                 baseline_records: Sequence[TestRecord]) -> dict:
    # Display fields fall back to the baseline when the content is absent from
    # the current export, but every "current side" assessment (outcome,
    # categories, timeout) stays empty so a missing row is never described as
    # if this build had run it.
    current = list(current_records)
    baseline = list(baseline_records)
    display = current or baseline
    current_cats = _failure_categories(current)
    baseline_cats = _failure_categories(baseline)
    view = {
        "identity": key[0],
        "identity_basis": display[0].identity_basis,
        "summary": display[0].summary,
        "parse_mode": display[0].parse_mode,
        "environment": display[0].environment,
        # ``build_id`` is the build that produced the row's current attempts;
        # it stays null when the content is only present in the baseline.
        "build_id": current[0].build_id if current else None,
        "baseline_build_id": baseline[0].build_id if baseline else None,
        "attempts": [record.attempt for record in _sorted_attempts(current)],
        "baseline_attempts": [record.attempt for record in _sorted_attempts(baseline)],
        "attempt_details": _attempt_view(current),
        "baseline_attempt_details": _attempt_view(baseline),
        "failure_categories": current_cats,
        "baseline_failure_categories": baseline_cats,
        "new_failure_categories": (sorted(set(current_cats) - set(baseline_cats))
                                   if current else []),
        "pre_existing_failure_categories": (sorted(set(current_cats) & set(baseline_cats))
                                            if current else []),
        "timeout": (_timeout_origin(current_cats, baseline_cats)
                    if current else "not_applicable"),
        "current_outcome": _outcome(current),
        "baseline_outcome": _outcome(baseline) or STATE_ABSENT,
        "scope_evidence": [],
        "current_scope_evidence": [],
        "notes": [],
    }
    _apply_rename(view, current, baseline)
    return view


def _has_other_scope(groups: Dict[GroupKey, List[TestRecord]],
                     identity: str, key: GroupKey) -> bool:
    """True when ``groups`` holds this content under a different scope."""
    return any(other[0] == identity and other != key for other in groups)


def _current_scope_evidence(groups: Dict[GroupKey, List[TestRecord]],
                            identity: str, key: GroupKey) -> List[dict]:
    """Describe same-content groups in ``groups`` outside ``key``."""
    return [
        {
            "parse_mode": groups[other][0].parse_mode,
            "environment": groups[other][0].environment,
            "outcome": _outcome(groups[other]) or STATE_ABSENT,
            "attempts": [record.attempt for record in _sorted_attempts(groups[other])],
        }
        for other in sorted(groups)
        if other[0] == identity and other != key
    ]


def _row_sort_key(row: dict) -> Tuple[str, str, str, str]:
    sample = row.get("sample")
    sample_text = sample if isinstance(sample, str) else ", ".join(map(str, sample or []))
    return (
        str(row.get("parse_mode", "")).casefold(),
        str(row.get("environment", "")).casefold(),
        sample_text.casefold(),
        str(row.get("identity", "")),
    )


def _build_conflict_row(key: GroupKey, current_records: Sequence[TestRecord],
                        baseline_records: Sequence[TestRecord],
                        current_dups: Dict[int, List[TestRecord]],
                        baseline_dups: Dict[int, List[TestRecord]]) -> dict:
    view = _common_view(key, current_records, baseline_records)
    conflicts = []
    for label, dups in (("current", current_dups), ("baseline", baseline_dups)):
        for attempt, records in sorted(dups.items()):
            conflicts.append({
                "build": label,
                "attempt": attempt,
                "entries": [
                    {
                        "status": record.status,
                        "failure_category": record.failure_category,
                        "sample_name": record.sample_name,
                    }
                    for record in sorted(
                        records,
                        key=lambda r: (r.status, r.failure_category or "", r.sample_name),
                    )
                ],
            })
    view.update({
        "section": SECTION_CONFLICT,
        "conflicts": conflicts,
        "status": "pending_confirmation",
        "current_outcome": None,
        "notes": view["notes"] + [
            "duplicate attempt number(s) in one mode/environment group; "
            "outcome left unclassified until the conflict is resolved"
        ],
    })
    return view


def compare_reports(baseline: Report, current: Report) -> dict:
    """Return a fully materialised regression summary as plain data."""
    baseline_groups = _group_records(baseline)
    current_groups = _group_records(current)

    sections: Dict[str, List[dict]] = {name: [] for name in SECTION_ORDER}
    counts = {
        "new_failures": 0,
        "fixed": 0,
        "persistent_failures": 0,
        "unstable": 0,
        "not_executed": 0,
        "missing_from_current": 0,
        "scope_mismatch": 0,
        "needs_confirmation": 0,
        "stable_passes": 0,
        "newly_passing": 0,
    }
    # Disposition of each group:
    #   conflict               - duplicate attempt numbers, outcome withheld
    #   not_executed           - all current attempts skipped
    #   scope_mismatch         - current content whose baseline lives only in
    #                            another mode/environment
    #   scope_mismatch_baseline- baseline content whose current counterpart is
    #                            not comparable or was reported elsewhere
    #   suppressed             - baseline row covered by a scope-mismatch row
    #   missing                - baseline content absent from the current export
    #   compared               - both sides present in the same scope
    dispositions: Dict[GroupKey, str] = {}
    compared_groups = 0

    all_keys = sorted(set(baseline_groups) | set(current_groups),
                      key=lambda k: (k[1], k[2], k[0]))
    current_identities = {key[0] for key in current_groups}

    # Pass 1: decide how each group is handled before emitting any row, so
    # pass 2 never has to guess whether a same-content group under another
    # scope actually produced a scope-mismatch row.
    for key in all_keys:
        baseline_records = baseline_groups.get(key, [])
        current_records = current_groups.get(key, [])
        if (_duplicate_attempts(current_records) or _duplicate_attempts(baseline_records)):
            dispositions[key] = "conflict"
        elif current_records and _outcome(current_records) == STATE_NOT_EXECUTED:
            dispositions[key] = "not_executed"
        elif not baseline_records and _has_other_scope(baseline_groups, key[0], key):
            dispositions[key] = "scope_mismatch"
        elif not current_records:
            dispositions[key] = "baseline_only"
        else:
            dispositions[key] = "compared"

    # Identities already described by a current-side scope-mismatch row.
    emitted_scope_identities = {
        key[0] for key, disposition in dispositions.items()
        if disposition == "scope_mismatch"
    }
    for key, disposition in list(dispositions.items()):
        if disposition != "baseline_only":
            continue
        identity = key[0]
        if identity in emitted_scope_identities:
            # The content is still exported under another scope, and that row
            # already shows both scopes: do not also report it as missing.
            dispositions[key] = "suppressed"
        elif identity in current_identities:
            # The content is present under another scope, but that scope is
            # held for confirmation or was not executed, so no scope-mismatch
            # row was produced from it. Describe the shift from this side
            # instead of calling the sample missing.
            dispositions[key] = "scope_mismatch_baseline"
        else:
            dispositions[key] = "missing"

    # Pass 2: emit rows.
    for key in all_keys:
        baseline_records = baseline_groups.get(key, [])
        current_records = current_groups.get(key, [])
        disposition = dispositions[key]

        if disposition == "suppressed":
            continue

        if disposition == "conflict":
            row = _build_conflict_row(
                key, current_records, baseline_records,
                _duplicate_attempts(current_records),
                _duplicate_attempts(baseline_records),
            )
            sections[SECTION_CONFLICT].append(row)
            counts["needs_confirmation"] += 1
            continue

        view = _common_view(key, current_records, baseline_records)
        current_state = _outcome(current_records)
        baseline_state = _outcome(baseline_records) or STATE_ABSENT
        view["scope_evidence"] = _current_scope_evidence(
            baseline_groups, key[0], key
        )

        if disposition == "missing":
            view["section"] = SECTION_MISSING
            view["notes"].append("no matching group in the current export")
            sections[SECTION_MISSING].append(view)
            counts["missing_from_current"] += 1
            continue

        if disposition == "scope_mismatch_baseline":
            view["current_scope_evidence"] = _current_scope_evidence(
                current_groups, key[0], key
            )
            view["section"] = SECTION_SCOPE_MISMATCH
            view["notes"].append(
                "the current export has this content only under a different "
                "parse mode/environment, which is reported separately; the "
                "baseline row is not compared"
            )
            sections[SECTION_SCOPE_MISMATCH].append(view)
            counts["scope_mismatch"] += 1
            continue

        if current_state == STATE_NOT_EXECUTED:
            view["section"] = SECTION_NOT_EXECUTED
            view["notes"].append("every current attempt is skipped")
            sections[SECTION_NOT_EXECUTED].append(view)
            counts["not_executed"] += 1
            continue

        if disposition == "scope_mismatch":
            # Same content exists in the baseline only under another
            # mode/environment: not comparable, so it cannot be a new failure.
            view["section"] = SECTION_SCOPE_MISMATCH
            view["notes"].append(
                "baseline has this content only under a different "
                "parse mode/environment, so no direct comparison was made"
            )
            sections[SECTION_SCOPE_MISMATCH].append(view)
            counts["scope_mismatch"] += 1
            continue

        compared_groups += 1

        if current_state == STATE_UNSTABLE:
            view["section"] = SECTION_UNSTABLE
            if baseline_state == STATE_UNSTABLE:
                view["notes"].append("baseline group was unstable as well")
            elif baseline_state == STATE_PASSED:
                view["notes"].append("baseline group passed; instability is new")
            sections[SECTION_UNSTABLE].append(view)
            counts["unstable"] += 1
            continue

        if current_state == STATE_FAILED:
            if baseline_state in (STATE_FAILED, STATE_UNSTABLE):
                view["section"] = SECTION_PERSISTENT
                if baseline_state == STATE_UNSTABLE:
                    view["notes"].append(
                        "baseline group contained failures but also passes; "
                        "the failure persists in every current attempt"
                    )
                sections[SECTION_PERSISTENT].append(view)
                counts["persistent_failures"] += 1
            else:
                view["section"] = SECTION_NEW_FAILURE
                if baseline_state == STATE_ABSENT:
                    view["notes"].append("no baseline record for this content/scope")
                elif baseline_state == STATE_NOT_EXECUTED:
                    view["notes"].append(
                        "baseline attempts were all skipped, so this failure "
                        "was not observed previously"
                    )
                sections[SECTION_NEW_FAILURE].append(view)
                counts["new_failures"] += 1
            continue

        # current_state == STATE_PASSED
        if baseline_state in (STATE_FAILED, STATE_UNSTABLE):
            view["section"] = SECTION_FIXED
            if baseline_state == STATE_UNSTABLE:
                view["notes"].append(
                    "baseline group was unstable; every current attempt passes"
                )
            sections[SECTION_FIXED].append(view)
            counts["fixed"] += 1
        elif baseline_state == STATE_PASSED:
            counts["stable_passes"] += 1
            if any(record.status == SKIPPED for record in current_records):
                view["notes"].append("some current attempts were skipped")
        else:
            counts["newly_passing"] += 1

    for name in SECTION_ORDER:
        sections[name].sort(key=_row_sort_key)

    suppressed_groups = sum(1 for disposition in dispositions.values()
                            if disposition == "suppressed")

    return {
        "baseline": {
            "build_id": baseline.build_id,
            "source": baseline.source,
            "group_count": baseline.group_count(),
            "attempt_count": len(baseline.records),
            "path": baseline.path,
        },
        "current": {
            "build_id": current.build_id,
            "source": current.source,
            "group_count": current.group_count(),
            "attempt_count": len(current.records),
            "path": current.path,
        },
        "compared_groups": compared_groups,
        "suppressed_groups": suppressed_groups,
        "accounted_groups": len(all_keys),
        "counts": counts,
        "sections": sections,
    }
