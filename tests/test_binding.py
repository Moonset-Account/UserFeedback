"""绑定规则测试：别名、星号、USING 合并、遮蔽、歧义与横向合法性。"""

import unittest

from querybind import (
    AmbiguousColumnError,
    Catalog,
    Executor,
    IllegalLateralError,
    TableData,
    UnresolvedColumnError,
    bind,
    col,
    eq,
    lit,
    star,
)
from querybind.model import (
    DerivedTable,
    Join,
    SelectNode,
    TableRef,
)

from tests._fixtures import QueryBindTestCase, build_catalog


class BaseTableAndAliasTests(QueryBindTestCase):
    def test_qualified_table_keeps_original_columns(self):
        q = SelectNode(
            projections=[
                col("id", table="d"),
                col("name", table="d"),
                col("loc", table="d"),
            ],
            from_=TableRef(name="dept", alias="d"),
        )
        b = bind(q, self.catalog)
        refs = [p.resolved for p in b.projections]
        self.assertEqual([r.qualifier for r in refs], ["d", "d", "d"])
        self.assertEqual([r.name for r in refs], ["id", "name", "loc"])
        self.assertEqual([r.scope_position for r in refs], [0, 1, 2])
        # id 来自唯一键
        self.assertTrue(refs[0].unique)
        self.assertFalse(refs[1].unique)

    def test_default_alias_is_table_name(self):
        q = SelectNode(
            projections=[col("id", table="dept")],
            from_=TableRef(name="dept"),
        )
        b = bind(q, self.catalog)
        self.assertEqual(b.projections[0].resolved.qualifier, "dept")

    def test_unresolved_qualified_column_reports_location(self):
        q = SelectNode(
            projections=[col("nope", table="d", location="proj-1")],
            from_=TableRef(name="dept", alias="d"),
        )
        with self.assertRaises(UnresolvedColumnError) as ctx:
            bind(q, self.catalog)
        self.assertEqual(ctx.exception.location, "proj-1")
        self.assertEqual(ctx.exception.qualifier, "d")
        self.assertEqual(ctx.exception.column, "nope")


class StarExpansionTests(QueryBindTestCase):
    def test_bare_star_follows_current_output_order(self):
        q = SelectNode(
            projections=[star()],
            from_=TableRef(name="emp", alias="e"),
        )
        b = bind(q, self.catalog)
        self.assertEqual(
            [p.alias for p in b.projections],
            ["id", "dept_id", "name", "salary"],
        )
        self.assertEqual(
            [p.resolved.scope_position for p in b.projections],
            [0, 1, 2, 3],
        )

    def test_qualified_star_expands_only_that_table(self):
        q = SelectNode(
            projections=[star("e")],
            from_=Join(
                join_type="inner",
                left=TableRef(name="dept", alias="d"),
                right=TableRef(name="emp", alias="e"),
                on=eq(col("id", table="d"), col("dept_id", table="e")),
            ),
        )
        b = bind(q, self.catalog)
        self.assertEqual(
            [p.resolved.qualifier for p in b.projections],
            ["e", "e", "e", "e"],
        )

    def test_star_then_extra_projection_keeps_order(self):
        q = SelectNode(
            projections=[star(), col("name", table="d", alias="dname")],
            from_=Join(
                join_type="inner",
                left=TableRef(name="dept", alias="d"),
                right=TableRef(name="emp", alias="e"),
                on=eq(col("id", table="d"), col("dept_id", table="e")),
            ),
        )
        b = bind(q, self.catalog)
        # 连接作用域：左表列在前，右表列在后，附加列最后
        names = [p.alias for p in b.projections]
        self.assertEqual(names[:3], ["id", "name", "loc"])
        self.assertEqual(names[3:7], ["id", "dept_id", "name", "salary"])
        self.assertEqual(names[-1], "dname")


class UsingJoinTests(QueryBindTestCase):
    def _using_query(self, join_type):
        return SelectNode(
            projections=[star()],
            from_=Join(
                join_type=join_type,
                left=TableRef(name="dept", alias="d"),
                right=TableRef(name="emp", alias="e"),
                using=["id"],
            ),
        )

    def test_using_column_appears_once_unqualified(self):
        b = bind(self._using_query("inner"), self.catalog)
        scope = b.report.scope
        unqualified = scope.columns["id"]
        self.assertEqual(len(unqualified), 1)
        merged = unqualified[0]
        self.assertTrue(merged.is_using_merged)
        self.assertIsNone(merged.qualifier)
        # 合并列在星号序列最前
        self.assertEqual(scope.ordered[0].name, "id")

    def test_qualified_names_still_reach_original_columns(self):
        b = bind(self._using_query("inner"), self.catalog)
        d = b.report.scope.find_table("d")
        e = b.report.scope.find_table("e")
        d_id = [c for c in d.columns if c.name == "id"][0]
        e_id = [c for c in e.columns if c.name == "id"][0]
        self.assertEqual(d_id.base_refs, (("dept", "id"),))
        self.assertEqual(e_id.base_refs, (("emp", "id"),))

        # 显式限定列仍可绑定到原列
        q = SelectNode(
            projections=[
                col("id", table="d", alias="did"),
                col("id", table="e", alias="eid"),
                col("id", alias="merged_id"),
            ],
            from_=Join(
                join_type="inner",
                left=TableRef(name="dept", alias="d"),
                right=TableRef(name="emp", alias="e"),
                using=["id"],
            ),
        )
        bq = bind(q, self.catalog)
        self.assertEqual(bq.projections[0].resolved.base_refs, (("dept", "id"),))
        self.assertEqual(bq.projections[1].resolved.base_refs, (("emp", "id"),))
        self.assertTrue(bq.projections[2].resolved.is_using_merged)

    def test_full_outer_using_keeps_coalesce_source(self):
        b = bind(self._using_query("full"), self.catalog)
        merged = b.report.scope.columns["id"][0]
        source = merged.source
        # 全外连接的来源表达式是 COALESCE(左, 右)
        self.assertEqual(source.name, "coalesce")
        self.assertEqual(source.args[0].resolved.qualifier, "d")
        self.assertEqual(source.args[1].resolved.qualifier, "e")

    def test_inner_using_source_prefers_left(self):
        b = bind(self._using_query("inner"), self.catalog)
        merged = b.report.scope.columns["id"][0]
        self.assertEqual(merged.source.resolved.qualifier, "d")

    def test_left_outer_using_source_prefers_left(self):
        b = bind(self._using_query("left"), self.catalog)
        merged = b.report.scope.columns["id"][0]
        self.assertEqual(merged.source.resolved.qualifier, "d")


class ShadowingAndAmbiguityTests(QueryBindTestCase):
    def test_derived_alias_shadows_outer_name(self):
        # 内层派生表 d 与右表 de 都暴露 name：同层未限定 name 必须报歧义，
        # 而限定名 d.name / de.name 仍分别可解析，体现别名遮蔽边界。
        inner = SelectNode(
            projections=[
                col("id", table="e", alias="id"),
                col("name", table="e", alias="name"),
            ],
            from_=TableRef(name="emp", alias="e"),
        )
        outer = SelectNode(
            projections=[
                col("name", table="d", alias="emp_name"),
                col("name", table="de", alias="dept_name"),
            ],
            from_=Join(
                join_type="inner",
                left=DerivedTable(query=inner, alias="d"),
                right=TableRef(name="dept", alias="de"),
                on=eq(col("id", table="d"), col("id", table="de")),
            ),
        )
        b = bind(outer, self.catalog)
        self.assertEqual(b.projections[0].resolved.base_refs,
                         (("emp", "name"),))
        self.assertEqual(b.projections[1].resolved.base_refs,
                         (("dept", "name"),))

        ambiguous = SelectNode(
            projections=[col("name", alias="who")],
            from_=Join(
                join_type="inner",
                left=DerivedTable(query=inner, alias="d"),
                right=TableRef(name="dept", alias="de"),
                on=eq(col("id", table="d"), col("id", table="de")),
            ),
        )
        with self.assertRaises(AmbiguousColumnError) as ctx:
            bind(ambiguous, self.catalog)
        self.assertEqual(
            {c["qualifier"] for c in ctx.exception.candidates}, {"d", "de"}
        )

    def test_shadowing_with_distinct_column_names_resolves_inner(self):
        inner = SelectNode(
            projections=[
                col("id", table="e", alias="id"),
                col("salary", table="e", alias="salary"),
            ],
            from_=TableRef(name="emp", alias="e"),
        )
        outer = SelectNode(
            projections=[col("salary", alias="s")],
            from_=DerivedTable(query=inner, alias="d"),
        )
        b = bind(outer, self.catalog)
        self.assertEqual(b.projections[0].resolved.base_refs,
                         (("emp", "salary"),))

    def test_ambiguous_column_lists_all_candidates_with_positions(self):
        q = SelectNode(
            projections=[col("id", location="select")],
            from_=Join(
                join_type="inner",
                left=TableRef(name="dept", alias="d"),
                right=TableRef(name="emp", alias="e"),
                on=eq(col("id", table="d"), col("dept_id", table="e")),
            ),
        )
        with self.assertRaises(AmbiguousColumnError) as ctx:
            bind(q, self.catalog)
        candidates = ctx.exception.candidates
        qualifiers = {c["qualifier"] for c in candidates}
        self.assertEqual(qualifiers, {"d", "e"})
        self.assertEqual(ctx.exception.location, "select")
        for c in candidates:
            self.assertIn("position", c)
            self.assertIn("depth", c)
            self.assertIn("base_refs", c)

    def test_nested_derived_outer_reference_remains_reachable(self):
        # 外层块（dept d）投影中的标量子查询是内层块；内层引用 d.id 合法。
        from querybind.model import SubqueryExpr
        inner = SelectNode(
            projections=[col("dept_id", table="e", alias="dept_id")],
            from_=TableRef(name="emp", alias="e"),
            where=eq(col("dept_id", table="e"), col("id", table="d")),
        )
        outer = SelectNode(
            projections=[
                col("id", table="d", alias="did"),
                SubqueryExpr(query=inner, alias="one_dept"),
            ],
            from_=TableRef(name="dept", alias="d"),
        )
        b = bind(outer, self.catalog)
        inner_report = b.projections[1].query.report
        free = {(f.qualifier, f.name): f for f in inner_report.free_vars}
        self.assertIn(("d", "id"), free)
        self.assertEqual(free[("d", "id")].depth, 0)
        self.assertFalse(free[("d", "id")].sibling)


class LateralLegalityTests(QueryBindTestCase):
    def test_non_lateral_derived_cannot_see_left_neighbour(self):
        sneaky = DerivedTable(
            query=SelectNode(
                projections=[col("id", table="e", alias="eid")],
                from_=TableRef(name="pay", alias="p"),
            ),
            alias="bad",
            lateral=False,
        )
        q = SelectNode(
            projections=[star()],
            from_=Join(
                join_type="inner",
                left=TableRef(name="emp", alias="e"),
                right=sneaky,
                on=lit(True),
            ),
        )
        with self.assertRaises(IllegalLateralError) as ctx:
            bind(q, self.catalog)
        self.assertEqual(ctx.exception.qualifier, "e")
        self.assertEqual(ctx.exception.column, "id")

    def test_non_lateral_derived_still_sees_outer_query_block(self):
        # 两层查询块：外层 FROM 为 dept d；投影中的标量子查询属于内层块，
        # 内层引用 d.id 是“引用外层块”，合法；内层没有同层左邻可偷看。
        from querybind.model import SubqueryExpr
        inner_scalar = SelectNode(
            projections=[col("id", table="e", alias="eid")],
            from_=TableRef(name="emp", alias="e"),
            where=eq(col("dept_id", table="e"), col("id", table="d")),
        )
        outer = SelectNode(
            projections=[
                col("id", table="d", alias="did"),
                SubqueryExpr(query=inner_scalar, alias="x_scalar"),
            ],
            from_=TableRef(name="dept", alias="d"),
        )
        b = bind(outer, self.catalog)  # 不抛异常即通过
        sq = b.projections[1].query
        free = {(f.qualifier, f.name): f for f in sq.report.free_vars}
        self.assertIn(("d", "id"), free)
        self.assertEqual(free[("d", "id")].depth, 0)
        self.assertFalse(free[("d", "id")].sibling)

    def test_lateral_derived_may_reference_left_neighbour(self):
        lat = DerivedTable(
            query=SelectNode(
                projections=[col("bonus", table="p", alias="bonus")],
                from_=TableRef(name="pay", alias="p"),
                where=eq(col("emp_id", table="p"), col("id", table="e")),
            ),
            alias="lb",
            lateral=True,
        )
        q = SelectNode(
            projections=[col("id", table="e"), col("bonus", table="lb")],
            from_=Join(
                join_type="left",
                left=TableRef(name="emp", alias="e"),
                right=lat,
                on=lit(True),
            ),
        )
        b = bind(q, self.catalog)
        free = b.from_.right.query.report.free_vars
        self.assertEqual(len(free), 1)
        self.assertTrue(free[0].sibling)
        self.assertEqual((free[0].qualifier, free[0].name), ("e", "id"))


class BoundaryTests(QueryBindTestCase):
    def test_constant_query_without_from(self):
        q = SelectNode(
            projections=[lit(7, alias="answer"), lit("ok", alias="status")],
        )
        b = bind(q, self.catalog)
        r = Executor(self.catalog).execute(b)
        self.assertEqual(r.names(), ["answer", "status"])
        self.assertEqual(r.ordered_rows(), [[7, "ok"]])

    def test_using_column_missing_on_one_side_raises(self):
        cat = Catalog([
            TableData("a", ["id"], [{"id": 1}], [("id",)]),
            TableData("b", ["other"], [{"other": 1}]),
        ])
        q = SelectNode(
            projections=[star()],
            from_=Join(
                join_type="inner",
                left=TableRef(name="a", alias="a"),
                right=TableRef(name="b", alias="b"),
                using=["id"],
            ),
        )
        with self.assertRaises(UnresolvedColumnError):
            bind(q, cat)


if __name__ == "__main__":
    unittest.main()
