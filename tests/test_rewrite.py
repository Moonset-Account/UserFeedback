"""相关标量子查询 -> LEFT JOIN 改写测试。"""

import unittest

from querybind import (
    Executor,
    bind,
    col,
    eq,
    lit,
    rewrite_query,
)
from querybind.model import (
    DerivedTable,
    FuncCall,
    Join,
    SelectNode,
    SubqueryExpr,
    TableRef,
)

from tests._fixtures import QueryBindTestCase


def _scalar_bonus(inner_alias="p", outer_alias="e", table="pay"):
    return SelectNode(
        projections=[col("bonus", table=inner_alias, alias="bonus")],
        from_=TableRef(name=table, alias=inner_alias),
        where=eq(col("emp_id", table=inner_alias),
                  col("id", table=outer_alias)),
    )


class DecorrelationTests(QueryBindTestCase):
    def setUp(self):
        super().setUp()
        self.executor = Executor(self.catalog)

    def test_unique_inner_key_is_rewritten_to_left_join(self):
        top = SelectNode(
            projections=[
                col("id", table="e", alias="eid"),
                SubqueryExpr(query=_scalar_bonus(), alias="b"),
            ],
            from_=TableRef(name="emp", alias="e"),
        )
        outcome = rewrite_query(top, self.catalog)
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(outcome.skipped, [])
        new_from = outcome.bound_after.from_
        self.assertIsInstance(new_from, Join)
        self.assertEqual(new_from.join_type, "left")
        self.assertIsInstance(new_from.right, DerivedTable)
        self.assertEqual(new_from.right.alias, outcome.join_aliases[0])
        # 投影中的子查询已替换为普通列引用
        replacement = outcome.bound_after.projections[1]
        self.assertNotIsInstance(replacement, SubqueryExpr)
        self.assertEqual(replacement.resolved.qualifier,
                         outcome.join_aliases[0])

    def test_rewritten_and_original_produce_identical_results(self):
        top = SelectNode(
            projections=[
                col("id", table="e", alias="eid"),
                SubqueryExpr(query=_scalar_bonus(), alias="b"),
            ],
            from_=TableRef(name="emp", alias="e"),
        )
        original = bind(top, self.catalog)
        outcome = rewrite_query(top, self.catalog)
        r1 = self.executor.execute(original)
        r2 = self.executor.execute(outcome.bound_after)
        self.assertEqual(r1.names(), r2.names())
        self.assertEqual(r1.ordered_rows(), r2.ordered_rows())

    def test_zero_matches_still_emits_null(self):
        # emp 13 的 dept_id=4，且 pay 中没有 emp_id=13
        top = SelectNode(
            projections=[
                col("id", table="e", alias="eid"),
                SubqueryExpr(query=_scalar_bonus(), alias="b"),
            ],
            from_=TableRef(name="emp", alias="e"),
        )
        outcome = rewrite_query(top, self.catalog)
        rows = self.executor.execute(outcome.bound_after).ordered_rows()
        by_id = {r[0]: r[1] for r in rows}
        self.assertIsNone(by_id[13])
        self.assertEqual(by_id[10], 5)
        self.assertEqual(by_id[12], 7)
        # 行数与左表一致，未因连接放大或丢行
        self.assertEqual(len(rows), 4)

    def test_non_unique_inner_key_blocks_rewrite(self):
        top = SelectNode(
            projections=[
                col("id", table="e", alias="eid"),
                SubqueryExpr(
                    query=_scalar_bonus(table="pay_dup"), alias="b"),
            ],
            from_=TableRef(name="emp", alias="e"),
        )
        outcome = rewrite_query(top, self.catalog)
        self.assertEqual(outcome.applied, 0)
        self.assertTrue(any("unique" in s for s in outcome.skipped))
        # 未改写时投影仍是子查询
        self.assertIsInstance(outcome.bound_after.projections[1], SubqueryExpr)

    def test_non_correlated_subquery_is_not_rewritten(self):
        standalone = SelectNode(
            projections=[lit(42, alias="answer")],
            from_=TableRef(name="pay", alias="p"),
        )
        top = SelectNode(
            projections=[
                col("id", table="e", alias="eid"),
                SubqueryExpr(query=standalone, alias="c"),
            ],
            from_=TableRef(name="emp", alias="e"),
        )
        outcome = rewrite_query(top, self.catalog)
        self.assertEqual(outcome.applied, 0)
        self.assertIsInstance(outcome.bound_after.projections[1], SubqueryExpr)

    def test_inner_only_filter_stays_inside_derived_table(self):
        scalar = SelectNode(
            projections=[col("bonus", table="p", alias="bonus")],
            from_=TableRef(name="pay", alias="p"),
            where=FuncCall("and", [
                eq(col("emp_id", table="p"), col("id", table="e")),
                FuncCall(">", [col("bonus", table="p"), lit(6)]),
            ]),
        )
        top = SelectNode(
            projections=[
                col("id", table="e", alias="eid"),
                SubqueryExpr(query=scalar, alias="b"),
            ],
            from_=TableRef(name="emp", alias="e"),
        )
        outcome = rewrite_query(top, self.catalog)
        self.assertEqual(outcome.applied, 1)
        derived = outcome.bound_after.from_.right
        # bonus > 6 下推到派生表内部
        where = derived.query.where
        self.assertIsNotNone(where)
        text = repr(where)
        self.assertIn("bonus", text)
        # 连接 ON 只含相关等值（键列）
        on = outcome.bound_after.from_.on
        self.assertEqual(on.name, "=")

        original = bind(top, self.catalog)
        r1 = self.executor.execute(original).ordered_rows()
        r2 = self.executor.execute(outcome.bound_after).ordered_rows()
        self.assertEqual(r1, r2)
        # emp 10 的 bonus=5 被过滤 -> NULL；emp 12 bonus=7 保留
        by_id = {r[0]: r[1] for r in r2}
        self.assertIsNone(by_id[10])
        self.assertEqual(by_id[12], 7)

    def test_residual_predicate_goes_to_join_on(self):
        # 残余谓词引用外层列，无法下推，必须进入 ON（否则零匹配补空语义改变）
        scalar = SelectNode(
            projections=[col("bonus", table="p", alias="bonus")],
            from_=TableRef(name="pay", alias="p"),
            where=FuncCall("and", [
                eq(col("emp_id", table="p"), col("id", table="e")),
                FuncCall("=", [
                    col("emp_id", table="p"),
                    col("id", table="e"),
                ]),
            ]),
        )
        top = SelectNode(
            projections=[
                col("id", table="e", alias="eid"),
                SubqueryExpr(query=scalar, alias="b"),
            ],
            from_=TableRef(name="emp", alias="e"),
        )
        outcome = rewrite_query(top, self.catalog)
        # 两个相同等值：一个被识别为相关键，另一个作为残余进 ON
        self.assertEqual(outcome.applied, 1)
        on = outcome.bound_after.from_.on
        self.assertEqual(on.name, "and")

    def test_rewrite_does_not_mutate_input_tree(self):
        top = SelectNode(
            projections=[
                col("id", table="e", alias="eid"),
                SubqueryExpr(query=_scalar_bonus(), alias="b"),
            ],
            from_=TableRef(name="emp", alias="e"),
        )
        snapshot = repr(top)
        rewrite_query(top, self.catalog)
        self.assertEqual(repr(top), snapshot)
        # 原树仍未绑定
        self.assertIsNone(top.projections[1].query.nid)

    def test_derived_table_unique_key_propagates_and_enables_rewrite(self):
        # 内层先对 pay 做派生表，唯一键 emp_id 应向上传播
        derived_pay = DerivedTable(
            query=SelectNode(
                projections=[
                    col("emp_id", table="p", alias="emp_id"),
                    col("bonus", table="p", alias="bonus"),
                ],
                from_=TableRef(name="pay", alias="p"),
            ),
            alias="dp",
        )
        scalar = SelectNode(
            projections=[col("bonus", table="dp", alias="bonus")],
            from_=derived_pay,
            where=eq(col("emp_id", table="dp"), col("id", table="e")),
        )
        top = SelectNode(
            projections=[
                col("id", table="e", alias="eid"),
                SubqueryExpr(query=scalar, alias="b"),
            ],
            from_=TableRef(name="emp", alias="e"),
        )
        outcome = rewrite_query(top, self.catalog)
        self.assertEqual(outcome.applied, 1, outcome.skipped)
        r1 = self.executor.execute(bind(top, self.catalog)).ordered_rows()
        r2 = self.executor.execute(outcome.bound_after).ordered_rows()
        self.assertEqual(r1, r2)


    def test_generated_join_alias_avoids_user_aliases(self):
        from querybind import Catalog, TableData
        catalog = Catalog([
            TableData("pay", ["id", "v"], [
                {"id": 1, "v": 9},
            ], [("id",)]),
            TableData("emp", ["id"], [
                {"id": 1},
            ], [("id",)]),
        ])
        scalar = SelectNode(
            projections=[col("v", table="p", alias="v")],
            from_=TableRef(name="pay", alias="p"),
            where=eq(col("id", table="p"), col("id", table="e")),
        )
        top = SelectNode(
            projections=[SubqueryExpr(query=scalar, alias="b")],
            # 用户在外层占用了生成器会首选的别名 __dq_1
            from_=Join(
                join_type="inner",
                left=TableRef(name="emp", alias="e"),
                right=TableRef(name="pay", alias="__dq_1"),
                on=eq(col("id", table="e"), col("id", table="__dq_1")),
            ),
        )
        outcome = rewrite_query(top, catalog)
        self.assertEqual(outcome.applied, 1)
        self.assertNotIn("__dq_1", outcome.join_aliases)
        self.assertEqual(outcome.join_aliases, ["__dq_2"])


if __name__ == "__main__":
    unittest.main()
