"""计划器测试：共享状态注册、AVG 成对重用、条件 intern 与阻塞条件定位。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sharedscan.model import (  # noqa: E402
    Agg,
    AggFn,
    Cmp,
    EmptyGroupPolicy,
    Request,
    Semantics,
    Table,
)
from sharedscan.planner import (  # noqa: E402
    DISTINCT_MERGE_NOT_EXCLUSIVE,
    DISTINCT_PARTIAL,
    DISTINCT_SET,
    GROUP_KEY_MISMATCH,
    NON_NUMERIC_AGG,
    TABLE_MISMATCH,
    UNKNOWN_PARTITION,
    BlockingReasons,
    build_shared_plan,
)
from sharedscan.states import COUNT, COUNT_STAR, SUM  # noqa: E402
from synth_data import build_catalog  # noqa: E402


def _req(name, aggs, *, where=None, partitions=("p1", "p2"), group_keys=("region",),
         having=None, semantics=None, key_domain=()):
    return Request(
        name=name,
        table="sales",
        partitions=partitions,
        group_keys=group_keys,
        aggs=aggs,
        where=where,
        having=having,
        semantics=semantics or Semantics(),
        key_domain=key_domain,
    )


def _state_keys(plan, kind=None, column=None, cond=None):
    out = []
    for key in plan.state_specs:
        k, col, cid, _inc, _scope = key
        if kind is not None and k != kind:
            continue
        if column is not None and col != column:
            continue
        if cond is not None and cid != cond:
            continue
        out.append(key)
    return out


class TestPlannerSharing(unittest.TestCase):
    def setUp(self):
        self.catalog = build_catalog(exclusive_cust=False)

    def test_single_scan_union_partitions(self):
        r1 = _req("r1", (Agg("n", AggFn.COUNT_STAR, None),), partitions=("p1",))
        r2 = _req("r2", (Agg("n", AggFn.COUNT_STAR, None),), partitions=("p2", "p3"))
        plan = build_shared_plan([r1, r2], self.catalog)
        self.assertEqual(plan.partitions, ("p1", "p2", "p3"))
        self.assertEqual(len(plan.projections), 2)

    def test_avg_reuses_sum_count_pair(self):
        r1 = _req("r1", (Agg("a", AggFn.AVG, "amount"),))
        r2 = _req("r2", (Agg("s", AggFn.SUM, "amount"),
                         Agg("c", AggFn.COUNT, "amount")))
        plan = build_shared_plan([r1, r2], self.catalog)
        # 相同条件（无条件）+ 相同分区范围：sum/count 各只有一份状态
        self.assertEqual(len(_state_keys(plan, SUM, "amount", cond=None)), 1)
        self.assertEqual(len(_state_keys(plan, COUNT, "amount", cond=None)), 1)
        binding = plan.projections["r1"].bindings["a"]
        self.assertEqual(binding.kind, "avg")
        # AVG 绑定的两个键恰好就是 r2 聚合使用的键
        self.assertEqual(binding.sum_key, plan.projections["r2"].bindings["s"].state_key)
        self.assertEqual(binding.count_key, plan.projections["r2"].bindings["c"].state_key)

    def test_identical_where_is_interned_to_one_condition(self):
        pred = Cmp("amount", ">", 0)
        r1 = _req("r1", (Agg("n", AggFn.COUNT_STAR, None),), where=pred)
        r2 = _req("r2", (Agg("n", AggFn.COUNT_STAR, None),), where=pred)
        plan = build_shared_plan([r1, r2], self.catalog)
        self.assertEqual(len(plan.conditions), 1)
        self.assertEqual(plan.projections["r1"].cond_id,
                         plan.projections["r2"].cond_id)

    def test_different_where_yields_separate_conditional_states(self):
        r1 = _req("r1", (Agg("n", AggFn.COUNT_STAR, None),),
                  where=Cmp("amount", ">", 0))
        r2 = _req("r2", (Agg("n", AggFn.COUNT_STAR, None),),
                  where=Cmp("amount", ">", 100))
        plan = build_shared_plan([r1, r2], self.catalog)
        self.assertEqual(len(plan.conditions), 2)
        # count_star 条件态：两个条件各一份；每个请求的 presence 与自己的
        # 条件态同键（count_star 不引用列），因此不再额外产生状态
        self.assertEqual(len(_state_keys(plan, COUNT_STAR)), 2)

    def test_partition_scope_is_part_of_state_identity(self):
        pred = Cmp("amount", ">", 0)
        r1 = _req("r1", (Agg("s", AggFn.SUM, "amount"),),
                  where=pred, partitions=("p1",))
        r2 = _req("r2", (Agg("s", AggFn.SUM, "amount"),),
                  where=pred, partitions=("p1", "p2"))
        plan = build_shared_plan([r1, r2], self.catalog)
        # 谓词相同但分区范围不同 -> 两份独立 sum 状态
        self.assertEqual(len(_state_keys(plan, SUM, "amount")), 2)

    def test_distinct_single_partition_keeps_value_set(self):
        r1 = _req("r1", (Agg("d", AggFn.COUNT_DISTINCT, "cust_id"),),
                  partitions=("p1",))
        plan = build_shared_plan([r1], self.catalog)
        kinds = {spec.kind for spec in plan.state_specs.values()}
        self.assertIn(DISTINCT_SET, kinds)
        self.assertNotIn(DISTINCT_PARTIAL, kinds)

    def test_distinct_multi_partition_blocked_without_exclusive_mark(self):
        r1 = _req("r1", (Agg("d", AggFn.COUNT_DISTINCT, "cust_id"),),
                  partitions=("p1", "p2"))
        with self.assertRaises(BlockingReasons) as ctx:
            build_shared_plan([r1], self.catalog)
        codes = {r.code for r in ctx.exception.reasons}
        self.assertIn(DISTINCT_MERGE_NOT_EXCLUSIVE, codes)

    def test_distinct_multi_partition_allowed_with_exclusive_mark(self):
        catalog = build_catalog(exclusive_cust=True)
        r1 = _req("r1", (Agg("d", AggFn.COUNT_DISTINCT, "cust_id"),),
                  partitions=("p1", "p2"))
        plan = build_shared_plan([r1], catalog)
        self.assertIn(DISTINCT_PARTIAL, {spec.kind for spec in plan.state_specs.values()})
        partial_specs = [s for s in plan.state_specs.values()
                         if s.kind == DISTINCT_PARTIAL]
        self.assertTrue(all(s.exclusive for s in partial_specs))


class TestPlannerBlocking(unittest.TestCase):
    def setUp(self):
        self.catalog = build_catalog(exclusive_cust=False)

    def _blocked_codes(self, requests):
        with self.assertRaises(BlockingReasons) as ctx:
            build_shared_plan(requests, self.catalog)
        return {r.code for r in ctx.exception.reasons}, ctx.exception.reasons

    def test_table_mismatch(self):
        # 在目录里放第二张同构表，确保触发的是“表不同”而非“表未知”
        from sharedscan.model import make_catalog
        other = Table(name="other", schema=dict(self.catalog.tables["sales"].schema),
                      partitions={"p1": []})
        catalog = make_catalog(
            {"sales": self.catalog.tables["sales"], "other": other},
            exclusive_distinct=set(),
        )
        r1 = _req("r1", (Agg("n", AggFn.COUNT_STAR, None),))
        r2 = Request(
            name="r2", table="other", partitions=("p1",),
            group_keys=("region",), aggs=(Agg("n", AggFn.COUNT_STAR, None),),
        )
        with self.assertRaises(BlockingReasons) as ctx:
            build_shared_plan([r1, r2], catalog)
        codes = {r.code for r in ctx.exception.reasons}
        self.assertIn(TABLE_MISMATCH, codes)
        self.assertTrue(any("r2" in r.request for r in ctx.exception.reasons))

    def test_unknown_partition(self):
        r1 = _req("r1", (Agg("n", AggFn.COUNT_STAR, None),),
                  partitions=("p1", "nope"))
        codes, _ = self._blocked_codes([r1])
        self.assertIn(UNKNOWN_PARTITION, codes)

    def test_group_key_mismatch(self):
        r1 = _req("r1", (Agg("n", AggFn.COUNT_STAR, None),),
                  group_keys=("region",))
        r2 = _req("r2", (Agg("n", AggFn.COUNT_STAR, None),),
                  group_keys=("cust_id",))
        codes, _ = self._blocked_codes([r1, r2])
        self.assertIn(GROUP_KEY_MISMATCH, codes)

    def test_non_numeric_sum(self):
        r1 = _req("r1", (Agg("s", AggFn.SUM, "region"),))
        codes, _ = self._blocked_codes([r1])
        self.assertIn(NON_NUMERIC_AGG, codes)

    def test_multiple_reasons_collected_at_once(self):
        # r1 同时触发未知分区 + 非数值聚合；r2 触发分组键集合不一致
        r1 = _req("r1", (Agg("s", AggFn.SUM, "region"),),
                  group_keys=("region",), partitions=("ghost",))
        r2 = _req("r2", (Agg("n", AggFn.COUNT_STAR, None),),
                  group_keys=("cust_id",))
        codes, _ = self._blocked_codes([r1, r2])
        self.assertIn(GROUP_KEY_MISMATCH, codes)
        self.assertIn(NON_NUMERIC_AGG, codes)
        self.assertIn(UNKNOWN_PARTITION, codes)

    def test_having_bad_reference_blocks(self):
        r1 = _req("r1", (Agg("n", AggFn.COUNT_STAR, None),),
                  having=Cmp("amount", ">", 0))  # amount 既非键也非聚合别名
        codes, _ = self._blocked_codes([r1])
        self.assertIn("BAD_HAVING_REF", codes)

    def test_type_incompatible_literal_blocks(self):
        r1 = _req("r1", (Agg("n", AggFn.COUNT_STAR, None),),
                  where=Cmp("amount", "=", "not-a-number"))
        codes, _ = self._blocked_codes([r1])
        self.assertIn("INVALID_LITERAL_TYPE", codes)

    def test_domain_arity_mismatch_blocks(self):
        r1 = _req(
            "r1", (Agg("n", AggFn.COUNT_STAR, None),),
            semantics=Semantics(empty_group=EmptyGroupPolicy.KEEP_DOMAIN),
            key_domain=(("east", "extra"),),
        )
        codes, _ = self._blocked_codes([r1])
        self.assertIn("DOMAIN_ARITY", codes)

    def test_empty_agg_set_blocks(self):
        r1 = _req("r1", ())
        codes, _ = self._blocked_codes([r1])
        self.assertIn("AGG_EMPTY", codes)


if __name__ == "__main__":
    unittest.main()
