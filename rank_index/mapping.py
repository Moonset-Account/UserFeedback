"""Compressed, arbitrary-precision mappings between committed rank spaces."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Any, Hashable

from .model import AddedRankError, CopyNotFoundError, DeletedRankError
from .snapshot import Snapshot


def _prod(xs: tuple[int, ...]) -> int:
    out = 1
    for x in xs:
        out *= x
    return out


def decode_layout_rank(local_rank: int, dims: tuple[int, int, int]) -> tuple[int, int, int]:
    d0, d1, d2 = dims
    slab = d1 * d2
    i = local_rank // slab
    rem = local_rank - i * slab
    j = rem // d2
    k = rem - j * d2
    return i, j, k


def encode_layout_rank(coords: tuple[int, int, int], dims: tuple[int, int, int]) -> int:
    i, j, k = coords
    _d0, d1, d2 = dims
    return i * d1 * d2 + j * d2 + k


def bad_run_interval(
    local_rank: int,
    layout_dims: tuple[int, int, int],
    retained_bounds: tuple[int, int, int],
) -> tuple[int, int]:
    """Maximal local interval containing a rank outside retained bounds."""
    o0, o1, o2 = layout_dims
    n0, n1, n2 = retained_bounds
    i, j, k = decode_layout_rank(local_rank, layout_dims)
    slab = o1 * o2

    if i >= n0:
        return n0 * slab, o0 * slab

    base_i = i * slab
    if j >= n1:
        lo = base_i + n1 * o2
        hi = base_i + slab
        if i == n0 - 1:
            hi = o0 * slab
        return lo, hi

    # Otherwise k >= n2, because the caller says the rank is bad.
    lo = base_i + j * o2 + n2
    hi = base_i + (j + 1) * o2
    if j == n1 - 1:
        hi = base_i + slab
        if i == n0 - 1:
            hi = o0 * slab
    return lo, hi


@dataclass(frozen=True, slots=True)
class IntervalPattern:
    """A compact family of repeated half-open integer intervals."""

    first_start: int
    length: int
    repeats: tuple[tuple[int, int], ...] = ()

    @property
    def interval_count(self) -> int:
        out = 1
        for count, _stride in self.repeats:
            out *= count
        return out

    @property
    def copied_rank_count(self) -> int:
        return self.length * self.interval_count

    def iter_intervals(self) -> Iterator[tuple[int, int]]:
        def rec(level: int, start: int) -> Iterator[tuple[int, int]]:
            if level == len(self.repeats):
                yield start, start + self.length
                return
            count, stride = self.repeats[level]
            for i in range(count):
                yield from rec(level + 1, start + i * stride)

        yield from rec(0, self.first_start)


@dataclass(frozen=True, slots=True)
class RangeCollection:
    """Eager compressed interval families."""

    patterns: tuple[IntervalPattern, ...] = ()

    @property
    def rank_count(self) -> int:
        return sum(p.copied_rank_count for p in self.patterns)

    def __iter__(self) -> Iterator[tuple[int, int]]:
        for pattern in self.patterns:
            yield from pattern.iter_intervals()

    def intervals(self, max_intervals: int | None = None) -> tuple[tuple[int, int], ...]:
        out: list[tuple[int, int]] = []
        for interval in self:
            out.append(interval)
            if max_intervals is not None and len(out) >= max_intervals:
                break
        return tuple(out)

    def merged_intervals(self) -> tuple[tuple[int, int], ...]:
        ordered = sorted(self)
        merged: list[list[int]] = []
        for start, stop in ordered:
            if not merged or start > merged[-1][1]:
                merged.append([start, stop])
            elif stop > merged[-1][1]:
                merged[-1][1] = stop
        return tuple((start, stop) for start, stop in merged)

    def __bool__(self) -> bool:
        return bool(self.patterns)


class LazyRangeSet:
    """Interval set whose pattern families are expanded only on request.

    Commit mapping construction does not enumerate row-id triples merely to
    build this object.  Calling :meth:`merged_intervals` can enumerate the
    physical row-id triples in affected groups; it never enumerates replica
    ranks because each multiplicity change remains an interval pattern.
    """

    def __init__(
        self,
        patterns_provider: Callable[[], Iterator[IntervalPattern]],
        container: Callable[[int], tuple[int, int] | None],
    ) -> None:
        self._patterns_provider = patterns_provider
        self._container = container

    def iter_patterns(self) -> Iterator[IntervalPattern]:
        yield from self._patterns_provider()

    def __iter__(self) -> Iterator[tuple[int, int]]:
        for pattern in self.iter_patterns():
            yield from pattern.iter_intervals()

    def containing(self, rank: int) -> tuple[int, int] | None:
        return self._container(rank)

    def intervals(self, max_intervals: int | None = None) -> tuple[tuple[int, int], ...]:
        out: list[tuple[int, int]] = []
        for interval in self:
            out.append(interval)
            if max_intervals is not None and len(out) >= max_intervals:
                break
        return tuple(out)

    def merged_intervals(self) -> tuple[tuple[int, int], ...]:
        ordered = sorted(self)
        merged: list[list[int]] = []
        for start, stop in ordered:
            if not merged or start > merged[-1][1]:
                merged.append([start, stop])
            elif stop > merged[-1][1]:
                merged[-1][1] = stop
        return tuple((start, stop) for start, stop in merged)


@dataclass(frozen=True, slots=True)
class AffineSegment:
    """A contiguous old interval mapping by a constant rank offset."""

    old_start: int
    old_stop: int
    new_start: int
    new_stop: int

    @property
    def length(self) -> int:
        return self.old_stop - self.old_start

    @property
    def offset(self) -> int:
        return self.new_start - self.old_start

    def contains_old(self, rank: int) -> bool:
        return self.old_start <= rank < self.old_stop

    def contains_new(self, rank: int) -> bool:
        return self.new_start <= rank < self.new_stop

    def forward(self, rank: int) -> int:
        return self.new_start + (rank - self.old_start)

    def reverse(self, rank: int) -> int:
        return self.old_start + (rank - self.new_start)


@dataclass(frozen=True, slots=True)
class BlockMapping:
    """Compressed mapping for one stable ``(R#,S#,T#)`` row-id triple."""

    key: tuple[Any, Any, Any]
    triple: tuple[Hashable, Hashable, Hashable]
    old_start: int | None
    old_dims: tuple[int, int, int] | None
    new_start: int | None
    new_dims: tuple[int, int, int] | None

    @property
    def old_stop(self) -> int | None:
        if self.old_start is None or self.old_dims is None:
            return None
        return self.old_start + _prod(self.old_dims)

    @property
    def new_stop(self) -> int | None:
        if self.new_start is None or self.new_dims is None:
            return None
        return self.new_start + _prod(self.new_dims)

    def deleted_patterns(self) -> tuple[IntervalPattern, ...]:
        if self.old_start is None or self.old_dims is None:
            return ()
        o0, o1, o2 = self.old_dims
        if self.new_dims is None:
            return (IntervalPattern(self.old_start, _prod(self.old_dims)),)
        n0, n1, n2 = self.new_dims
        patterns: list[IntervalPattern] = []

        if o0 > n0:
            patterns.append(
                IntervalPattern(self.old_start + n0 * o1 * o2, (o0 - n0) * o1 * o2)
            )
        if o1 > n1:
            patterns.append(
                IntervalPattern(
                    self.old_start + n1 * o2,
                    (o1 - n1) * o2,
                    ((min(o0, n0), o1 * o2),),
                )
            )
        if o2 > n2:
            patterns.append(
                IntervalPattern(
                    self.old_start + n2,
                    o2 - n2,
                    (
                        (min(o0, n0), o1 * o2),
                        (min(o1, n1), o2),
                    ),
                )
            )
        return tuple(patterns)

    def added_patterns(self) -> tuple[IntervalPattern, ...]:
        if self.new_start is None or self.new_dims is None:
            return ()
        n0, n1, n2 = self.new_dims
        if self.old_dims is None:
            return (IntervalPattern(self.new_start, _prod(self.new_dims)),)
        o0, o1, o2 = self.old_dims
        patterns: list[IntervalPattern] = []
        if n0 > o0:
            patterns.append(
                IntervalPattern(self.new_start + o0 * n1 * n2, (n0 - o0) * n1 * n2)
            )
        if n1 > o1:
            patterns.append(
                IntervalPattern(
                    self.new_start + o1 * n2,
                    (n1 - o1) * n2,
                    ((min(n0, o0), n1 * n2),),
                )
            )
        if n2 > o2:
            patterns.append(
                IntervalPattern(
                    self.new_start + o2,
                    n2 - o2,
                    (
                        (min(n0, o0), n1 * n2),
                        (min(n1, o1), n2),
                    ),
                )
            )
        return tuple(patterns)

    def iter_retained_runs(self) -> Iterator[tuple[int, int, int, int]]:
        if self.old_dims is None or self.new_dims is None:
            return
        assert self.old_start is not None and self.new_start is not None
        o0, o1, o2 = self.old_dims
        n0, n1, n2 = self.new_dims
        k0, k1, k2 = min(o0, n0), min(o1, n1), min(o2, n2)
        if k0 == 0 or k1 == 0 or k2 == 0:
            return

        if o2 == n2 and o1 == n1:
            old_len = k0 * o1 * o2
            new_len = k0 * n1 * n2
            yield self.old_start, self.old_start + old_len, self.new_start, self.new_start + new_len
        elif o2 == n2:
            for i in range(k0):
                old_s = self.old_start + i * o1 * o2
                new_s = self.new_start + i * n1 * n2
                yield old_s, old_s + k1 * o2, new_s, new_s + k1 * n2
        else:
            for i in range(k0):
                for j in range(k1):
                    old_s = self.old_start + i * o1 * o2 + j * o2
                    new_s = self.new_start + i * n1 * n2 + j * n2
                    yield old_s, old_s + k2, new_s, new_s + k2


@dataclass(frozen=True, slots=True)
class RankTranslation:
    status: str
    old_rank: int | None = None
    new_rank: int | None = None
    old_interval: tuple[int, int] | None = None
    new_interval: tuple[int, int] | None = None

    @property
    def retained(self) -> bool:
        return self.status == "retained"

    @property
    def deleted(self) -> bool:
        return self.status == "deleted"

    @property
    def added(self) -> bool:
        return self.status == "added"


def _common_retained_mass(old_group, new_group) -> int:
    """Factorized number of retained replica copies in one key group."""

    def common_mass(old_ids, old_m, old_pos, new_ids, new_m, new_pos):
        total = 0
        for row_id, op in old_pos.items():
            np = new_pos.get(row_id)
            if np is not None:
                total += min(old_m[op], new_m[np])
        return total

    r = common_mass(
        old_group.r_ids, old_group.r_m, old_group.r_pos,
        new_group.r_ids, new_group.r_m, new_group.r_pos,
    )
    s = common_mass(
        old_group.s_ids, old_group.s_m, old_group.s_pos,
        new_group.s_ids, new_group.s_m, new_group.s_pos,
    )
    t = common_mass(
        old_group.t_ids, old_group.t_m, old_group.t_pos,
        new_group.t_ids, new_group.t_m, new_group.t_pos,
    )
    return r * s * t


def _iter_block_mappings(
    old: Snapshot, new: Snapshot, keys: tuple[tuple[Any, Any, Any], ...]
) -> Iterator[BlockMapping]:
    for key in keys:
        og = old.group(key)
        ng = new.group(key)
        old_base = old.rank_before_key(key)
        new_base = new.rank_before_key(key)

        if og is not None and ng is None:
            yield BlockMapping(
                key, ("*", "*", "*"), old_base, (1, 1, og.total), None, None
            )
            continue
        if og is None and ng is not None:
            yield BlockMapping(
                key, ("*", "*", "*"), None, None, new_base, (1, 1, ng.total)
            )
            continue
        if og is None or ng is None:
            continue

        old_blocks = {triple: (start, dims) for triple, start, dims in og.iter_blocks()}
        new_blocks = {triple: (start, dims) for triple, start, dims in ng.iter_blocks()}
        for triple in sorted(set(old_blocks) | set(new_blocks)):
            ob = old_blocks.get(triple)
            nb = new_blocks.get(triple)
            yield BlockMapping(
                key=key,
                triple=triple,  # type: ignore[arg-type]
                old_start=old_base + ob[0] if ob else None,
                old_dims=ob[1] if ob else None,
                new_start=new_base + nb[0] if nb else None,
                new_dims=nb[1] if nb else None,
            )


def _iter_side_patterns(
    old: Snapshot,
    new: Snapshot,
    keys: tuple[tuple[Any, Any, Any], ...],
    side: str,
) -> Iterator[IntervalPattern]:
    for block in _iter_block_mappings(old, new, keys):
        if side == "old":
            yield from block.deleted_patterns()
        else:
            yield from block.added_patterns()


def find_deleted_interval(old: Snapshot, new: Snapshot, old_rank: int) -> tuple[int, int]:
    group, local, start = old.group_for_rank(old_rank)
    key = group.key
    new_group = new.group(key)
    if new_group is None:
        return start, start + group.total

    copy = group.copy_at(local)
    try:
        ri = group.r_pos[copy.r_row]
        sj = group.s_pos[copy.s_row]
        tj = group.t_pos[copy.t_row]
    except KeyError:  # defensive: copy came from this group
        return start, start + group.total

    old_dims = (group.r_m[ri], group.s_m[sj], group.t_m[tj])
    try:
        nri = new_group.r_pos[copy.r_row]
        nsj = new_group.s_pos[copy.s_row]
        ntj = new_group.t_pos[copy.t_row]
    except KeyError:
        first = replace(copy, r_rep=1, s_rep=1, t_rep=1)
        block_start = start + group.rank_of_copy(first)
        return block_start, block_start + _prod(old_dims)

    new_dims = (new_group.r_m[nri], new_group.s_m[nsj], new_group.t_m[ntj])
    first = replace(copy, r_rep=1, s_rep=1, t_rep=1)
    block_start = start + group.rank_of_copy(first)
    lo, hi = bad_run_interval(old_rank - block_start, old_dims, new_dims)
    return block_start + lo, block_start + hi


def find_added_interval(new: Snapshot, old: Snapshot, new_rank: int) -> tuple[int, int]:
    group, local, start = new.group_for_rank(new_rank)
    key = group.key
    old_group = old.group(key)
    if old_group is None:
        return start, start + group.total

    copy = group.copy_at(local)
    try:
        ri = group.r_pos[copy.r_row]
        sj = group.s_pos[copy.s_row]
        tj = group.t_pos[copy.t_row]
    except KeyError:
        return start, start + group.total

    new_dims = (group.r_m[ri], group.s_m[sj], group.t_m[tj])
    try:
        ori = old_group.r_pos[copy.r_row]
        osj = old_group.s_pos[copy.s_row]
        otj = old_group.t_pos[copy.t_row]
    except KeyError:
        first = replace(copy, r_rep=1, s_rep=1, t_rep=1)
        block_start = start + group.rank_of_copy(first)
        return block_start, block_start + _prod(new_dims)

    old_dims = (old_group.r_m[ori], old_group.s_m[osj], old_group.t_m[otj])
    first = replace(copy, r_rep=1, s_rep=1, t_rep=1)
    block_start = start + group.rank_of_copy(first)
    lo, hi = bad_run_interval(new_rank - block_start, new_dims, old_dims)
    return block_start + lo, block_start + hi


@dataclass(slots=True)
class RankMapping:
    """Compressed commit mapping between old and new snapshot rank spaces."""

    old: Snapshot
    new: Snapshot
    affected_keys: tuple[tuple[Any, Any, Any], ...]
    unaffected_segments: tuple[AffineSegment, ...]
    deleted_old_ranges: LazyRangeSet
    added_new_ranges: LazyRangeSet
    deleted_old_count: int
    added_new_count: int
    retained_count: int

    @classmethod
    def build(
        cls,
        old: Snapshot,
        new: Snapshot,
        affected_keys: set[tuple[Any, Any, Any]],
    ) -> "RankMapping":
        keys = tuple(sorted(affected_keys))
        segments: list[AffineSegment] = []

        prev: tuple[Any, Any, Any] | None = None
        for key in keys:
            old_start = 0 if prev is None else old.rank_before_key(prev) + old.group_total(prev)
            old_stop = old.rank_before_key(key)
            new_start = 0 if prev is None else new.rank_before_key(prev) + new.group_total(prev)
            new_stop = new.rank_before_key(key)
            if old_stop > old_start or new_stop > new_start:
                if old_stop - old_start != new_stop - new_start:
                    raise AssertionError("unaffected rank bridge has mismatched length")
                if old_stop > old_start:
                    segments.append(AffineSegment(old_start, old_stop, new_start, new_stop))
            prev = key

        tail_old_start = 0 if prev is None else old.rank_before_key(prev) + old.group_total(prev)
        tail_new_start = 0 if prev is None else new.rank_before_key(prev) + new.group_total(prev)
        if old.total - tail_old_start != new.total - tail_new_start:
            raise AssertionError("unaffected tail has mismatched length")
        if old.total > tail_old_start:
            segments.append(
                AffineSegment(tail_old_start, old.total, tail_new_start, new.total)
            )

        old_affected_total = 0
        retained_in_affected = 0
        for key in keys:
            og = old.group(key)
            ng = new.group(key)
            if og is not None:
                old_affected_total += og.total
                if ng is not None:
                    retained_in_affected += _common_retained_mass(og, ng)

        retained_count = old.total - old_affected_total + retained_in_affected
        deleted_count = old.total - retained_count
        added_count = new.total - retained_count
        if deleted_count < 0 or added_count < 0:
            raise AssertionError("mapping counts are negative")

        mapping = cls(
            old=old,
            new=new,
            affected_keys=keys,
            unaffected_segments=tuple(segments),
            deleted_old_ranges=None,  # type: ignore[arg-type]
            added_new_ranges=None,  # type: ignore[arg-type]
            deleted_old_count=deleted_count,
            added_new_count=added_count,
            retained_count=retained_count,
        )
        mapping.deleted_old_ranges = LazyRangeSet(
            lambda: _iter_side_patterns(old, new, keys, "old"),
            lambda rank: find_deleted_interval(old, new, rank),
        )
        mapping.added_new_ranges = LazyRangeSet(
            lambda: _iter_side_patterns(old, new, keys, "new"),
            lambda rank: find_added_interval(new, old, rank),
        )
        return mapping

    def iter_block_mappings(self) -> Iterator[BlockMapping]:
        """Lazily enumerate row-id triple mappings in affected key groups."""
        yield from _iter_block_mappings(self.old, self.new, self.affected_keys)

    def iter_retained_runs(self) -> Iterator[tuple[int, int, int, int]]:
        """Yield ordinary contiguous retained runs in affected blocks."""
        for block in self.iter_block_mappings():
            if block.old_dims is not None and block.new_dims is not None:
                yield from block.iter_retained_runs()

    # ---- forward / reverse location ------------------------------------

    def locate_old(self, old_rank: int) -> RankTranslation:
        self.old._check_rank(old_rank)
        old_copy = self.old.at(old_rank)
        try:
            new_rank = self.new.rank_of(old_copy)
        except CopyNotFoundError:
            interval = find_deleted_interval(self.old, self.new, old_rank)
            return RankTranslation("deleted", old_rank=old_rank, old_interval=interval)
        return RankTranslation("retained", old_rank=old_rank, new_rank=new_rank)

    def locate_new(self, new_rank: int) -> RankTranslation:
        self.new._check_rank(new_rank)
        new_copy = self.new.at(new_rank)
        try:
            old_rank = self.old.rank_of(new_copy)
        except CopyNotFoundError:
            interval = find_added_interval(self.new, self.old, new_rank)
            return RankTranslation("added", new_rank=new_rank, new_interval=interval)
        return RankTranslation("retained", old_rank=old_rank, new_rank=new_rank)

    def old_to_new(self, old_rank: int) -> int:
        result = self.locate_old(old_rank)
        if result.new_rank is None:
            raise DeletedRankError(old_rank, result.old_interval)
        return result.new_rank

    def new_to_old(self, new_rank: int) -> int:
        result = self.locate_new(new_rank)
        if result.old_rank is None:
            raise AddedRankError(new_rank, result.new_interval)
        return result.old_rank

    def forward(self, old_rank: int) -> RankTranslation:
        return self.locate_old(old_rank)

    def reverse(self, new_rank: int) -> RankTranslation:
        return self.locate_new(new_rank)

    def map_copy_to_new(self, copy):
        old_rank = self.old.rank_of(copy)
        return self.new.at(self.old_to_new(old_rank))

    def map_copy_to_old(self, copy):
        new_rank = self.new.rank_of(copy)
        return self.old.at(self.new_to_old(new_rank))

    def deleted_interval_containing(self, old_rank: int) -> tuple[int, int]:
        return find_deleted_interval(self.old, self.new, old_rank)

    def added_interval_containing(self, new_rank: int) -> tuple[int, int]:
        return find_added_interval(self.new, self.old, new_rank)
