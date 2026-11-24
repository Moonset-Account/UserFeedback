"""Stream-level semantics: visible events, equivalence and counterexamples."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .model import (
    Barrier,
    Declaration,
    InvalidCandidate,
    Message,
    Receipt,
    Stream,
    Update,
    anchor_sequence,
)
from .symbolic import (
    State,
    execute_concrete,
    initial_state,
    lex_smallest_point,
)


@dataclass(frozen=True)
class TraceEntry:
    """One externally visible anchor encountered while replaying a stream."""

    descriptor: Tuple
    observation: Optional[State]  # projected symbolic state for barriers


@dataclass(frozen=True)
class SymbolicTrace:
    entries: Tuple[TraceEntry, ...]
    final_state: State


def symbolic_trace(stream: Stream) -> SymbolicTrace:
    """Run a stream symbolically, recording every anchor in order."""
    state = initial_state(stream.declaration)
    entries: List[TraceEntry] = []
    for message in stream.messages:
        if isinstance(message, Update):
            state = state.apply(stream.declaration.operations[message.operation])
            if message.receipt is not None:
                entries.append(
                    TraceEntry(("receipt", message.receipt), None)
                )
        elif isinstance(message, Barrier):
            projected = state.project(message.registers)
            entries.append(
                TraceEntry(
                    ("barrier", message.registers, message.receipt), projected
                )
            )
        elif isinstance(message, Receipt):
            entries.append(TraceEntry(("receipt", message.receipt_id), None))
        else:  # pragma: no cover - guarded by Stream validation
            raise InvalidCandidate(f"unknown message type {type(message).__name__}")
    return SymbolicTrace(tuple(entries), state)


def concrete_replay(
    declaration: Declaration, entry: Sequence[int], messages: Sequence[Message]
) -> Tuple[List[Tuple], Tuple[int, ...]]:
    """Independent concrete reference replay.

    Returns the anchor observations (``(descriptor, values-or-None)`` in
    order) and the final register values.
    """
    values = list(entry)
    for r, (lo, hi) in enumerate(declaration.registers):
        if not lo <= values[r] <= hi:
            raise ValueError("entry state outside register bounds")
    seen: List[Tuple] = []
    for message in messages:
        if isinstance(message, Update):
            values = list(
                execute_concrete(
                    declaration, values, (message.operation,), validate=False
                )
            )
            if message.receipt is not None:
                seen.append((("receipt", message.receipt), None))
        elif isinstance(message, Barrier):
            observed = tuple(values[r] for r in message.registers)
            seen.append(
                (("barrier", message.registers, message.receipt), observed)
            )
        elif isinstance(message, Receipt):
            seen.append((("receipt", message.receipt_id), None))
    return seen, tuple(values)


@dataclass(frozen=True)
class Mismatch:
    """Description of the first difference between two streams."""

    kind: str  # 'structure' | 'observation' | 'final'
    anchor_index: int  # -1 for final-state differences
    entry: Optional[Tuple[int, ...]]
    descriptor: Optional[Tuple]
    original: object
    candidate: object


@dataclass(frozen=True)
class CandidateVerdict:
    equivalent: bool
    mismatch: Optional[Mismatch]


def compare_streams(original: Stream, candidate: Stream) -> CandidateVerdict:
    """Decide equivalence and, if not equivalent, locate the first difference.

    Two streams are equivalent when their anchor sequences are identical and,
    for *every* legal entry state, every barrier publishes the same values
    and both streams end in the same register state.
    """
    if original.declaration is not candidate.declaration:
        if original.declaration != candidate.declaration:
            raise InvalidCandidate(
                "candidate stream uses a different declaration"
            )
    original_anchors = anchor_sequence(original.messages)
    candidate_anchors = anchor_sequence(candidate.messages)
    if original_anchors != candidate_anchors:
        prefix = min(len(original_anchors), len(candidate_anchors))
        index = prefix
        for i in range(prefix):
            if original_anchors[i] != candidate_anchors[i]:
                index = i
                break
        left = original_anchors[index] if index < len(original_anchors) else None
        right = candidate_anchors[index] if index < len(candidate_anchors) else None
        return CandidateVerdict(
            False,
            Mismatch("structure", index, None, None, left, right),
        )

    trace_a = symbolic_trace(original)
    trace_b = symbolic_trace(candidate)

    first_boxes: Optional[List] = None
    first_index = -1
    first_descriptor = None
    for index, (entry_a, entry_b) in enumerate(
        zip(trace_a.entries, trace_b.entries)
    ):
        if entry_a.descriptor[0] != "barrier":
            continue
        boxes = entry_a.observation.divergence_boxes(entry_b.observation)
        if boxes:
            first_boxes = boxes
            first_index = index
            first_descriptor = entry_a.descriptor
            break

    if first_boxes is not None:
        entry_state = min(
            (lex_smallest_point(box) for box in first_boxes),
            key=lambda point: point,
        )
        seen_a, _ = concrete_replay(
            original.declaration, entry_state, original.messages
        )
        seen_b, _ = concrete_replay(
            candidate.declaration, entry_state, candidate.messages
        )
        value_a = seen_a[first_index][1]
        value_b = seen_b[first_index][1]
        return CandidateVerdict(
            False,
            Mismatch(
                "observation",
                first_index,
                entry_state,
                first_descriptor,
                value_a,
                value_b,
            ),
        )

    final_boxes = trace_a.final_state.divergence_boxes(trace_b.final_state)
    if final_boxes:
        entry_state = min(
            (lex_smallest_point(box) for box in final_boxes),
            key=lambda point: point,
        )
        _, final_a = concrete_replay(
            original.declaration, entry_state, original.messages
        )
        _, final_b = concrete_replay(
            candidate.declaration, entry_state, candidate.messages
        )
        return CandidateVerdict(
            False,
            Mismatch(
                "final", -1, entry_state, None, final_a, final_b
            ),
        )

    return CandidateVerdict(True, None)


def is_equivalent(original: Stream, candidate: Stream) -> bool:
    return compare_streams(original, candidate).equivalent
