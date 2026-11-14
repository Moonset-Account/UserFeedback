"""Deliberately non-conforming comparison implementations.

These exist only as regression probes.  They return the *same booleans* as the
real comparator but stop reading at the first differing byte, so they model the
exact future change the trace audit is meant to catch.  Nothing here is used by
``app.py`` at runtime.
"""

from __future__ import annotations

import digest_compare


def early_return_compare_with_trace(
    left, right, expected_length=digest_compare.DEFAULT_DIGEST_LENGTH
):
    """Stop at the first differing byte, recording only the steps performed."""
    digest_compare.validate_lengths(left, right, expected_length)
    steps = []
    for position in range(expected_length):
        steps.append(digest_compare.CompareStep(digest_compare.READ_LEFT, position))
        left_byte = left[position]
        steps.append(digest_compare.CompareStep(digest_compare.READ_RIGHT, position))
        right_byte = right[position]
        steps.append(digest_compare.CompareStep(digest_compare.XOR, position))
        steps.append(digest_compare.CompareStep(digest_compare.ACCUMULATE, position))
        if left_byte != right_byte:
            return False, digest_compare.ComparisonTrace(
                expected_length, tuple(steps), False
            )
    return True, digest_compare.ComparisonTrace(expected_length, tuple(steps), True)


def early_return_compare(
    left, right, expected_length=digest_compare.DEFAULT_DIGEST_LENGTH
) -> bool:
    """Boolean-only early return: correct result, difference-dependent path."""
    digest_compare.validate_lengths(left, right, expected_length)
    for position in range(expected_length):
        if left[position] != right[position]:
            return False
    return True


def early_return_scan(left, right, expected_length, record_steps):
    """``digest_compare._scan``-shaped adapter for patching in tests."""
    return early_return_compare_with_trace(left, right, expected_length)
