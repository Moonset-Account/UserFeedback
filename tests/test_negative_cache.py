"""negative_cache 的验收测试。

覆盖题面交叉约束：
- 可接受结果：开区间证明名称不存在、起点证明类型缺失、绕回区间；
- 应被阻止/拒绝：非法标签（含通配符）、失效记录写入、冲突证据；
- 边界/交错：开区间端点、失效时刻边界、冲突随失效消解、
  同名委派点的父子区证据隔离。
"""

import unittest

from negative_cache import NegativeCache, NSECRecord, QueryResult


def rec(zone, owner, next_name, types, expires):
    return NSECRecord(
        zone=zone,
        owner=owner,
        next_name=next_name,
        types=frozenset(types),
        expires=expires,
    )


class TestAcceptableResults(unittest.TestCase):
    """可接受结果：名称不存在 / 类型不存在 / 绕回区间。"""

    def setUp(self):
        self.cache = NegativeCache()
        self.cache.add(rec("example", "alpha", "charlie", {"A"}, expires=1000), now=0)

    def test_open_interval_proves_name_missing(self):
        # "beta" 严格落在 (alpha, charlie) 开区间内。
        result = self.cache.query("example", "beta", "A", now=500)
        self.assertEqual(result, QueryResult.NAME_MISSING)

    def test_owner_proves_type_missing(self):
        # 起点 alpha 只有 A 类型，查询 TXT 证明类型缺失。
        result = self.cache.query("example", "alpha", "TXT", now=500)
        self.assertEqual(result, QueryResult.TYPE_MISSING)

    def test_owner_with_existing_type_is_insufficient(self):
        # 起点处类型据记录存在，否定缓存不能下结论。
        result = self.cache.query("example", "alpha", "A", now=500)
        self.assertEqual(result, QueryResult.INSUFFICIENT)

    def test_wraparound_interval(self):
        cache = NegativeCache()
        # next <= owner：区间绕回排序末尾。
        cache.add(rec("example", "zulu", "alpha", {"SOA", "NS"}, expires=1000), now=0)
        # 排序末尾一侧：owner 之后。
        self.assertEqual(
            cache.query("example", "zz", "A", now=500), QueryResult.NAME_MISSING
        )
        # 排序开头一侧：next_name 之前。
        self.assertEqual(
            cache.query("example", "aardvark", "A", now=500),
            QueryResult.NAME_MISSING,
        )
        # 绕回区间不覆盖中段。
        self.assertEqual(
            cache.query("example", "middle", "A", now=500),
            QueryResult.INSUFFICIENT,
        )


class TestBoundaries(unittest.TestCase):
    """边界：开区间端点与失效时刻。"""

    def setUp(self):
        self.cache = NegativeCache()
        self.cache.add(rec("example", "alpha", "charlie", {"A"}, expires=1000), now=0)

    def test_interval_endpoints_not_covered(self):
        # 终点 charlie 不在开区间内，也没有自己的记录：证据不足。
        self.assertEqual(
            self.cache.query("example", "charlie", "A", now=500),
            QueryResult.INSUFFICIENT,
        )
        # 起点 alpha 不被当作名称不存在。
        self.assertNotEqual(
            self.cache.query("example", "alpha", "TXT", now=500),
            QueryResult.NAME_MISSING,
        )

    def test_expiry_boundary(self):
        # now == expires 时记录已失效，证据消失。
        self.assertEqual(
            self.cache.query("example", "beta", "A", now=999),
            QueryResult.NAME_MISSING,
        )
        self.assertEqual(
            self.cache.query("example", "beta", "A", now=1000),
            QueryResult.INSUFFICIENT,
        )

    def test_unknown_zone_is_insufficient(self):
        self.assertEqual(
            self.cache.query("other", "beta", "A", now=500),
            QueryResult.INSUFFICIENT,
        )


class TestRejectedInputs(unittest.TestCase):
    """应被阻止或拒绝的输入。"""

    def test_uppercase_label_rejected(self):
        with self.assertRaises(ValueError):
            rec("example", "Alpha", "charlie", {"A"}, expires=1000)

    def test_wildcard_record_rejected(self):
        # 题面规定没有通配记录，"*" 不是合法标签。
        with self.assertRaises(ValueError):
            rec("example", "*", "charlie", {"A"}, expires=1000)

    def test_multilevel_name_rejected(self):
        with self.assertRaises(ValueError):
            rec("example", "a.b", "charlie", {"A"}, expires=1000)

    def test_empty_types_rejected(self):
        with self.assertRaises(ValueError):
            rec("example", "alpha", "charlie", set(), expires=1000)

    def test_degenerate_interval_rejected(self):
        with self.assertRaises(ValueError):
            rec("example", "alpha", "alpha", {"A"}, expires=1000)

    def test_expired_record_refused_on_add(self):
        cache = NegativeCache()
        with self.assertRaises(ValueError):
            cache.add(rec("example", "alpha", "charlie", {"A"}, expires=100), now=200)

    def test_illegal_query_rejected(self):
        cache = NegativeCache()
        with self.assertRaises(ValueError):
            cache.query("example", "Bad Name", "A", now=0)
        with self.assertRaises(ValueError):
            cache.query("example", "alpha", "not a type!", now=0)


class TestConflictingEvidence(unittest.TestCase):
    """相反的有效证据重叠时返回待重新核验。"""

    def setUp(self):
        self.cache = NegativeCache()
        # 两条有效记录的区间相互重叠：一致区内不可能同时成立。
        self.cache.add(rec("example", "alpha", "charlie", {"A"}, expires=1000), now=0)
        self.cache.add(rec("example", "bravo", "delta", {"A"}, expires=800), now=0)

    def test_name_covered_by_two_intervals_needs_revalidation(self):
        # "bx" 同时落在 (alpha, charlie) 与 (bravo, delta) 内。
        self.assertEqual(
            self.cache.query("example", "bx", "A", now=500),
            QueryResult.REVALIDATE,
        )

    def test_owner_inside_other_interval_needs_revalidation(self):
        # "bravo" 是记录起点，又落在 (alpha, charlie) 开区间内。
        self.assertEqual(
            self.cache.query("example", "bravo", "TXT", now=500),
            QueryResult.REVALIDATE,
        )

    def test_non_overlapping_part_still_conclusive(self):
        # "apple" 只被 (alpha, charlie) 覆盖，结论不受影响。
        self.assertEqual(
            self.cache.query("example", "apple", "A", now=500),
            QueryResult.NAME_MISSING,
        )

    def test_conflict_resolves_after_one_record_expires(self):
        # 交错场景：800 时刻后 (bravo, delta) 失效，冲突消解，
        # 原本待核验的名称重新得到确定结论。
        self.assertEqual(
            self.cache.query("example", "bx", "A", now=800),
            QueryResult.NAME_MISSING,
        )
        self.assertEqual(
            self.cache.query("example", "bravo", "TXT", now=800),
            QueryResult.NAME_MISSING,
        )


class TestZoneCutIsolation(unittest.TestCase):
    """同名委派点：父区证明不能借给子区，反之亦然。"""

    def setUp(self):
        self.cache = NegativeCache()
        # 父区 example 在委派点 sub 只有 NS（无 SOA）。
        self.cache.add(
            rec("example", "sub", "zulu", {"NS"}, expires=1000), now=0
        )
        # 子区 sub 自己的证据：apex 记录与区间 (apex, web)。
        self.cache.add(
            rec("sub", "sub", "web", {"SOA", "NS"}, expires=1000), now=0
        )

    def test_delegation_point_detected(self):
        parent = rec("example", "sub", "zulu", {"NS"}, expires=1000)
        self.assertTrue(parent.is_delegation())
        apex = rec("sub", "sub", "web", {"SOA", "NS"}, expires=1000)
        self.assertFalse(apex.is_delegation())

    def test_parent_proof_not_lent_to_child(self):
        # 父区记录证明 (sub, zulu) 内名称不存在，但子区查询不受影响：
        # "web" 落在父区区间内，在子区却是子区记录的区间终点（证据不足）。
        self.assertEqual(
            self.cache.query("example", "web", "A", now=500),
            QueryResult.NAME_MISSING,
        )
        self.assertEqual(
            self.cache.query("sub", "web", "A", now=500),
            QueryResult.INSUFFICIENT,
        )

    def test_child_proof_not_lent_to_parent(self):
        # 子区 apex 证明 sub 处 TXT 缺失，父区在 sub 只有 NS 委派：
        # 父区查询 sub/TXT 由父区自己的记录判定（NS 之外的类型缺失）。
        self.assertEqual(
            self.cache.query("sub", "sub", "TXT", now=500),
            QueryResult.TYPE_MISSING,
        )
        self.assertEqual(
            self.cache.query("example", "sub", "TXT", now=500),
            QueryResult.TYPE_MISSING,
        )
        # 父区查 NS：委派点据记录存在 NS，否定缓存无法下结论。
        self.assertEqual(
            self.cache.query("example", "sub", "NS", now=500),
            QueryResult.INSUFFICIENT,
        )


class TestReplacement(unittest.TestCase):
    """同 (zone, owner) 记录替换语义。"""

    def test_same_owner_record_replaced(self):
        cache = NegativeCache()
        cache.add(rec("example", "alpha", "charlie", {"A"}, expires=1000), now=0)
        cache.add(
            rec("example", "alpha", "charlie", {"A", "TXT"}, expires=2000), now=0
        )
        # 旧记录被替换：TXT 现在据记录存在。
        self.assertEqual(
            cache.query("example", "alpha", "TXT", now=500),
            QueryResult.INSUFFICIENT,
        )


if __name__ == "__main__":
    unittest.main()
