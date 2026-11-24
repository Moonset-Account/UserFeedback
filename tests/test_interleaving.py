"""交错与边界场景：复合唯一键、多个相关子查询、USING 限定原列。"""

import unittest

from querybind import (
    Catalog,
    Executor,
    TableData,
    bind,
    col,
    eq,
    rewrite_query,
)
from querybind.model import (
    FuncCall,
    Join,
    SelectNode,
    SubqueryExpr,
    TableRef,
)


def _catalog_with_composite_key():
    return Catalog([
        TableData(
            "orders",
            ["tenant", "seq", "amount"],
            [
                {"tenant": "t1", "seq": 1, "amount": 100},
                {"tenant": "t1", "seq": 2, "amount": 200},
                {"tenant": "t2", "seq": 1, "amount": 300},
            ],
            unique_keys=[("tenant", "seq")],
        ),
        TableData(
            "header",
            ["tenant", "seq", "label"],
            [
                {"tenant": "t1", "seq": 1, "label": "first-t1"},
                {"tenant": "t1", "seq": 2, "label": "second-t1"},
                {"tenant": "t2", "seq": 1, "label": "first-t2"},
            ],
            unique_keys=[("tenant", "seq")],
        ),
    ])


class CompositeKeyRewriteTests(unittest.TestCase):
    def setUp(self):
        self.catalog = _catalog_with_composite_key()
        self.ex = Executor(self.catalog)

    def test_composite_unique_key_correlation_rewrites(self):
        scalar = SelectNode(
            projections=[col("label", table="h", alias="label")],
            from_=TableRef(name="header", alias="h"),
            where=FuncCall("and", [
                eq(col("tenant", table="h"), col("tenant", table="o")),
                eq(col("seq", table="h"), col("seq", table="o")),
            ]),
        )
        top = SelectNode(
            projections=[
                col("tenant", table="o", alias="tenant"),
                col("seq", table="o", alias="seq"),
                SubqueryExpr(query=scalar, alias="lbl"),
            ],
            from_=TableRef(name="orders", alias="o"),
        )
        outcome = rewrite_query(top, self.catalog)
        self.assertEqual(outcome.applied, 1, outcome.skipped)
        # 派生表应输出两个键列 + 一个值列
        derived = outcome.bound_after.from_.right
        self.assertEqual(
            [c.name for c in derived.query.report.outputs],
            ["k1", "k2", "label"],
        )
        r1 = self.ex.execute(bind(top, self.catalog)).ordered_rows()
        r2 = self.ex.execute(outcome.bound_after).ordered_rows()
        self.assertEqual(r1, r2)
        self.assertEqual(
            r2,
            [["t1", 1, "first-t1"],
             ["t1", 2, "second-t1"],
             ["t2", 1, "first-t2"]],
        )

    def test_single_column_of_composite_key_is_not_enough(self):
        # 只用 tenant 相关：单列无法证明 (tenant, seq) 唯一，禁止改写
        scalar = SelectNode(
            projections=[col("label", table="h", alias="label")],
            from_=TableRef(name="header", alias="h"),
            where=eq(col("tenant", table="h"), col("tenant", table="o")),
        )
        top = SelectNode(
            projections=[SubqueryExpr(query=scalar, alias="lbl")],
            from_=TableRef(name="orders", alias="o"),
        )
        outcome = rewrite_query(top, self.catalog)
        self.assertEqual(outcome.applied, 0)
        self.assertTrue(any("unique" in s for s in outcome.skipped))


class MultipleScalarSubqueryTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog([
            TableData("emp", ["id", "name"], [
                {"id": 1, "name": "A"}, {"id": 2, "name": "B"},
            ], [("id",)]),
            TableData("pay", ["emp_id", "bonus"], [
                {"emp_id": 1, "bonus": 5},
            ], [("emp_id",)]),
            TableData("tag", ["emp_id", "tag"], [
                {"emp_id": 1, "tag": "vip"},
                {"emp_id": 2, "tag": "new"},
            ], [("emp_id",)]),
        ])
        self.ex = Executor(self.catalog)

    def test_two_correlated_scalars_both_lifted(self):
        bonus_sq = SelectNode(
            projections=[col("bonus", table="p", alias="bonus")],
            from_=TableRef(name="pay", alias="p"),
            where=eq(col("emp_id", table="p"), col("id", table="e")),
        )
        tag_sq = SelectNode(
            projections=[col("tag", table="t", alias="tag")],
            from_=TableRef(name="tag", alias="t"),
            where=eq(col("emp_id", table="t"), col("id", table="e")),
        )
        top = SelectNode(
            projections=[
                col("id", table="e", alias="eid"),
                SubqueryExpr(query=bonus_sq, alias="b"),
                SubqueryExpr(query=tag_sq, alias="g"),
            ],
            from_=TableRef(name="emp", alias="e"),
        )
        outcome = rewrite_query(top, self.catalog)
        self.assertEqual(outcome.applied, 2)
        self.assertEqual(len(outcome.join_aliases), 2)
        # 两次提升形成嵌套左连接
        outer_join = outcome.bound_after.from_
        self.assertEqual(outer_join.join_type, "left")
        self.assertIsInstance(outer_join.left, Join)
        self.assertEqual(outer_join.left.join_type, "left")

        r1 = self.ex.execute(bind(top, self.catalog)).ordered_rows()
        r2 = self.ex.execute(outcome.bound_after).ordered_rows()
        self.assertEqual(r1, r2)
        # emp 2 在 pay 中零匹配 -> NULL，tag 仍有值
        self.assertEqual(r2, [[1, 5, "vip"], [2, None, "new"]])


class UsingQualifiedOriginalColumnTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog([
            TableData("a", ["id", "v1"], [
                {"id": 1, "v1": "x"}, {"id": 2, "v1": "y"},
            ], [("id",)]),
            TableData("b", ["id", "v2"], [
                {"id": 2, "v2": "m"}, {"id": 3, "v2": "n"},
            ], [("id",)]),
        ])
        self.ex = Executor(self.catalog)

    def test_full_outer_qualified_columns_keep_original_values(self):
        q = SelectNode(
            projections=[
                col("id", alias="merged_id"),
                col("id", table="a", alias="a_id"),
                col("id", table="b", alias="b_id"),
                col("v1", table="a", alias="v1"),
                col("v2", table="b", alias="v2"),
            ],
            from_=Join(
                join_type="full",
                left=TableRef(name="a", alias="a"),
                right=TableRef(name="b", alias="b"),
                using=["id"],
            ),
        )
        bound = bind(q, self.catalog)
        r = self.ex.execute(bound)
        rows = {row[0]: row[1:] for row in r.ordered_rows()}
        # 合并列、左原列、右原列在仅左/仅右行上各自正确
        self.assertEqual(rows[1], [1, None, "x", None])
        self.assertEqual(rows[2], [2, 2, "y", "m"])
        self.assertEqual(rows[3], [None, 3, None, "n"])

    def test_using_merged_column_appears_once_in_star(self):
        q = SelectNode(
            projections=[],
            from_=Join(
                join_type="inner",
                left=TableRef(name="a", alias="a"),
                right=TableRef(name="b", alias="b"),
                using=["id"],
            ),
        )
        from querybind.model import star
        q.projections = [star()]
        bound = bind(q, self.catalog)
        names = [p.alias for p in bound.projections]
        self.assertEqual(names.count("id"), 1)
        self.assertEqual(names, ["id", "v1", "v2"])


class JoinKeyPropagationTests(unittest.TestCase):
    def test_unique_keys_propagate_through_inner_join_chain(self):
        catalog = Catalog([
            TableData("a", ["id", "x"], [{"id": 1, "x": 1}], [("id",)]),
            TableData("b", ["id", "a_id", "y"], [
                {"id": 10, "a_id": 1, "y": 2}
            ], [("id",), ("a_id",)]),
            TableData("c", ["id", "b_id", "z"], [
                {"id": 20, "b_id": 10, "z": 3}
            ], [("id",), ("b_id",)]),
        ])
        q = SelectNode(
            projections=[col("id", table="a")],
            from_=Join(
                join_type="inner",
                left=Join(
                    join_type="inner",
                    left=TableRef(name="a", alias="a"),
                    right=TableRef(name="b", alias="b"),
                    on=eq(col("id", table="a"), col("a_id", table="b")),
                ),
                right=TableRef(name="c", alias="c"),
                on=eq(col("id", table="b"), col("b_id", table="c")),
            ),
        )
        bound = bind(q, catalog)
        refs = {
            part for key in bound.report.scope.unique_keys
            for key_col in key for part in key_col
        }
        self.assertIn(("a", "id"), refs)
        self.assertIn(("b", "id"), refs)
        self.assertIn(("c", "id"), refs)

    def test_cross_join_drops_all_unique_keys(self):
        catalog = Catalog([
            TableData("a", ["id"], [{"id": 1}], [("id",)]),
            TableData("b", ["id"], [{"id": 1}], [("id",)]),
        ])
        q = SelectNode(
            projections=[col("id", table="a")],
            from_=Join(
                join_type="inner",
                left=TableRef(name="a", alias="a"),
                right=TableRef(name="b", alias="b"),
            ),
        )
        bound = bind(q, catalog)
        self.assertEqual(bound.report.scope.unique_keys, [])


if __name__ == "__main__":
    unittest.main()
