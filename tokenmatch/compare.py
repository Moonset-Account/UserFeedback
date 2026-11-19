"""Scope-aware, rename-tolerant contiguous token matching.

Given two ordered, annotated token streams, find every maximal contiguous
match of at least ``min_length`` token pairs under these rules:

* Scope enter/exit tokens match when their scope flavors match.
* Local names (binds and references) match when they form a consistent
  one-to-one (bijection) relation between the two input binding numberings.
  Every use inside a match must keep pointing at the corresponding
  declaration; declarations outside the candidate fragment are still checked
  by their input binding number.
* External names match by original spelling.
* Literals match by value.
* Approved public boilerplate intervals sever matches: boilerplate tokens can
  never belong to a match, so two real fragments cannot be joined through a
  boilerplate stretch to reach the length threshold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque

from .tokens import (
    Token,
    LITERAL,
    NAME_BIND,
    NAME_REF,
    SCOPE_ENTER,
    SCOPE_EXIT,
)

__all__ = ["Match", "Boilerplate", "compare_modules", "compatible"]


@dataclass(frozen=True)
class Boilerplate:
    """Approved public-boilerplate interval on one side.

    Bounds are half-open ``[start, end)`` token indices.
    """

    side: str  # "left" or "right"
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.side not in ("left", "right"):
            raise ValueError("boilerplate side must be 'left' or 'right'")
        if self.start < 0 or self.end < self.start:
            raise ValueError("invalid boilerplate interval")


@dataclass(frozen=True)
class Match:
    """One retained maximal contiguous match.

    Intervals are half-open token index ranges on each side; both spans have
    ``length`` tokens.  ``bindings`` is the local binding bijection recorded
    in the order the pairs were first encountered:
    ``{left_binding: right_binding}``.
    """

    left_start: int
    left_end: int
    right_start: int
    right_end: int
    length: int
    bindings: dict[int, int] = field(default_factory=dict)

    def pairs(self) -> list[tuple[int, int]]:
        return [
            (self.left_start + k, self.right_start + k)
            for k in range(self.length)
        ]


def _mark_boilerplate(tokens: list[Token], spans: list[Boilerplate]) -> list[bool]:
    """Return a per-token boolean flag from half-open boilerplate spans."""
    n = len(tokens)
    diff = [0] * (n + 1)
    for span in spans:
        lo = max(0, span.start)
        hi = min(n, span.end)
        if lo < hi:
            diff[lo] += 1
            diff[hi] -= 1
    marked = [False] * n
    coverage = 0
    for i in range(n):
        coverage += diff[i]
        marked[i] = coverage > 0
    return marked


def compatible(left: Token, right: Token) -> tuple[bool, bool]:
    """Check the local, per-pair compatibility rules.

    Returns ``(compatible, is_local_name_pair)``.  The bijection consistency
    across a whole run is enforced by :func:`compare_modules`; this function
    only checks kinds, scope flavors, spelling and literal values.
    """
    if left.kind != right.kind:
        return False, False

    kind = left.kind
    if kind == LITERAL:
        return left.value == right.value, False
    if kind in (SCOPE_ENTER, SCOPE_EXIT):
        return left.scope_kind == right.scope_kind, False
    if kind in (NAME_BIND, NAME_REF):
        if left.external or right.external:
            # External names match both sides externally, by spelling.
            return (
                left.external
                and right.external
                and left.text == right.text
            ), False
        if left.binding is None or right.binding is None:
            return False, False
        return True, True
    raise ValueError(f"unsupported token kind: {kind!r}")


def compare_modules(
    left: list[Token],
    right: list[Token],
    min_length: int = 1,
    boilerplate: list[Boilerplate] | None = None,
) -> list[Match]:
    """Find all maximal contiguous matches reaching ``min_length``.

    The result is sorted by left interval start, then right interval start.
    Boilerplate intervals break runs on either side, and local names must
    induce a single one-to-one binding mapping inside each match.
    """
    if min_length < 1:
        raise ValueError("min_length must be >= 1")
    boilerplate = list(boilerplate or [])
    left_boiler = _mark_boilerplate(
        left, [b for b in boilerplate if b.side == "left"]
    )
    right_boiler = _mark_boilerplate(
        right, [b for b in boilerplate if b.side == "right"]
    )

    n_left, n_right = len(left), len(right)
    results: list[Match] = []

    def emit(run_start_i: int, i: int, run_start_j: int, j: int,
             mapping: dict[int, int]) -> None:
        length = i - run_start_i
        if length >= min_length:
            results.append(
                Match(
                    left_start=run_start_i,
                    left_end=i,
                    right_start=run_start_j,
                    right_end=j,
                    length=length,
                    bindings=dict(mapping),
                )
            )

    # A contiguous match lies on one anti-diagonal (i - j constant).
    diagonals = list(range(1 - n_right, n_left))
    for diag in diagonals:
        if diag >= 0:
            start_i, start_j = diag, 0
        else:
            start_i, start_j = 0, -diag

        # Current run state for this diagonal.
        run_start_i = -1
        run_start_j = -1
        l_to_r: dict[int, int] = {}
        r_to_l: dict[int, int] = {}
        order: deque[tuple[int, int, int]] = deque()  # (left_idx, lb, rb)

        i, j = start_i, start_j
        while i < n_left and j < n_right:
            is_pair, is_local = compatible(left[i], right[j])
            severed = left_boiler[i] or right_boiler[j]

            if not is_pair or severed:
                # Close any run ending before this token.
                if run_start_i >= 0:
                    emit(run_start_i, i, run_start_j, j, l_to_r)
                    run_start_i = -1
                    run_start_j = -1
                    l_to_r = {}
                    r_to_l = {}
                    order.clear()
                i += 1
                j += 1
                continue

            if run_start_i < 0:
                run_start_i, run_start_j = i, j

            if not is_local:
                i += 1
                j += 1
                continue

            lb = left[i].binding
            rb = right[j].binding
            assert lb is not None and rb is not None
            mapped_rb = l_to_r.get(lb)
            mapped_lb = r_to_l.get(rb)

            conflict = (mapped_rb is not None and mapped_rb != rb) or (
                mapped_lb is not None and mapped_lb != lb
            )

            if conflict:
                # The prefix up to this pair is itself a maximal valid run;
                # emit it if it reaches the threshold, then restart after the
                # most recent occurrence of either conflicting binding so the
                # remaining prefix is conflict-free.
                emit(run_start_i, i, run_start_j, j, l_to_r)

                conflict_pos = run_start_i
                for (pos, old_lb, old_rb) in order:
                    if old_lb == lb or old_rb == rb:
                        conflict_pos = max(conflict_pos, pos + 1)

                # Drop every binding pair introduced before conflict_pos,
                # then rebuild both mapping directions from the surviving
                # pairs so stale entries after the cut are removed as well.
                while order and order[0][0] < conflict_pos:
                    order.popleft()
                l_to_r = {old_lb: old_rb for (_, old_lb, old_rb) in order}
                r_to_l = {old_rb: old_lb for (_, old_lb, old_rb) in order}

                run_start_i = conflict_pos
                run_start_j = conflict_pos - diag

            # Register the current pair (first sight or consistent reuse).
            if l_to_r.get(lb) != rb:
                l_to_r[lb] = rb
                r_to_l[rb] = lb
                order.append((i, lb, rb))

            i += 1
            j += 1

        if run_start_i >= 0:
            # i / j hold the diagonal's end position at loop exit.
            emit(run_start_i, i, run_start_j, j, l_to_r)

    results.sort(key=lambda m: (m.left_start, m.right_start))
    return results
