"""Optimal (shortest encoded) equivalent message stream construction.

Receipts are hard, in-situ boundaries: updates may never move across a
receipt, so the stream is cut into independent segments at every receipt.
Barriers are soft boundaries: an update may be scheduled across a barrier as
long as every barrier still publishes the same values for every legal entry
state.  Within a segment the compressor searches the shortest update
sequence; ties are broken by the lexicographic ordering of the operation
indices emitted.
"""

from __future__ import annotations

from dataclasses import dataclass
import heapq
from typing import Callable, List, Optional, Sequence, Tuple

from .model import (
    Barrier,
    Declaration,
    Message,
    Receipt,
    Stream,
    Update,
)
from .symbolic import State, initial_state


@dataclass(frozen=True)
class Segment:
    items: Tuple[Message, ...]          # Updates and Barriers only
    terminal: Optional[Tuple]           # receipt descriptor ending the segment


@dataclass(frozen=True)
class CompressionResult:
    stream: Stream
    encoded_bytes: int
    update_count: int
    nodes_explored: int
    truncated: bool = False


def split_segments(messages: Sequence[Message]) -> Tuple[Segment, ...]:
    """Cut a message stream at every receipt (standalone or attached).

    A receipt attached to an :class:`Update` is re-emitted as a standalone
    :class:`Receipt` at the segment end (identical anchor descriptor).  A
    receipt attached to a :class:`Barrier` stays on the barrier, because the
    receipt is part of the barrier anchor; the segment still ends there so no
    update can move across it.
    """
    segments: List[Segment] = []
    items: List[Message] = []
    for message in messages:
        if isinstance(message, Update):
            if message.receipt is None:
                items.append(message)
            else:
                items.append(Update(message.operation))
                segments.append(
                    Segment(tuple(items), ("receipt", message.receipt))
                )
                items = []
        elif isinstance(message, Barrier):
            items.append(message)  # keeps any attached receipt
            if message.receipt is not None:
                segments.append(
                    Segment(tuple(items), ("barrier-receipt", message.receipt))
                )
                items = []
        elif isinstance(message, Receipt):
            segments.append(Segment(tuple(items), ("receipt", message.receipt_id)))
            items = []
        else:  # pragma: no cover - guarded by Stream validation
            raise TypeError(f"unknown message type {type(message).__name__}")
    segments.append(Segment(tuple(items), None))
    return tuple(segments)


class _Node:
    """Heap node; ordering is (byte cost, operation tuple, insertion order)."""

    __slots__ = ("cost", "seq", "order", "state", "consumed", "trace")

    def __init__(self, cost, seq, order, state, consumed, trace=()):
        self.cost = cost
        self.seq = seq
        self.order = order
        self.state = state
        self.consumed = consumed
        # Interleaved history: ("u", op_index) or ("b", barrier_index).
        self.trace = trace

    def _key(self):
        return (self.cost, self.seq, self.order)

    def __eq__(self, other):
        return self._key() == other._key()

    def __lt__(self, other):
        return self._key() < other._key()

    def __hash__(self):
        return hash(self._key())


@dataclass
class _SearchPlan:
    declaration: Declaration
    barriers: Tuple[Barrier, ...]
    expected_observations: Tuple[State, ...]
    goal: State
    max_ops: int

    def barrier_holds(self, state: State, consumed: int) -> bool:
        barrier = self.barriers[consumed]
        projected = state.project(barrier.registers)
        return projected.pieces == self.expected_observations[consumed].pieces


def _build_plan(declaration: Declaration, segment: Segment) -> _SearchPlan:
    barriers: List[Barrier] = []
    expected: List[State] = []
    state = initial_state(declaration)
    update_count = 0
    for message in segment.items:
        if isinstance(message, Barrier):
            barriers.append(message)
            expected.append(state.project(message.registers))
        else:
            update_count += 1
            state = state.apply(declaration.operations[message.operation])
    return _SearchPlan(
        declaration=declaration,
        barriers=tuple(barriers),
        expected_observations=tuple(expected),
        goal=state,
        max_ops=update_count,
    )


def compress_segment(
    declaration: Declaration,
    segment: Segment,
    update_byte_cost: Callable[[int], int],
    node_cap: int = 200_000,
) -> Tuple[Tuple[Message, ...], int, bool]:
    """Search one receipt-delimited segment.

    Returns the interleaved segment messages (updates and barriers, no
    receipt), the total number of nodes explored and whether the node cap
    was reached (in which case the original segment is returned as a safe
    fallback).

    Iterative deepening over update length: layers L = 0, 1, ..., max_ops
    are searched in order, so the first layer containing a goal gives the
    minimum update count.  Within a layer every update has the same wire
    size under the declared format, and the heap orders ties by the
    operation-index tuple, satisfying the required same-length ordering.
    """
    plan = _build_plan(declaration, segment)
    n_barriers = len(plan.barriers)
    start = initial_state(declaration)
    total_nodes = 0

    for depth_limit in range(plan.max_ops + 1):
        counter = 0
        heap: List[_Node] = []
        heapq.heappush(heap, _Node(0, (), counter, start, 0, ()))
        visited = {(start.key(), 0)}
        layer_nodes = 0

        while heap:
            node = heapq.heappop(heap)
            layer_nodes += 1
            total_nodes += 1
            if total_nodes > node_cap:
                return tuple(segment.items), total_nodes, True

            # Goal: every barrier has been published and the state matches.
            if (
                node.consumed == n_barriers
                and node.state.pieces == plan.goal.pieces
            ):
                return (
                    _trace_to_messages(node.trace, plan.barriers),
                    total_nodes,
                    False,
                )

            # Publish the next pending barrier for free whenever its
            # observation still matches the original one.
            if node.consumed < n_barriers and plan.barrier_holds(
                node.state, node.consumed
            ):
                key = (node.state.key(), node.consumed + 1)
                if key not in visited:
                    visited.add(key)
                    counter += 1
                    heapq.heappush(
                        heap,
                        _Node(
                            node.cost,
                            node.seq,
                            counter,
                            node.state,
                            node.consumed + 1,
                            node.trace + (("b", node.consumed),),
                        ),
                    )

            # Append one declared operation, up to the current depth limit.
            if len(node.seq) < depth_limit:
                for op_index in range(len(declaration.operations)):
                    new_state = node.state.apply(
                        declaration.operations[op_index]
                    )
                    key = (new_state.key(), node.consumed)
                    if key in visited:
                        continue
                    visited.add(key)
                    counter += 1
                    heapq.heappush(
                        heap,
                        _Node(
                            node.cost + update_byte_cost(op_index),
                            node.seq + (op_index,),
                            counter,
                            new_state,
                            node.consumed,
                            node.trace + (("u", op_index),),
                        ),
                    )

    # The original sequence always reaches the goal, so this is unreachable
    # unless the node cap fired above.
    return tuple(segment.items), total_nodes, True


def _trace_to_messages(
    trace: Tuple[Tuple[str, int], ...], barriers: Tuple[Barrier, ...]
) -> Tuple[Message, ...]:
    messages: List[Message] = []
    for kind, value in trace:
        if kind == "u":
            messages.append(Update(value))
        else:
            messages.append(barriers[value])
    return tuple(messages)


def compress_stream(stream: Stream, node_cap: int = 200_000) -> CompressionResult:
    """Compress a full stream and re-emit it with receipts preserved."""
    from .encoding import encoded_length

    declaration = stream.declaration

    def update_byte_cost(op_index: int) -> int:
        # Under the declared wire format every update has the same body size;
        # the hook keeps cost ordering explicit and testable.
        return 1 + _unsigned_len(op_index)

    output_messages: List[Message] = []
    total_nodes = 0
    truncated = False
    for segment in split_segments(stream.messages):
        segment_messages, nodes, seg_truncated = compress_segment(
            declaration, segment, update_byte_cost, node_cap=node_cap
        )
        total_nodes += nodes
        truncated = truncated or seg_truncated
        output_messages.extend(segment_messages)
        if segment.terminal is not None and segment.terminal[0] == "receipt":
            output_messages.append(Receipt(segment.terminal[1]))
        # A "barrier-receipt" terminal needs no extra message: the receipt is
        # already carried by the last barrier inside segment_messages.

    result_stream = Stream(declaration, tuple(output_messages))
    return CompressionResult(
        stream=result_stream,
        encoded_bytes=encoded_length(result_stream),
        update_count=sum(
            1 for m in result_stream.messages if isinstance(m, Update)
        ),
        nodes_explored=total_nodes,
        truncated=truncated,
    )


def _unsigned_len(value: int) -> int:
    length = 1
    while value >= 128:
        value >>= 7
        length += 1
    return length
