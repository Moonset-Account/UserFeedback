"""谓词三值逻辑与参考解释器的基础语义测试。"""

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
    IsNotNull,
    IsNull,
    Or,
    Request,
    Semantics,
)
from sharedscan.predicates import eval_predicate, row_passes  # noqa: E402
from sharedscan.interpreter import execute_request  # noqa: E402
from synth_data import build_catalog  # noqa: E402


class TestThreeValuedLogic(unittest.TestCase):
    def setUp(self):
        self.row = {"a": 1, "b": None, "s": "x", "flag": True}

    def test_null_comparison_is_unknown(self):
        self.assertIsNone(eval_predicate(Cmp("b", "=", 1), self.row))
        self.assertIsNone(eval_predicate(Cmp("b", "<", 1), self.row))
        # UNKNOWN 不放行
        self.assertFalse(row_passes(Cmp("b", "=", 1), self.row))

    def test_is_null_is_strict_boolean(self):
        self.assertTrue(eval_predicate(IsNull("b"), self.row))
        self.assertFalse(eval_predicate(IsNotNull("b"), self.row))
        self.assertTrue(eval_predicate(IsNotNull("a"), self.row))

    def test_and_or_kleene_truth_tables(self):
        unknown = Cmp("b", "=", 1)
        true = Cmp("a", "=", 1)
        false = Cmp("a", "=", 2)
        self.assertTrue(eval_predicate(And(true, true), self.row))
        self.assertFalse(eval_predicate(And(true, false), self.row))
        self.assertIsNone(eval_predicate(And(true, unknown), self.row))
        self.assertFalse(eval_predicate(And(false, unknown), self.row))
        self.assertTrue(eval_predicate(Or(false, true), self.row))
        self.assertIsNone(eval_predicate(Or(false, unknown), self.row))
        self.assertTrue(eval_predicate(Or(unknown, true), self.row))
        self.assertFalse(eval_predicate(Or(false, false), self.row))

    def test_type_families_do_not_mix(self):
        # 字符串与数字不可比较 -> UNKNOWN
        self.assertIsNone(eval_predicate(Cmp("s", "=", 1), self.row))
        # bool 与 int 不混比
        self.assertIsNone(eval_predicate(Cmp("flag", "=", 1), self.row))
        self.assertTrue(eval_predicate(Cmp("flag", "=", True), self.row))
        # int 与 float 同属数值族，可比较
        self.assertTrue(eval_predicate(Cmp("a", "=", 1.0), self.row))


class TestReferenceInterpreter(unittest.TestCase):
    def setUp(self):
        self.catalog = build_catalog(exclusive_cust=False)

    def _req(self, **kwargs):
        defaults = dict(
            name="r",
            table="sales",
            partitions=("p1",),
            group_keys=(),
            aggs=(Agg("n", AggFn.COUNT_STAR, None),),
        )
        defaults.update(kwargs)
        return Request(**defaults)

    def test_grand_total_basic_aggregates(self):
        req = self._req(
            aggs=(
                Agg("n", AggFn.COUNT_STAR, None),
                Agg("c", AggFn.COUNT, "amount"),
                Agg("s", AggFn.SUM, "amount"),
                Agg("mn", AggFn.MIN, "amount"),
                Agg("mx", AggFn.MAX, "amount"),
            )
        )
        result = execute_request(req, self.catalog)
        self.assertEqual(result.columns, ("n", "c", "s", "mn", "mx"))
        # p1 四行；amount 三个非空(10,20,7)；sum=37；min=7 max=20
        self.assertEqual(result.rows, [(4, 3, 37, 7, 20)])

    def test_avg_is_float_and_ignores_nulls(self):
        req = self._req(aggs=(Agg("avg_amt", AggFn.AVG, "amount"),))
        result = execute_request(req, self.catalog)
        # (10+20+7)/3 = 37/3
        self.assertAlmostEqual(result.rows[0][0], 37 / 3)
        self.assertEqual(result.column_families["avg_amt"], {"float"})

    def test_count_distinct_with_and_without_null(self):
        with_null = self._req(aggs=(Agg("d", AggFn.COUNT_DISTINCT, "cust_id",
                                        include_null=True),))
        without_null = self._req(aggs=(Agg("d", AggFn.COUNT_DISTINCT, "cust_id",
                                           include_null=False),))
        # p1 cust_id: c1,c2,c1,NULL -> 去重 {c1,c2,NULL}=3；忽略 NULL -> 2
        self.assertEqual(execute_request(with_null, self.catalog).rows, [(3,)])
        self.assertEqual(execute_request(without_null, self.catalog).rows, [(2,)])

    def test_group_by_including_null_key(self):
        req = self._req(
            group_keys=("region",),
            aggs=(Agg("n", AggFn.COUNT_STAR, None),),
        )
        result = execute_request(req, self.catalog)
        bag = {row[0]: row[1] for row in result.rows}
        self.assertEqual(bag, {"east": 2, "west": 1, None: 1})
        # region 列同时观察到字符串族与 NULL(None) 族
        self.assertEqual(result.column_families["region"], {"str", None})

    def test_empty_input_policies(self):
        one_null = self._req(
            partitions=("p4",),
            semantics=Semantics(empty_input=EmptyInputPolicy.ONE_NULL_ROW),
            aggs=(Agg("n", AggFn.COUNT_STAR, None), Agg("s", AggFn.SUM, "amount")),
        )
        zero = self._req(
            partitions=("p4",),
            semantics=Semantics(empty_input=EmptyInputPolicy.ZERO_ROWS),
        )
        r1 = execute_request(one_null, self.catalog)
        r2 = execute_request(zero, self.catalog)
        self.assertEqual(r1.rows, [(0, None)])
        self.assertEqual(r2.rows, [])

    def test_keep_domain_fills_missing_groups(self):
        req = self._req(
            partitions=("p1",),
            group_keys=("region",),
            aggs=(Agg("n", AggFn.COUNT_STAR, None),
                  Agg("s", AggFn.SUM, "amount")),
            semantics=Semantics(empty_group=EmptyGroupPolicy.KEEP_DOMAIN),
            key_domain=(("east",), ("west",), ("north",), (None,)),
        )
        result = execute_request(req, self.catalog)
        bag = {row[0]: (row[1], row[2]) for row in result.rows}
        # north 在 p1 缺席 -> 空组值 (0, NULL)
        self.assertEqual(bag["north"], (0, None))
        self.assertEqual(bag["east"], (2, 30))
        # NULL 键在 p1 实际存在，不是补齐行
        self.assertEqual(bag[None], (1, 7))

    def test_where_unknown_rows_are_filtered(self):
        req = self._req(
            where=Cmp("amount", ">", 0),
            aggs=(Agg("n", AggFn.COUNT_STAR, None),),
        )
        # amount NULL 的行比较为 UNKNOWN，被过滤；剩 3 行
        self.assertEqual(execute_request(req, self.catalog).rows, [(3,)])

    def test_having_runs_after_grouping(self):
        req = self._req(
            group_keys=("region",),
            aggs=(Agg("n", AggFn.COUNT_STAR, None),),
            having=Cmp("n", ">=", 2),
        )
        result = execute_request(req, self.catalog)
        self.assertEqual([row[0] for row in result.rows], ["east"])

    def test_having_can_filter_domain_filled_rows(self):
        req = self._req(
            partitions=("p1",),
            group_keys=("region",),
            aggs=(Agg("n", AggFn.COUNT_STAR, None),),
            semantics=Semantics(empty_group=EmptyGroupPolicy.KEEP_DOMAIN),
            # north 在 p1 缺席，是唯一的补齐行
            key_domain=(("east",), ("north",)),
            having=Cmp("n", ">", 0),
        )
        result = execute_request(req, self.catalog)
        # north 补齐行 n=0 被 HAVING 过滤；真实组（east/west/NULL 键）全部保留
        keys = sorted((row[0] for row in result.rows), key=lambda x: str(x))
        self.assertEqual(keys, [None, "east", "west"])

    def test_or_predicate(self):
        req = self._req(
            where=Or(Cmp("region", "=", "west"), Cmp("region", "=", "north")),
            aggs=(Agg("n", AggFn.COUNT_STAR, None),),
        )
        self.assertEqual(execute_request(req, self.catalog).rows, [(1,)])

    def test_nested_and_or(self):
        req = self._req(
            where=And(
                Cmp("amount", ">=", 10),
                Or(Cmp("region", "=", "east"), IsNotNull("cust_id")),
            ),
            aggs=(Agg("n", AggFn.COUNT_STAR, None),),
        )
        # p1: 行1 east/10 通过; 行2 east/20 通过; 行3 west/NULL amount 被第一层拦;
        # 行4 amount=7 被第一层拦
        self.assertEqual(execute_request(req, self.catalog).rows, [(2,)])


if __name__ == "__main__":
    unittest.main()
