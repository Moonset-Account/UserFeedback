"""Core implementation of the constrained differential encoder.

Algorithm overview
------------------

Let ``S`` be the old base (length *n*) and ``T`` the approved target (length
*m*).  A copy instruction emitted at target position *i* is valid only when its
source span ``[offset, offset + length)`` is fully contained in one public
interval *and* the corresponding bytes equal ``T[i:i+length]``.

1. For every target position ``i`` and every base offset ``o`` the longest
   common prefix of ``T[i:]`` and ``S[o:]`` is computed with a single backward
   dynamic programming pass (``O(n*m)`` time, ``O(m)`` memory).  Each LCP value
   is capped so that the matched source span never crosses the end of the
   public interval containing ``o``; offsets outside every public interval get
   length zero.  This enforces both the "fully inside one interval" rule and
   the "only the public instance may be referenced" rule.

2. Optimal parsing is a shortest-path problem on positions ``0..m``:

   * a literal run of length ``L`` from position ``i`` costs
     ``literal_head + L * literal_byte``;
   * a copy of length ``L`` costs ``copy_cost`` regardless of length.

   Because copy cost is fixed, at each position only the longest feasible copy
   is needed (it dominates every shorter copy starting at the same position).
   Copy transitions are range relaxations processed with a min-heap in
   ``O(m log m)`` overall; literal transitions use a running minimum in
   ``O(m)``.

The parser therefore finds the globally minimum total cost, not a greedy
approximation, and reconstructs one optimal instruction sequence.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "CopyOp",
    "LiteralOp",
    "Op",
    "Plan",
    "decode",
    "encode",
    "normalize_intervals",
    "plan_cost",
]


# ---------------------------------------------------------------------------
# Instruction data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CopyOp:
    """Copy ``length`` bytes from base offset ``offset`` into the target."""

    offset: int
    length: int

    @property
    def kind(self) -> str:
        return "copy"

    def to_jsonable(self) -> dict:
        return {"op": "copy", "offset": self.offset, "length": self.length}


@dataclass(frozen=True)
class LiteralOp:
    """Write the literal payload bytes directly to the target."""

    data: bytes

    @property
    def kind(self) -> str:
        return "literal"

    def to_jsonable(self) -> dict:
        return {"op": "literal", "data": self.data.hex()}


Op = object  # documentation alias; the real union is CopyOp | LiteralOp.


@dataclass(frozen=True)
class Plan:
    """An instruction sequence that rebuilds the target byte for byte."""

    ops: Tuple[Op, ...]
    cost: float
    source_length: int
    target_length: int
    public_intervals: Tuple[Tuple[int, int], ...]
    copy_cost: float
    literal_head: float
    literal_byte: float

    def rebuild(self) -> bytes:
        return decode(self)

    def to_jsonable(self) -> dict:
        return {
            "ops": [op.to_jsonable() for op in self.ops],
            "cost": self.cost,
            "source_length": self.source_length,
            "target_length": self.target_length,
            "public_intervals": [list(iv) for iv in self.public_intervals],
            "costs": {
                "copy": self.copy_cost,
                "literal_head": self.literal_head,
                "literal_byte": self.literal_byte,
            },
        }


# ---------------------------------------------------------------------------
# Public interval handling
# ---------------------------------------------------------------------------


def normalize_intervals(
    intervals: Iterable[Tuple[int, int]], base_length: int
) -> Tuple[Tuple[int, int], ...]:
    """Validate and normalize half-open intervals ``[start, end)``.

    Intervals are returned sorted.  Overlapping intervals are rejected: each
    copy must live inside exactly one interval, so merging would silently
    authorize cross-boundary spans.  Adjacent (touching but non-overlapping)
    intervals stay separate, which keeps the boundary itself enforced.
    """

    if base_length < 0:
        raise ValueError("base_length must be non-negative")

    cleaned: List[Tuple[int, int]] = []
    for pair in intervals:
        try:
            start, end = pair
        except (TypeError, ValueError) as exc:
            raise ValueError(f"interval must be a (start, end) pair: {pair!r}") from exc
        if not isinstance(start, int) or not isinstance(end, int):
            raise ValueError(f"interval bounds must be integers: {pair!r}")
        if start < 0 or end < 0:
            raise ValueError(f"interval bounds must be non-negative: {pair!r}")
        if start > end:
            raise ValueError(f"interval start must not exceed end: {pair!r}")
        if end > base_length:
            raise ValueError(
                f"interval {pair!r} exceeds base length {base_length}"
            )
        if start < end:  # empty intervals carry no authorization, drop them.
            cleaned.append((start, end))

    cleaned.sort()
    for (s1, e1), (s2, e2) in zip(cleaned, cleaned[1:]):
        if s2 < e1:
            raise ValueError(
                f"public intervals must not overlap: {(s1, e1)} vs {(s2, e2)}"
            )
    return tuple(cleaned)


def _interval_end_caps(
    intervals: Sequence[Tuple[int, int]], base_length: int
) -> List[int]:
    """For every base offset, the exclusive end of the interval containing it.

    Offsets outside every public interval get cap ``0``, meaning no copy may
    start there.
    """

    caps = [0] * base_length
    for start, end in intervals:
        for offset in range(start, end):
            caps[offset] = end
    return caps


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def encode(
    source: bytes,
    public_intervals: Iterable[Tuple[int, int]],
    target: bytes,
    copy_cost: float,
    literal_head: float,
    literal_byte: float = 1,
) -> Plan:
    """Build a minimum-cost instruction sequence rebuilding ``target``.

    Parameters
    ----------
    source:
        The old base package bytes.
    public_intervals:
        Half-open ``[start, end)`` byte ranges of the base that update
        packages are allowed to reference.
    target:
        The approved public target bytes.
    copy_cost:
        Fixed cost of every copy instruction.
    literal_head:
        Fixed header cost of every literal instruction.
    literal_byte:
        Per-byte cost added on top of the literal header.
    """

    if not isinstance(source, (bytes, bytearray, memoryview)):
        raise TypeError("source must be bytes-like")
    if not isinstance(target, (bytes, bytearray, memoryview)):
        raise TypeError("target must be bytes-like")
    source = bytes(source)
    target = bytes(target)
    intervals = normalize_intervals(public_intervals, len(source))

    for name, value in (
        ("copy_cost", copy_cost),
        ("literal_head", literal_head),
        ("literal_byte", literal_byte),
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError(f"{name} must be a number")
        if value < 0:
            raise ValueError(f"{name} must be non-negative")

    n = len(source)
    m = len(target)

    if m == 0:
        return Plan(
            ops=(),
            cost=0,
            source_length=n,
            target_length=0,
            public_intervals=intervals,
            copy_cost=copy_cost,
            literal_head=literal_head,
            literal_byte=literal_byte,
        )

    caps = _interval_end_caps(intervals, n)

    # Longest authorized match per target position, together with the source
    # offset realizing it.  Backward LCP DP over the (n x m) alignment grid,
    # each row discarded as soon as the next one has been computed.
    best_length = [0] * m
    best_offset = [-1] * m
    previous = [0] * (m + 1)
    for o in range(n - 1, -1, -1):
        cap = caps[o]
        current = [0] * (m + 1)
        if cap:  # offset inside a public interval
            src_byte = source[o]
            span = cap - o
            for i in range(m - 1, -1, -1):
                if src_byte == target[i]:
                    length = previous[i + 1] + 1
                    if length > span:
                        length = span
                    current[i] = length
                    if length > best_length[i]:
                        best_length[i] = length
                        best_offset[i] = o
        previous = current

    # --- shortest path over target positions -------------------------------
    infinity = float("inf")
    dp: List[float] = [infinity] * (m + 1)
    pred: List[Optional[Tuple[str, int, object]]] = [None] * (m + 1)
    dp[0] = 0

    # Heap entries for copy ranges opened at position i:
    # (arrival_cost, exclusive_end, start_position, source_offset).
    pending: List[Tuple[float, int, int, int]] = []

    # Running minimum supporting literal transitions of every length L >= 1:
    # dp[i] + literal_head - i*b, evaluated at target position j = i + L.
    lit_idx = -1
    lit_value = infinity

    for j in range(1, m + 1):
        i = j - 1

        # Open the longest copy available at i.  Its fixed cost dominates all
        # shorter copies starting at the same position.
        max_len = best_length[i]
        if max_len > 0:
            heapq.heappush(
                pending,
                (
                    dp[i] + copy_cost,
                    i + max_len,
                    i,
                    best_offset[i],
                ),
            )

        # Literal run ending at j (length 1 here; the running min covers all
        # earlier start positions as well).
        candidate = dp[i] + literal_head - i * literal_byte
        if candidate < lit_value:
            lit_value = candidate
            lit_idx = i

        literal_candidate = lit_value + j * literal_byte
        if literal_candidate < dp[j]:
            dp[j] = literal_candidate
            pred[j] = ("literal", lit_idx, None)

        while pending and pending[0][1] < j:
            heapq.heappop(pending)
        if pending:
            arrival, _end, start, offset = pending[0]
            # Strict comparison keeps parsing deterministic: on equal cost the
            # literal transition chosen above wins.
            if arrival < dp[j]:
                dp[j] = arrival
                pred[j] = ("copy", start, offset)

    # --- reconstruct instructions ------------------------------------------
    ops: List[Op] = []
    position = m
    while position > 0:
        entry = pred[position]
        if entry is None:  # pragma: no cover - literals always reach every j.
            raise RuntimeError("internal error: unreachable target position")
        kind, start, payload = entry
        if kind == "copy":
            offset = payload
            ops.append(CopyOp(offset=offset, length=position - start))
        else:
            ops.append(LiteralOp(data=bytes(target[start:position])))
        position = start
    ops.reverse()

    return Plan(
        ops=tuple(ops),
        cost=dp[m],
        source_length=n,
        target_length=m,
        public_intervals=intervals,
        copy_cost=copy_cost,
        literal_head=literal_head,
        literal_byte=literal_byte,
    )


# ---------------------------------------------------------------------------
# Decoding / verification
# ---------------------------------------------------------------------------


def decode(
    plan: Plan,
    source: Optional[bytes] = None,
    public_intervals: Optional[Iterable[Tuple[int, int]]] = None,
) -> bytes:
    """Rebuild target bytes from a plan.

    When ``source`` and ``public_intervals`` are supplied every copy is
    re-validated against the authorization rules; a :class:`ValueError` is
    raised for any instruction that references bytes outside a single public
    interval.
    """

    if source is not None:
        source = bytes(source)
        intervals = (
            normalize_intervals(public_intervals, len(source))
            if public_intervals is not None
            else plan.public_intervals
        )
        caps = _interval_end_caps(intervals, len(source))
    else:
        caps = None

    chunks: List[bytes] = []
    cursor = 0
    for op in plan.ops:
        if isinstance(op, LiteralOp):
            if not isinstance(op.data, bytes):
                raise ValueError(f"literal payload must be bytes, got {type(op.data)!r}")
            chunks.append(op.data)
            cursor += len(op.data)
        elif isinstance(op, CopyOp):
            if op.offset < 0 or op.length <= 0:
                raise ValueError(f"invalid copy instruction: {op!r}")
            end = op.offset + op.length
            if source is None:
                raise ValueError("a source is required to execute copy instructions")
            if end > len(source):
                raise ValueError(f"copy {op!r} runs past the end of the source")
            if caps is not None:
                cap = caps[op.offset]
                if cap == 0 or end > cap:
                    raise ValueError(
                        f"copy {op!r} is not fully contained in one public interval"
                    )
            chunks.append(source[op.offset:end])
            cursor += op.length
        else:  # pragma: no cover - dataclass construction prevents this.
            raise ValueError(f"unknown instruction: {op!r}")

    rebuilt = b"".join(chunks)
    if cursor != len(rebuilt) or cursor != plan.target_length:
        raise ValueError(
            f"plan rebuilds {len(rebuilt)} bytes, target declares {plan.target_length}"
        )
    return rebuilt


def plan_cost(
    ops: Sequence[Op],
    copy_cost: float,
    literal_head: float,
    literal_byte: float = 1,
) -> float:
    """Compute the total cost of an instruction sequence."""

    total = 0
    for op in ops:
        if isinstance(op, CopyOp):
            total += copy_cost
        elif isinstance(op, LiteralOp):
            total += literal_head + len(op.data) * literal_byte
        else:
            raise ValueError(f"unknown instruction: {op!r}")
    return total
