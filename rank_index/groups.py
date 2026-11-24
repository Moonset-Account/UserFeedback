"""Compressed ``(a,b,c)`` key groups.

A group stores only physical row references and multiplicity sums/prefixes.
It never creates one Python object per replica.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Hashable

from .model import Copy, CopyNotFoundError
from .tables import TableSet


def _sorted_rows(rows: list) -> tuple:
    return tuple(sorted(rows, key=lambda row: row.row_id))


def _mass_ends(multiplicities: tuple[int, ...]) -> tuple[int, ...]:
    out: list[int] = []
    total = 0
    for m in multiplicities:
        total += m
        out.append(total)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class KeyGroup:
    key: tuple[Any, Any, Any]
    r_ids: tuple[Hashable, ...]
    s_ids: tuple[Hashable, ...]
    t_ids: tuple[Hashable, ...]
    r_m: tuple[int, ...]
    s_m: tuple[int, ...]
    t_m: tuple[int, ...]
    r_mass_ends: tuple[int, ...]
    s_mass_ends: tuple[int, ...]
    t_mass_ends: tuple[int, ...]
    r_pos: dict[Hashable, int]
    s_pos: dict[Hashable, int]
    t_pos: dict[Hashable, int]
    total: int

    @property
    def r_mass(self) -> int:
        return self.r_mass_ends[-1] if self.r_mass_ends else 0

    @property
    def s_mass(self) -> int:
        return self.s_mass_ends[-1] if self.s_mass_ends else 0

    @property
    def t_mass(self) -> int:
        return self.t_mass_ends[-1] if self.t_mass_ends else 0

    def copy_at(self, local_rank: int) -> Copy:
        if not isinstance(local_rank, int) or isinstance(local_rank, bool):
            raise TypeError("rank must be an integer")
        if local_rank < 0 or local_rank >= self.total:
            raise IndexError("local rank outside key group")

        a, b, c = self.key

        # Select R row by its whole contribution R_m * S_mass * T_mass.
        r_unit = self.s_mass * self.t_mass
        r_scaled = local_rank // r_unit if r_unit else 0
        ri = bisect_right(self.r_mass_ends, r_scaled)
        if ri == len(self.r_m):  # exact upper boundary belongs to next impossible? guard
            ri -= 1
        r_before = self.r_mass_ends[ri - 1] if ri else 0
        q = local_rank - r_before * r_unit
        mr = self.r_m[ri]

        # Select S row.  Boundaries are mr * T_mass * cumulative S mass.
        s_factor = mr * self.t_mass
        sj = bisect_right(self.s_mass_ends, q // s_factor)
        if sj == len(self.s_m):
            sj -= 1
        s_before = self.s_mass_ends[sj - 1] if sj else 0
        q -= s_before * s_factor
        ms = self.s_m[sj]

        # Select T row.  Boundaries are mr*ms * cumulative T mass.
        t_factor = mr * ms
        tj = bisect_right(self.t_mass_ends, q // t_factor)
        if tj == len(self.t_m):
            tj -= 1
        t_before = self.t_mass_ends[tj - 1] if tj else 0
        q -= t_before * t_factor
        mt = self.t_m[tj]

        # q is inside one (r,s,t) block of size mr*ms*mt.
        r_rep0 = q // (ms * mt)
        rem = q - r_rep0 * ms * mt
        s_rep0 = rem // mt
        t_rep0 = rem - s_rep0 * mt

        return Copy(
            a=a,
            b=b,
            c=c,
            r_row=self.r_ids[ri],
            s_row=self.s_ids[sj],
            t_row=self.t_ids[tj],
            r_rep=r_rep0 + 1,
            s_rep=s_rep0 + 1,
            t_rep=t_rep0 + 1,
        )

    def rank_of_copy(self, copy: Copy) -> int:
        if copy.key != self.key:
            raise CopyNotFoundError("copy belongs to a different (a,b,c) key")
        try:
            ri = self.r_pos[copy.r_row]
            sj = self.s_pos[copy.s_row]
            tj = self.t_pos[copy.t_row]
        except KeyError as exc:
            raise CopyNotFoundError("one or more physical row numbers are absent") from exc

        mr, ms, mt = self.r_m[ri], self.s_m[sj], self.t_m[tj]
        if not (1 <= copy.r_rep <= mr and 1 <= copy.s_rep <= ms and 1 <= copy.t_rep <= mt):
            raise CopyNotFoundError("replica ordinal is outside the row multiplicity")

        r_before = self.r_mass_ends[ri - 1] if ri else 0
        s_before = self.s_mass_ends[sj - 1] if sj else 0
        t_before = self.t_mass_ends[tj - 1] if tj else 0

        rank = r_before * self.s_mass * self.t_mass
        rank += mr * s_before * self.t_mass
        rank += mr * ms * t_before
        rank += (copy.r_rep - 1) * ms * mt
        rank += (copy.s_rep - 1) * mt
        rank += copy.t_rep - 1
        return rank

    def iter_blocks(self):
        """Yield compressed row-triple blocks in output row-id order.

        Each yield is ``(triple, local_start, dims)`` where
        ``dims=(mr, ms, mt)``.  The number of yields is the number of
        physical row-id triples, never the product of multiplicities.
        """
        start = 0
        for ri, (rid, mr) in enumerate(zip(self.r_ids, self.r_m)):
            for sj, (sid, ms) in enumerate(zip(self.s_ids, self.s_m)):
                for tj, (tid, mt) in enumerate(zip(self.t_ids, self.t_m)):
                    yield (rid, sid, tid), start, (mr, ms, mt)
                    start += mr * ms * mt


def build_group(key: tuple[Any, Any, Any], tables: TableSet) -> KeyGroup | None:
    a, b, c = key
    r_rows = _sorted_rows(tables.r_ab.get((a, b), ()))
    s_rows = _sorted_rows(tables.s_bc.get((b, c), ()))
    t_rows = _sorted_rows(tables.t_ca.get((c, a), ()))
    if not r_rows or not s_rows or not t_rows:
        return None

    r_ids = tuple(row.row_id for row in r_rows)
    s_ids = tuple(row.row_id for row in s_rows)
    t_ids = tuple(row.row_id for row in t_rows)
    r_m = tuple(row.multiplicity for row in r_rows)
    s_m = tuple(row.multiplicity for row in s_rows)
    t_m = tuple(row.multiplicity for row in t_rows)
    r_ends = _mass_ends(r_m)
    s_ends = _mass_ends(s_m)
    t_ends = _mass_ends(t_m)
    total = r_ends[-1] * s_ends[-1] * t_ends[-1]

    return KeyGroup(
        key=key,
        r_ids=r_ids,
        s_ids=s_ids,
        t_ids=t_ids,
        r_m=r_m,
        s_m=s_m,
        t_m=t_m,
        r_mass_ends=r_ends,
        s_mass_ends=s_ends,
        t_mass_ends=t_ends,
        r_pos={row_id: i for i, row_id in enumerate(r_ids)},
        s_pos={row_id: i for i, row_id in enumerate(s_ids)},
        t_pos={row_id: i for i, row_id in enumerate(t_ids)},
        total=total,
    )
