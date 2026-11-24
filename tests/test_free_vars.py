"""自由列分析与绑定深度测试。"""

import unittest

from querybind import bind, col, eq, subq
from querybind.model import (
    DerivedTable,
    Join,
    SelectNode,
    SubqueryExpr,
    TableRef,
)

from tests._fixtures import QueryBindTestCase


class FreeVariableTests(QueryBindTestCase):
    def test_top_level_query_has_no_free_vars(self):
        q = SelectNode(
            projections=[col("id", table="d")],
            from_=TableRef(name="dept", alias="d"),
        )
        b = bind(q, self.catalog)
        self.assertEqual(b.report.free_vars, [])

    def test_correlated_scalar_reports_free_var_with_depth(self):
        scalar = SelectNode(
            projections=[col("bonus", table="p", alias="bonus")],
            from_=TableRef(name="pay", alias="p"),
            where=eq(col("emp_id", table="p"), col("id", table="e")),
        )
        outer = SelectNode(
            projections=[
                col("id", table="e", alias="eid"),
                SubqueryExpr(query=scalar, alias="b"),
            ],
            from_=TableRef(name="emp", alias="e"),
        )
        b = bind(outer, self.catalog)
        top_report = b.report
        self.assertEqual(top_report.free_vars, [])
        inner_report = b.projections[1].query.report
        self.assertEqual(len(inner_report.free_vars), 1)
        fv = inner_report.free_vars[0]
        self.assertEqual((fv.qualifier, fv.name), ("e", "id"))
        self.assertEqual(fv.depth, 0)
        self.assertFalse(fv.sibling)

    def test_two_level_nesting_reports_correct_depth(self):
        # 深度 2 的子查询引用深度 0 的列
        level2 = SelectNode(
            projections=[col("bonus", table="p", alias="bonus")],
            from_=TableRef(name="pay", alias="p"),
            where=eq(col("emp_id", table="p"), col("id", table="e")),
        )
        level1 = SelectNode(
            projections=[SubqueryExpr(query=level2, alias="b")],
            from_=TableRef(name="emp", alias="e"),
            where=eq(col("dept_id", table="e"), col("id", table="d")),
        )
        top = SelectNode(
            projections=[
                col("id", table="d", alias="did"),
                SubqueryExpr(query=level1, alias="nested"),
            ],
            from_=TableRef(name="dept", alias="d"),
        )
        b = bind(top, self.catalog)
        l1 = b.projections[1].query
        l2 = l1.projections[0].query
        # level1 自由列 d.id，深度 0
        self.assertEqual(
            {(f.qualifier, f.name, f.depth) for f in l1.report.free_vars},
            {("d", "id", 0)},
        )
        # level2 自由列 e.id（深度 1）；d.id 跨两层不可见，故不出现
        self.assertEqual(
            {(f.qualifier, f.name, f.depth) for f in l2.report.free_vars},
            {("e", "id", 1)},
        )

    def test_lateral_free_var_marked_sibling(self):
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
                on=None,
            ),
        )
        b = bind(q, self.catalog)
        fv = b.from_.right.query.report.free_vars
        self.assertEqual(len(fv), 1)
        self.assertTrue(fv[0].sibling)
        self.assertEqual(fv[0].depth, 0)

    def test_free_vars_include_lineage(self):
        scalar = SelectNode(
            projections=[col("bonus", table="p", alias="bonus")],
            from_=TableRef(name="pay", alias="p"),
            where=eq(col("emp_id", table="p"), col("id", table="e")),
        )
        q = SelectNode(
            projections=[SubqueryExpr(query=scalar, alias="b")],
            from_=TableRef(name="emp", alias="e"),
        )
        b = bind(q, self.catalog)
        fv = b.projections[0].query.report.free_vars[0]
        self.assertEqual(fv.base_refs, (("emp", "id"),))

    def test_output_descriptors_carry_position_and_uniqueness(self):
        q = SelectNode(
            projections=[
                col("name", table="d", alias="dname"),
                col("id", table="d", alias="did"),
            ],
            from_=TableRef(name="dept", alias="d"),
        )
        b = bind(q, self.catalog)
        outs = b.report.outputs
        self.assertEqual([o.position for o in outs], [0, 1])
        self.assertEqual([o.name for o in outs], ["dname", "did"])
        self.assertFalse(outs[0].unique)
        self.assertTrue(outs[1].unique)


if __name__ == "__main__":
    unittest.main()
