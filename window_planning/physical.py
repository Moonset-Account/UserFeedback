"""Physical keys, orderings and measured stable sorts.

A *physical key* is the concrete tuple of key components describing one total
(or partial) ordering of the table:

* partition components are normalised to ``ASC NULLS LAST`` and may appear in
  any permutation, because partitions only need equal values to be contiguous;
* ordering components keep the window's declared direction and NULL position;
* an optional row-number component (:data:`~window_planning.spec.ROWNO_COLUMN`)
  acts as a physical tie-breaking tail. It may break physical ties but can
  never split a logical equal-value group, because it always comes last.

Comparisons executed by Python's stable Timsort are counted with a
``cmp_to_key`` wrapper, so the fee table is charged against the *real* number
of comparisons rather than a textbook estimate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import cmp_to_key
from typing import Iterable

from .spec import (
    ROWNO_COLUMN,
    Direction,
    Nulls,
    OrderKey,
    TiePolicy,
    WindowSpec,
)


class KeyRole(Enum):
    PARTITION = "PARTITION"
    ORDER = "ORDER"
    ROWNO_TAIL = "ROWNO_TAIL"


@dataclass(frozen=True)
class PhysKeyPart:
    """One component of a physical ordering key."""

    column: str
    direction: Direction
    nulls: Nulls
    role: KeyRole

    def describe(self) -> str:
        if self.role is KeyRole.ROWNO_TAIL:
            return f"{self.column}(rowno-tail)"
        prefix = "P:" if self.role is KeyRole.PARTITION else "O:"
        return f"{prefix}{self.column} {self.direction.value} NULLS {self.nulls.value}"


#: Canonical convention for partition components and row-number tails.
_CANONICAL_DIRECTION = Direction.ASC
_CANONICAL_NULLS = Nulls.LAST


def partition_part(column: str) -> PhysKeyPart:
    return PhysKeyPart(column, _CANONICAL_DIRECTION, _CANONICAL_NULLS, KeyRole.PARTITION)


def order_part(key: OrderKey) -> PhysKeyPart:
    return PhysKeyPart(key.column, key.direction, key.nulls, KeyRole.ORDER)


def rowno_part() -> PhysKeyPart:
    return PhysKeyPart(
        ROWNO_COLUMN, _CANONICAL_DIRECTION, _CANONICAL_NULLS, KeyRole.ROWNO_TAIL
    )


@dataclass(frozen=True)
class PhysKey:
    """A concrete physical ordering key (ordered tuple of components)."""

    parts: tuple[PhysKeyPart, ...]

    def describe(self) -> str:
        return " | ".join(p.describe() for p in self.parts)

    def columns(self) -> tuple[str, ...]:
        return tuple(p.column for p in self.parts)

    def logical_columns(self) -> tuple[str, ...]:
        """Columns excluding a trailing row-number tail."""
        return tuple(p.column for p in self.parts if p.role is not KeyRole.ROWNO_TAIL)


#: The physical key describing the untouched input order (original row number).
INITIAL_KEY = PhysKey((rowno_part(),))


# --------------------------------------------------------------------------- #
# Value-level comparator
# --------------------------------------------------------------------------- #


class _MixedTypeError(TypeError):
    """Raised when two non-NULL values of unrelated types must be ordered."""


def _compare_values(a: object, b: object) -> int:
    """Compare two non-NULL values, rejecting genuinely mixed types."""
    if type(a) is type(b):
        if a < b:
            return -1
        if a > b:
            return 1
        return 0
    # Booleans are ints in Python; allow that narrow numeric relationship.
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a < b:
            return -1
        if a > b:
            return 1
        return 0
    raise _MixedTypeError(
        f"cannot order values of unrelated types: {type(a).__name__} vs "
        f"{type(b).__name__}"
    )


def make_comparator(rows: list[dict], key: PhysKey, counter: list[int]):
    """Build a cmp-style comparator over row indices, counting each invocation.

    Every call to the returned function is exactly one Timsort comparison, so
    incrementing ``counter[0]`` here gives the true comparison count.
    """

    def compare(i: int, j: int) -> int:
        counter[0] += 1
        ra, rb = rows[i], rows[j]
        for part in key.parts:
            a = ra[part.column]
            b = rb[part.column]
            if a is None or b is None:
                if a is None and b is None:
                    continue
                null_first = part.nulls is Nulls.FIRST
                # a is NULL, b is not: -1 when NULLs FIRST, else 1.
                if a is None:
                    return -1 if null_first else 1
                return 1 if null_first else -1
            sign = _compare_values(a, b)
            if sign:
                return -sign if part.direction is Direction.DESC else sign
        return 0

    return compare


# --------------------------------------------------------------------------- #
# Measured stable sorts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SortMeasurement:
    """The result of one measured stable sort operation."""

    permutation: tuple[int, ...]
    comparisons: int
    group_sizes: tuple[int, ...]


def measured_stable_sort(
    rows: list[dict], permutation: tuple[int, ...], key: PhysKey
) -> SortMeasurement:
    """Stable-sort one group of rows under ``key`` and count comparisons.

    Python's sort is stable, so rows equal on every key component retain their
    relative order from ``permutation``.
    """
    counter = [0]
    comparator = make_comparator(rows, key, counter)
    ordered = sorted(permutation, key=cmp_to_key(comparator))
    return SortMeasurement(tuple(ordered), counter[0], (len(permutation),))


def _partition_groups(
    rows: list[dict], permutation: tuple[int, ...], partition_cols: tuple[str, ...]
) -> list[tuple[int, ...]]:
    """Split a permutation into maximal contiguous groups equal on the columns."""
    groups: list[list[int]] = []
    signature: tuple[object, ...] | None = None
    for idx in permutation:
        sig = tuple(rows[idx][c] for c in partition_cols)
        if not groups or sig != signature:
            groups.append([idx])
            signature = sig
        else:
            groups[-1].append(idx)
    return [tuple(g) for g in groups]


def measured_partial_sort(
    rows: list[dict],
    permutation: tuple[int, ...],
    prefix_cols: tuple[str, ...],
    sort_key: PhysKey,
) -> SortMeasurement:
    """Stable-sort inside each contiguous prefix group separately.

    The existing order is reused at the group level: groups stay exactly where
    they are and only rows *within* each group are re-sorted. Comparison counts
    of the independent per-group sorts are summed, matching a real executor
    that never compares across partition boundaries.
    """
    groups = _partition_groups(rows, permutation, prefix_cols)
    new_perm: list[int] = []
    total_comparisons = 0
    sizes: list[int] = []
    for group in groups:
        if len(group) > 1:
            measurement = measured_stable_sort(rows, group, sort_key)
            new_perm.extend(measurement.permutation)
            total_comparisons += measurement.comparisons
            sizes.append(len(group))
        else:
            new_perm.extend(group)
    return SortMeasurement(tuple(new_perm), total_comparisons, tuple(sizes))


# --------------------------------------------------------------------------- #
# Candidate physical keys and reuse predicates
# --------------------------------------------------------------------------- #


def _permutations(items: tuple[str, ...]) -> list[tuple[str, ...]]:
    if len(items) <= 1:
        return [items]
    out: list[tuple[str, ...]] = []
    for pos, column in enumerate(items):
        rest = items[:pos] + items[pos + 1 :]
        for tail in _permutations(rest):
            out.append((column,) + tail)
    return out


def candidate_keys(spec: WindowSpec) -> list[PhysKey]:
    """All physical keys a full/partial sort for this window may use.

    * partition columns may appear in any permutation;
    * ordering components follow in declared order with their declared
      direction / NULL semantics;
    * ``ROW_NUMBER`` policy always carries an explicit row-number tail,
      guaranteeing a deterministic total order;
    * ``GROUP`` policy may additionally carry that tail, which never splits a
      logical group (it is strictly last).
    """
    keys: list[PhysKey] = []
    for perm in _permutations(tuple(spec.partition_by)):
        parts = [partition_part(c) for c in perm]
        parts.extend(order_part(k) for k in spec.order_by)
        if spec.tie_policy is TiePolicy.ROW_NUMBER:
            keys.append(PhysKey(tuple(parts + [rowno_part()])))
        else:
            keys.append(PhysKey(tuple(parts)))
            keys.append(PhysKey(tuple(parts + [rowno_part()])))
    return keys


def partition_prefix_match(current: PhysKey, spec: WindowSpec) -> bool:
    """True iff the first key components cover exactly the partition columns.

    The components must form a contiguous prefix whose column multiset equals
    the window's partition set. Their order is irrelevant because partition
    columns are interchangeable at the physical level.
    """
    wanted = tuple(spec.partition_by)
    if len(current.parts) < len(wanted):
        return False
    prefix = current.parts[: len(wanted)]
    if any(p.role is not KeyRole.PARTITION for p in prefix):
        return False
    return sorted(p.column for p in prefix) == sorted(wanted)


def order_suffix_match(current: PhysKey, spec: WindowSpec) -> bool:
    """True iff the components after the partition prefix match the order keys.

    Direction and NULL position must match component by component. A trailing
    row-number part, when present, is ignored for the logical comparison.
    """
    p_len = len(spec.partition_by)
    suffix = list(current.parts[p_len:])
    if suffix and suffix[-1].role is KeyRole.ROWNO_TAIL:
        suffix.pop()
    wanted = list(spec.order_by)
    if len(suffix) != len(wanted):
        return False
    for part, order_key in zip(suffix, wanted):
        if part.role is not KeyRole.ORDER:
            return False
        if part.column != order_key.column:
            return False
        if part.direction != order_key.direction:
            return False
        if part.nulls != order_key.nulls:
            return False
    return True


def can_reuse(current: PhysKey, spec: WindowSpec) -> bool:
    """True iff the current physical ordering already serves this window.

    * the partition columns form a contiguous prefix (any permutation);
    * the ordering suffix matches direction and NULL semantics;
    * ``ROW_NUMBER`` windows additionally need a row-number tail so that the
      total order is deterministic;
    * ``GROUP`` windows ignore any extra tail because the row-number tail
      cannot split a logical equal-value group.
    """
    if not partition_prefix_match(current, spec):
        return False
    if not order_suffix_match(current, spec):
        return False
    if spec.tie_policy is TiePolicy.ROW_NUMBER:
        p_len = len(spec.partition_by)
        o_len = len(spec.order_by)
        tail_index = p_len + o_len
        if tail_index >= len(current.parts):
            return False
        tail = current.parts[tail_index]
        if tail.role is not KeyRole.ROWNO_TAIL:
            return False
    return True


def partial_sort_key(current: PhysKey, spec: WindowSpec) -> PhysKey:
    """Build the sort key for an in-prefix-group partial sort.

    The partition prefix is kept in its *current* physical permutation (the
    reused ordering basis), followed by this window's ordering components and
    -- for ``ROW_NUMBER`` policy -- the row-number tail.
    """
    p_len = len(spec.partition_by)
    parts = list(current.parts[:p_len])
    parts.extend(order_part(k) for k in spec.order_by)
    if spec.tie_policy is TiePolicy.ROW_NUMBER:
        parts.append(rowno_part())
    return PhysKey(tuple(parts))


def iter_physkeys(keys: Iterable[PhysKey]) -> Iterable[PhysKey]:
    """Deduplicate identical physical keys while preserving order."""
    seen: set[PhysKey] = set()
    for key in keys:
        if key not in seen:
            seen.add(key)
            yield key
