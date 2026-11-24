"""Shared declaration builders and exhaustive/concrete checking helpers."""

from __future__ import annotations

import itertools
from typing import Iterable, Tuple

from compressor import (
    ASSIGN,
    COND,
    SADD,
    SWAP,
    Declaration,
    Operation,
    Stream,
    concrete_replay,
    symbolic_trace,
)


def all_entry_states(declaration: Declaration) -> Iterable[Tuple[int, ...]]:
    """Enumerate every legal entry state (only for small test domains)."""
    axes = [range(lo, hi + 1) for lo, hi in declaration.registers]
    return itertools.product(*axes)


def assert_streams_agree_everywhere(testcase, original: Stream, candidate: Stream):
    """Exhaustive concrete equivalence over all entry states plus anchors."""
    trace_a = symbolic_trace(original)
    trace_b = symbolic_trace(candidate)
    testcase.assertEqual(
        [entry.descriptor for entry in trace_a.entries],
        [entry.descriptor for entry in trace_b.entries],
    )
    for entry in all_entry_states(original.declaration):
        seen_a, final_a = concrete_replay(
            original.declaration, entry, original.messages
        )
        seen_b, final_b = concrete_replay(
            candidate.declaration, entry, candidate.messages
        )
        testcase.assertEqual(seen_a, seen_b)
        testcase.assertEqual(final_a, final_b)


def small_declaration() -> Declaration:
    return Declaration(
        registers=((0, 10), (-5, 5)),
        constants=(-5, -3, -1, 0, 1, 3, 5, 7),
        operations=(
            Operation(SADD, target=0, constant=3),       # 0
            Operation(SADD, target=0, constant=-3),      # 1
            Operation(ASSIGN, target=0, constant=7),     # 2
            Operation(ASSIGN, target=0, constant=5),     # 3
            Operation(SWAP, target=0, source=1),         # 4
            Operation(SADD, target=1, constant=1),       # 5
            Operation(SADD, target=1, constant=-1),      # 6
            Operation(
                COND,
                target=0,
                source=1,
                low=-3,
                high=3,
                constant=7,
                action=ASSIGN,
            ),                                          # 7
            Operation(
                COND,
                target=1,
                source=0,
                low=0,
                high=5,
                constant=1,
                action=SADD,
            ),                                          # 8
        ),
    )
