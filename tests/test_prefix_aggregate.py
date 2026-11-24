import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest  # noqa: E402

from planner.aggregate import (  # noqa: E402
    AggregationConflict,
    collapse_siblings,
    decompose_interval,
    minimal_advertisements,
)
from planner.prefix import (  # noqa: E402
    BITS,
    SPACE_SIZE,
    Prefix,
    cover_with_prefixes,
    intervals_disjoint,
    merge_intervals,
    parse_prefix,
    union_covers,
)


class TestPrefixBasics(unittest.TestCase):
    def test_alignment_validation(self) -> None:
        Prefix(0, 64)
        Prefix((1 << 64), 64)
        with self.assertRaises(ValueError):
            Prefix(1, 64)
        with self.assertRaises(ValueError):
            Prefix(-1, 64)
        with self.assertRaises(ValueError):
            Prefix(1 << 128, 0)

    def test_family_relations(self) -> None:
        p = Prefix(0x2000, 116)
        sib = p.sibling
        self.assertEqual(sib.addr, 0x3000)
        parent = p.parent
        self.assertEqual(parent.length, 115)
        self.assertEqual(parent.addr, 0x2000)
        self.assertEqual(p.child(0), Prefix(0x2000, 117))
        self.assertEqual(p.child(1).addr, 0x2800)
        self.assertTrue(parent.contains(p) and parent.contains(sib))

    def test_parse_and_format_roundtrip(self) -> None:
        pfx = parse_prefix("2001:db8::/32")
        self.assertEqual(pfx.length, 32)
        self.assertEqual(str(pfx), "2001:db8::/32")
        self.assertEqual(pfx.size, 1 << 96)

    def test_intervals(self) -> None:
        self.assertTrue(intervals_disjoint((0, 4), (4, 8)))
        self.assertFalse(intervals_disjoint((0, 5), (4, 8)))
        self.assertEqual(merge_intervals([(0, 4), (4, 8), (20, 24)]), [(0, 8), (20, 24)])


class TestDecompose(unittest.TestCase):
    def test_aligned_block_single(self) -> None:
        parts = decompose_interval(0, 16)
        self.assertEqual(parts, [Prefix(0, BITS - 4)])

    def test_odd_interval_minimal_pieces(self) -> None:
        # [3, 10) 的规范最小分解为 3 块：[3,4) /128、[4,8) /126、[8,10) /127。
        parts = decompose_interval(3, 10)
        covered = merge_intervals([p.interval for p in parts])
        self.assertEqual(covered, [(3, 10)])
        self.assertEqual(len(parts), 3)
        self.assertEqual([p.length for p in parts], [128, 126, 127])
        rebuilt = collapse_siblings([(p, "t", "p") for p in parts])
        self.assertEqual(
            sorted(item[0].interval for item in rebuilt),
            sorted(p.interval for p in parts),
        )

    def test_full_space(self) -> None:
        parts = decompose_interval(0, SPACE_SIZE)
        self.assertEqual(parts, [Prefix(0, 0)])

    def test_cover_with_prefixes(self) -> None:
        pool = [Prefix(0, 126), Prefix(4, 126), Prefix(8, 126)]
        pieces = cover_with_prefixes(1, 9, pool)
        self.assertEqual(merge_intervals([p.interval for p in pieces]), [(1, 9)])
        # 起点非对齐时首块必须是 /128，验证对齐处理。
        self.assertEqual(pieces[0], Prefix(1, 128))
        with self.assertRaises(ValueError):
            cover_with_prefixes(0, 1 << 127, [Prefix(0, 128)])

    def test_union_covers_symbolic(self) -> None:
        # 巨大前缀 + 极少固定点：只用端点运算判断覆盖，不枚举地址。
        big = parse_prefix("2001:db8::/32")
        anchor = parse_prefix("2001:db8:1::/48")
        self.assertTrue(union_covers([big], anchor))
        outside = parse_prefix("2001:db9:1::/48")
        self.assertFalse(union_covers([big], outside))


class TestAggregation(unittest.TestCase):
    def _p(self, addr: int, length: int) -> Prefix:
        return Prefix(addr, length)

    def test_same_owner_siblings_merge(self) -> None:
        base = 1 << 64
        a = Prefix(base, 65)
        b = Prefix(base + (1 << 63), 65)
        out = minimal_advertisements([(a, "t1", "p1"), (b, "t1", "p1")])
        self.assertEqual(out, [(Prefix(base, 64), "t1", "p1")])

    def test_different_policy_never_merges(self) -> None:
        base = 1 << 64
        a = Prefix(base, 65)
        b = Prefix(base + (1 << 63), 65)
        out = minimal_advertisements([(a, "t1", "p1"), (b, "t1", "p2")])
        self.assertEqual(len(out), 2)

    def test_hole_is_not_announced(self) -> None:
        # 三块相邻持有，中间缺一块，结果不得出现覆盖空洞的大前缀。
        block = 1 << 64
        entries = [
            (Prefix(0 * block, 64), "t1", "p"),
            (Prefix(2 * block, 64), "t1", "p"),
        ]
        out = minimal_advertisements(entries)
        intervals = merge_intervals([p.interval for p, *_ in out])
        self.assertEqual(intervals, [(0, block), (2 * block, 3 * block)])
        for pfx, *_ in out:
            self.assertNotEqual(pfx, Prefix(0, 63))

    def test_overlapping_different_owners_conflict(self) -> None:
        with self.assertRaises(AggregationConflict):
            minimal_advertisements(
                [(Prefix(0, 64), "t1", "p"), (Prefix(0, 65), "t2", "p")]
            )

    def test_collapse_matches_interval_method(self) -> None:
        rng = [
            (Prefix(0, 66), "t", "p"),
            (Prefix(1 << 62, 66), "t", "p"),
            (Prefix(1 << 63, 65), "t", "p"),
        ]
        a = minimal_advertisements(rng)
        b = collapse_siblings(rng)
        self.assertEqual(
            sorted(x[0].interval for x in a), sorted(x[0].interval for x in b)
        )


if __name__ == "__main__":
    unittest.main()
