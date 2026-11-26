"""端到端测试：改写前（参考执行）与改写后（共享计划投影）逐行、逐类型对照。

每个正常场景都通过 :func:`verify_plan` 断言行多重集与类型族完全一致，
并对关键场景额外断言具体期望值，保证不是“两个实现一起错”。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sharedscan.model import (  # noqa: E402
    Agg,
    AggFn,
    And,
    Cmp,
    EmptyGroupPolicy,
    EmptyInputPolicy,
    IsNull,
    Or,
    Request,
    Semantics,
)
from sharedscan.planner import BlockingReasons, build_shared_plan  # noqa: E402
from sharedscan.interpreter import execute_request, execute_shared_plan  # noqa: E402
from sharedscan.verify import type_strict_equal, verify_plan  # noqa: E402
from synth_data import build_catalog  # noqa: E402


def _req(name, aggs, *, where=None, having=None, partitions=("p1", "p2", "p3"),
         group_keys=("region",), semantics=None, key_domain=()):
    return Request(
        name=name, table="sales", partitions=partitions, group_keys=group_keys,
        aggs=aggs, where=where, having=having,
        semantics=semantics or Semantics(), key_domain=key_domain,
    )


def _bag(result):
    return {row: result.rows.count(row) for row in result.rows}


class TestEndToEndEquivalence(unittest.TestCase):
    def setUp(self):
        self.catalog = build_catalog(exclusive_cust=False)

    def _check(self, requests, catalog=None):
        catalog = catalog or self.catalog
        plan = build_shared_plan(requests, catalog)
        report = verify_plan(requests, plan, catalog)
        report.raise_if_failed()
        self.assertEqual(report.compared, len(requests))
        return plan

    def test_basic_grouped_sharing_with_concrete_values(self):
        r1 = _req("r1", (Agg("n", AggFn.COUNT_STAR, None),
                         Agg("s", AggFn.SUM, "amount")))
        r2 = _req("r2", (Agg("n", AggFn.COUNT_STAR, None),
                         Agg("c", AggFn.COUNT, "amount")))
        plan = self._check([r1, r2])

        shared = execute_shared_plan(plan, self.catalog)
        bag1 = {row[0]: (row[1], row[2]) for row in shared["r1"].rows}
        # 全量数据按 region（amount 缺失不计入 sum/count）
        self.assertEqual(bag1["east"], (4, 80))    # p1: 10+20, p3: 40+10
        self.assertEqual(bag1["west"], (3, 20))    # p1 NULL+5+15
        self.assertEqual(bag1["north"], (2, 30))   # p3 NULL + p2 30
        self.assertEqual(bag1[None], (1, 7))
        # 共享执行与参考执行结果一致
        ref = execute_request(r1, self.catalog)
        self.assertEqual(_bag(ref), _bag(shared["r1"]))

    def test_avg_sum_count_state_reuse_end_to_end(self):
        r1 = _req("r1", (Agg("a", AggFn.AVG, "amount"),))
        r2 = _req("r2", (Agg("s", AggFn.SUM, "amount"),
                         Agg("c", AggFn.COUNT, "amount")))
        plan = self._check([r1, r2])
        # AVG 没有自己的状态种类
        self.assertNotIn("avg", {spec.kind for spec in plan.state_specs.values()})

        shared = execute_shared_plan(plan, self.catalog)
        ref = execute_request(r1, self.catalog)
        for ref_row, got_row in zip(sorted(ref.rows, key=str),
                                    sorted(shared["r1"].rows, key=str)):
            self.assertEqual(ref_row[0], got_row[0])
            if got_row[1] is not None:
                self.assertIsInstance(got_row[1], float)

    def test_different_where_conditional_aggregation(self):
        r1 = _req("r1", (Agg("n", AggFn.COUNT_STAR, None),),
                  where=Cmp("amount", ">", 10))
        r2 = _req("r2", (Agg("n", AggFn.COUNT_STAR, None),),
                  where=Or(Cmp("region", "=", "north"), IsNull("region")))
        self._check([r1, r2])

        # 具体期望：r1 只统计 amount>10 的行（NULL amount 的行被 UNKNOWN 过滤）
        ref1 = execute_request(r1, self.catalog)
        bag = {row[0]: row[1] for row in ref1.rows}
        # east: 20/40 共 2 行（两个 10 不满足 >10）；west: 15；north: 30
        self.assertEqual(bag, {"east": 2, "west": 1, "north": 1})

    def test_where_then_having_order_preserved(self):
        # WHERE 在聚合前、HAVING 在聚合后，两者不可互换
        r1 = _req(
            "r1",
            (Agg("n", AggFn.COUNT_STAR, None),),
            where=Cmp("amount", ">", 5),
            having=Cmp("n", ">=", 2),
        )
        self._check([r1])
        ref = execute_request(r1, self.catalog)
        # WHERE 后各组计数：east 4, west 1(15), north 1(30), NULL键1(7)
        # HAVING n>=2 只留 east
        self.assertEqual([row[0] for row in ref.rows], ["east"])

    def test_grand_total_empty_input_policies(self):
        one = _req("one", (Agg("n", AggFn.COUNT_STAR, None),
                           Agg("s", AggFn.SUM, "amount"),
                           Agg("a", AggFn.AVG, "amount")),
                   partitions=("p4",), group_keys=(),
                   semantics=Semantics(empty_input=EmptyInputPolicy.ONE_NULL_ROW))
        zero = _req("zero", (Agg("n", AggFn.COUNT_STAR, None),),
                    partitions=("p4",), group_keys=(),
                    semantics=Semantics(empty_input=EmptyInputPolicy.ZERO_ROWS))
        self._check([one, zero])
        plan = build_shared_plan([one, zero], self.catalog)
        shared = execute_shared_plan(plan, self.catalog)
        self.assertEqual(shared["one"].rows, [(0, None, None)])
        self.assertEqual(shared["zero"].rows, [])

    def test_keep_domain_equivalence_with_null_and_missing_groups(self):
        domain = (("east",), ("west",), ("north",), (None,))
        r1 = _req(
            "r1",
            (Agg("n", AggFn.COUNT_STAR, None), Agg("s", AggFn.SUM, "amount")),
            partitions=("p1",),
            semantics=Semantics(empty_group=EmptyGroupPolicy.KEEP_DOMAIN),
            key_domain=domain,
        )
        r2 = _req("r2", (Agg("n", AggFn.COUNT_STAR, None),),
                  partitions=("p1",),
                  semantics=Semantics(empty_group=EmptyGroupPolicy.DROP))
        self._check([r1, r2])
        plan = build_shared_plan([r1, r2], self.catalog)
        shared = execute_shared_plan(plan, self.catalog)
        bag = {row[0]: (row[1], row[2]) for row in shared["r1"].rows}
        # north 缺席 -> 补齐空聚合值
        self.assertEqual(bag["north"], (0, None))
        # DROP 策略不含 north
        self.assertNotIn("north", {row[0] for row in shared["r2"].rows})

    def test_partition_subset_scoping(self):
        # 同谓词、不同分区子集，结果必须各自正确，不能串分区
        r1 = _req("r1", (Agg("n", AggFn.COUNT_STAR, None),),
                  partitions=("p1",))
        r2 = _req("r2", (Agg("n", AggFn.COUNT_STAR, None),),
                  partitions=("p1", "p2"))
        self._check([r1, r2])
        plan = build_shared_plan([r1, r2], self.catalog)
        shared = execute_shared_plan(plan, self.catalog)
        self.assertEqual(sum(row[1] for row in shared["r1"].rows), 4)
        self.assertEqual(sum(row[1] for row in shared["r2"].rows), 7)

    def test_distinct_count_single_partition_equivalence(self):
        r1 = _req("r1", (Agg("d", AggFn.COUNT_DISTINCT, "cust_id"),
                         Agg("d0", AggFn.COUNT_DISTINCT, "cust_id",
                             include_null=False)),
                  partitions=("p1",))
        self._check([r1])
        plan = build_shared_plan([r1], self.catalog)
        shared = execute_shared_plan(plan, self.catalog)
        bag = {row[0]: (row[1], row[2]) for row in shared["r1"].rows}
        # p1: east {c1,c2} -> (2,2)；west {c1} -> (1,1)；NULL 键 {NULL} -> (1,0)
        self.assertEqual(bag["east"], (2, 2))
        self.assertEqual(bag[None], (1, 0))

    def test_distinct_count_exclusive_multi_partition_equivalence(self):
        catalog = build_catalog(exclusive_cust=True)
        r1 = _req("r1", (Agg("d", AggFn.COUNT_DISTINCT, "cust_id"),),
                  partitions=("p1", "p2"))
        plan = self._check([r1], catalog=catalog)
        shared = execute_shared_plan(plan, catalog)
        # p1 去重 {c1,c2,NULL}、p2 去重 {c3,NULL}；互斥标示下计数直接相加
        bag = {row[0]: row[1] for row in shared["r1"].rows}
        self.assertEqual(bag["east"], 2)   # p1: c1,c2
        # west: p1 的 {c1} + p2 的 {c3}（p2 west 的 cust_id 均非空）= 2
        self.assertEqual(bag["west"], 2)
        self.assertEqual(bag["north"], 1)  # p2: NULL -> 1
        self.assertEqual(bag[None], 1)     # p1 NULL 键行的 NULL cust_id

    def test_non_exclusive_distinct_is_blocked_not_silently_wrong(self):
        r1 = _req("r1", (Agg("d", AggFn.COUNT_DISTINCT, "cust_id"),),
                  partitions=("p1", "p3"))  # c1 跨两分区重复
        with self.assertRaises(BlockingReasons) as ctx:
            build_shared_plan([r1], self.catalog)
        self.assertEqual(
            [r.code for r in ctx.exception.reasons],
            ["DISTINCT_MERGE_NOT_EXCLUSIVE"],
        )

    def test_type_strict_equality_helper(self):
        self.assertTrue(type_strict_equal(1, 1))
        self.assertFalse(type_strict_equal(1, 1.0))
        self.assertFalse(type_strict_equal(1, True))
        self.assertTrue(type_strict_equal(None, None))
        self.assertFalse(type_strict_equal(None, 0))

    def test_int_vs_float_output_families_distinguished(self):
        # SUM(int) 保持 int；AVG(int) 输出 float；两者类型族必须被区分
        r1 = _req("r1", (Agg("s", AggFn.SUM, "amount"),
                         Agg("a", AggFn.AVG, "amount")))
        self._check([r1])
        ref = execute_request(r1, self.catalog)
        east = next(row for row in ref.rows if row[0] == "east")
        self.assertIsInstance(east[1], int)
        self.assertIsInstance(east[2], float)

    def test_mixed_semantics_share_one_scan(self):
        # 空组策略/HAVING/WHERE/分区子集全不同的分组请求仍能进同一共享计划。
        # 注意：grand-total（无分组键）与分组请求分组键集合不同，按规则阻塞，
        # 因此这里不放无分组键请求。
        r1 = _req("r1", (Agg("n", AggFn.COUNT_STAR, None),),
                  where=Cmp("flag", "=", True),
                  having=Cmp("n", ">", 0))
        r2 = _req("r2", (Agg("n", AggFn.COUNT_STAR, None),
                         Agg("s", AggFn.SUM, "amount")),
                  partitions=("p1",),
                  semantics=Semantics(empty_group=EmptyGroupPolicy.KEEP_DOMAIN),
                  key_domain=(("east",), ("north",)))
        plan = self._check([r1, r2])
        self.assertEqual(plan.partitions, ("p1", "p2", "p3"))

    def test_grand_total_cannot_share_with_grouped_request(self):
        # 无分组键 vs 有分组键：分组键集合不一致，定位为 GROUP_KEY_MISMATCH
        grouped = _req("grouped", (Agg("n", AggFn.COUNT_STAR, None),),
                       group_keys=("region",))
        total = _req("total", (Agg("n", AggFn.COUNT_STAR, None),),
                     group_keys=())
        with self.assertRaises(BlockingReasons) as ctx:
            build_shared_plan([grouped, total], self.catalog)
        self.assertIn("GROUP_KEY_MISMATCH",
                      {r.code for r in ctx.exception.reasons})

    def test_compound_and_or_where_equivalence(self):
        pred = And(
            Cmp("amount", ">=", 10),
            Or(Cmp("region", "=", "east"), Cmp("region", "=", "north")),
        )
        r1 = _req("r1", (Agg("n", AggFn.COUNT_STAR, None),
                         Agg("s", AggFn.SUM, "amount")) , where=pred)
        self._check([r1])

    def test_null_key_distinct_from_domain_filled_group(self):
        # NULL 键是真实组；域中的 'ghost' 才是缺席补齐组，二者不得混淆
        r1 = _req(
            "r1", (Agg("n", AggFn.COUNT_STAR, None),),
            partitions=("p1",),
            semantics=Semantics(empty_group=EmptyGroupPolicy.KEEP_DOMAIN),
            key_domain=((None,), ("ghost",)),
        )
        self._check([r1])
        plan = build_shared_plan([r1], self.catalog)
        shared = execute_shared_plan(plan, self.catalog)["r1"]
        bag = {row[0]: row[1] for row in shared.rows}
        self.assertEqual(bag[None], 1)       # 真实 NULL 键组
        self.assertEqual(bag["ghost"], 0)    # 域补齐的缺席组


if __name__ == "__main__":
    unittest.main()
