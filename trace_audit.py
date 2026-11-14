"""Trace audit: compare recorded operation traces against an independent spec.

The expected trace is built here from a written specification, not by calling
the comparison routine again.  That makes a regression check meaningful: if
``digest_compare`` is ever changed to stop at the first difference, its traces
no longer match the spec and the audit names the affected ``case_id`` groups.

Report lines contain only digest length, trace-difference summaries and pass
status.  Digest bytes are never formatted.
"""

from __future__ import annotations

from dataclasses import dataclass

import digest_compare
from digest_compare import ComparisonTrace, DigestLengthError
from synthetic_cases import SyntheticCase

FULL_SCAN_SPEC = (
    "for every byte position p in 0..length-1, in order: read left[p], "
    "read right[p], xor, accumulate"
)


def expected_signature(length: int) -> tuple[tuple[str, int], ...]:
    """Independent spec of the full-scan trace, in exact operation order."""
    signature: list[tuple[str, int]] = []
    for position in range(length):
        signature.append((digest_compare.READ_LEFT, position))
        signature.append((digest_compare.READ_RIGHT, position))
        signature.append((digest_compare.XOR, position))
        signature.append((digest_compare.ACCUMULATE, position))
    return tuple(signature)


@dataclass(frozen=True)
class TraceRecord:
    """Report-safe outcome for one synthetic case."""

    case_id: str
    length: int
    result_matches: bool
    trace_matches: bool
    difference: str

    @property
    def passed(self) -> bool:
        return self.result_matches and self.trace_matches

    def report_line(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        result = "ok" if self.result_matches else "wrong-boolean"
        return (
            f"case_id={self.case_id} length={self.length} "
            f"trace_difference={self.difference} "
            f"result_check={result} status={status}"
        )


def audit_case(
    case: SyntheticCase, comparator=digest_compare.compare_digests_with_trace
) -> TraceRecord:
    """Run one case and compare its trace with the full-scan spec."""
    try:
        equal, trace = comparator(case.left, case.right, case.length)
    except DigestLengthError as exc:  # defensive: fixtures are well-formed
        return TraceRecord(case.case_id, case.length, False, False, f"error: {exc}")
    result_matches = equal is case.expected_equal
    expected = ComparisonTrace(
        case.length,
        tuple(
            digest_compare.CompareStep(kind, pos)
            for kind, pos in expected_signature(case.length)
        ),
        case.expected_equal,
    )
    difference = digest_compare.describe_trace_difference(expected, trace)
    return TraceRecord(
        case_id=case.case_id,
        length=case.length,
        result_matches=result_matches,
        trace_matches=(difference == "none"),
        difference=difference,
    )


def audit_group(
    cases: list[SyntheticCase], comparator=digest_compare.compare_digests_with_trace
) -> list[TraceRecord]:
    return [audit_case(case, comparator) for case in cases]


def changed_case_ids(records: list[TraceRecord]) -> list[str]:
    """Which case groups have a trace that departs from the spec."""
    return [record.case_id for record in records if not record.trace_matches]


def summarize(records: list[TraceRecord]) -> str:
    total = len(records)
    passed = sum(1 for record in records if record.passed)
    changed = changed_case_ids(records)
    changed_text = ",".join(changed) if changed else "none"
    return (
        f"cases={total} passed={passed} failed={total - passed} "
        f"changed_case_ids={changed_text}"
    )
