"""Pedagogical fixed-length byte-digest comparison with an inspectable trace.

This module is a teaching implementation, not a claim of cryptographic
constant-time behaviour.  It deliberately scans every byte position of the two
fixed-length inputs, records each read / xor / accumulate step it performs, and
only derives the boolean result after the whole scan.  A caller can therefore
check that the comparison path does not depend on where the inputs differ.

Length validation happens before any byte is read, so inputs of the wrong
length fail with a dedicated :class:`DigestLengthError` instead of being folded
into a normal ``False`` comparison result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

DEFAULT_DIGEST_LENGTH = 32

READ_LEFT = "read_left"
READ_RIGHT = "read_right"
XOR = "xor"
ACCUMULATE = "accumulate"

STEP_KINDS = (READ_LEFT, READ_RIGHT, XOR, ACCUMULATE)


class DigestLengthError(ValueError):
    """Raised independently of comparison when an input has the wrong length."""

    def __init__(self, actual_lengths: Mapping[str, int], expected_length: int):
        self.actual_lengths = dict(actual_lengths)
        self.expected_length = expected_length
        self.sides = tuple(self.actual_lengths)
        details = ", ".join(
            f"{side}={length}" for side, length in self.actual_lengths.items()
        )
        super().__init__(
            f"digest length error ({details}); expected_length={expected_length}"
        )


@dataclass(frozen=True)
class CompareStep:
    """One operation performed by the comparison routine.

    ``position`` is the byte index touched by the operation.  No byte value is
    stored, so traces can be logged without disclosing digest material.
    """

    kind: str
    position: int

    def label(self) -> str:
        return f"{self.kind}@{self.position}"


@dataclass(frozen=True)
class ComparisonTrace:
    """The ordered operations executed for one comparison."""

    digest_length: int
    steps: tuple[CompareStep, ...]
    equal: bool

    @property
    def signature(self) -> tuple[tuple[str, int], ...]:
        return tuple((step.kind, step.position) for step in self.steps)

    @property
    def step_count(self) -> int:
        return len(self.steps)

    def read_positions(self, side: str = "left") -> tuple[int, ...]:
        if side not in ("left", "right"):
            raise ValueError("side must be 'left' or 'right'")
        kind = READ_LEFT if side == "left" else READ_RIGHT
        return tuple(step.position for step in self.steps if step.kind == kind)

    def describe_difference(self, baseline: "ComparisonTrace") -> str:
        return describe_trace_difference(baseline, self)


def validate_lengths(
    left: object, right: object, expected_length: int
) -> None:
    """Validate both inputs independently, before any comparison step."""
    if expected_length < 1:
        raise ValueError("expected_length must be at least 1")
    bad: dict[str, int] = {}
    left_length = len(left)  # type: ignore[arg-type]
    right_length = len(right)  # type: ignore[arg-type]
    if left_length != expected_length:
        bad["left"] = left_length
    if right_length != expected_length:
        bad["right"] = right_length
    if bad:
        raise DigestLengthError(bad, expected_length)


def _scan(
    left: Sequence[int],
    right: Sequence[int],
    expected_length: int,
    record_steps: bool,
) -> tuple[bool, ComparisonTrace]:
    validate_lengths(left, right, expected_length)
    steps: list[CompareStep] = []
    difference = 0
    for position in range(expected_length):
        if record_steps:
            steps.append(CompareStep(READ_LEFT, position))
        left_byte = left[position]
        if record_steps:
            steps.append(CompareStep(READ_RIGHT, position))
        right_byte = right[position]
        if record_steps:
            steps.append(CompareStep(XOR, position))
        xor_value = left_byte ^ right_byte
        if record_steps:
            steps.append(CompareStep(ACCUMULATE, position))
        difference |= xor_value
    equal = difference == 0
    trace = ComparisonTrace(expected_length, tuple(steps), equal)
    return equal, trace


def compare_digests(
    left: Sequence[int],
    right: Sequence[int],
    expected_length: int = DEFAULT_DIGEST_LENGTH,
) -> bool:
    """Return whether two equal-length fixed-size digests are identical.

    Raises :class:`DigestLengthError` when either input is not exactly
    ``expected_length`` bytes.  The input is never scanned before that check.
    """
    equal, _ = _scan(left, right, expected_length, record_steps=False)
    return equal


def compare_digests_with_trace(
    left: Sequence[int],
    right: Sequence[int],
    expected_length: int = DEFAULT_DIGEST_LENGTH,
) -> tuple[bool, ComparisonTrace]:
    """Compare and return ``(equal, trace)`` for regression inspection."""
    return _scan(left, right, expected_length, record_steps=True)


def describe_trace_difference(
    baseline: ComparisonTrace, candidate: ComparisonTrace
) -> str:
    """Describe how ``candidate``'s operation trace differs from ``baseline``.

    The description contains only step kinds, byte positions and counts; it
    never contains digest bytes.
    """
    if baseline.signature == candidate.signature:
        return "none"
    counts = f"step_count {baseline.step_count}->{candidate.step_count}"
    shared = min(baseline.step_count, candidate.step_count)
    for index in range(shared):
        base_step = baseline.steps[index]
        candidate_step = candidate.steps[index]
        if base_step != candidate_step:
            return (
                f"{counts}; first_divergence=step {index} "
                f"baseline={base_step.label()} candidate={candidate_step.label()}"
            )
    return f"{counts}; first_divergence=step {shared} (one trace is a prefix)"
