"""小型行集执行测试：连接语义、USING 合并、LATERAL 与嵌套别名列序。"""

import unittest

from querybind import (
    Executor,
    bind,
    col,
    eq,
    lit,
    star,
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


class ExecutorTestCase(QueryBindTestCase):
    def setUp(self):
        super().setUp()
        self.ex = Executor(self.catalog)

    def _run(self, q):
        return self.ex.execute(bind(q, self.catalog))


class BaseExecutionTests(ExecutorTestCase):
    def test_simple_table_scan_order(self):
        q = SelectNode(
            projections=[star()],
            from_=TableRef(name="dept", alias="d"),
        )
        r = self._run(q)
        self.assertEqual(r.names(), ["id", "name", "loc"])
        self.assertEqual(
            r.ordered_rows(),
            [[1, "Eng", "BJ"], [2, "Sales", "SH"], [3, "HR", "SZ"]],
        )

    def test_where_filter_and_null_arithmetic(self):
        q = SelectNode(
            projections=[
                col("id", table="e", alias="eid"),
                FuncCall("+", [col("salary", table="e"), lit(None)],
                         alias="null_math"),
            ],
            from_=TableRef(name="emp", alias="e"),
            where=FuncCall(">", [col("salary", table="e"), lit(150)]),
        )
        r = self._run(q)
        self.assertEqual([row[0] for row in r.ordered_rows()], [11, 12, 13])
        self.assertTrue(all(row[1] is None for row in r.ordered_rows()))


class InnerJoinTests(ExecutorTestCase):
    def test_inner_join_on_predicate(self):
        q = SelectNode(
            projections=[
                col("id", table="e", alias="eid"),
                col("name", table="d", alias="dname"),
            ],
            from_=Join(
                join_type="inner",
                left=TableRef(name="emp", alias="e"),
                right=TableRef(name="dept", alias="d"),
                on=eq(col("dept_id", table="e"), col("id", table="d")),
            ),
        )
        r = self._run(q)
        self.assertEqual(
            r.ordered_rows(),
            [[10, "Eng"], [11, "Eng"], [12, "Sales"]],
        )

    def test_inner_using_merges_column_once(self):
        # 用 dept_id 做 USING：emp 与 dept 都有 dept_id? dept 无该列，故构造同名列
        q = SelectNode(
            projections=[star()],
            from_=Join(
                join_type="inner",
                left=TableRef(name="dept", alias="d"),
                right=TableRef(name="emp", alias="e"),
                using=["id"],
            ),
        )
        r = self._run(q)
        # id 在星号中只出现一次（合并列），其余列按左后右顺序
        self.assertEqual(r.names().count("id"), 1)
        names = r.names()
        self.assertEqual(names[0], "id")
        # 内连接：id 相等的行，dept id=1,2 与 emp 无同 id（emp id 从 10 起）-> 零行
        self.assertEqual(r.ordered_rows(), [])


class OuterJoinTests(ExecutorTestCase):
    def test_left_join_preserves_unmatched_left_rows(self):
        q = SelectNode(
            projections=[
                col("id", table="e", alias="eid"),
                col("bonus", table="p", alias="bonus"),
            ],
            from_=Join(
                join_type="left",
                left=TableRef(name="emp", alias="e"),
                right=TableRef(name="pay", alias="p"),
                on=eq(col("id", table="e"), col("emp_id", table="p")),
            ),
        )
        r = self._run(q)
        by_id = {row[0]: row[1] for row in r.ordered_rows()}
        self.assertEqual(by_id[10], 5)
        self.assertIsNone(by_id[11])
        self.assertIsNone(by_id[13])
        self.assertEqual(len(r.rows), 4)

    def test_full_outer_using_coalesces_first_non_null(self):
        q = SelectNode(
            projections=[
                col("id", alias="merged_id"),
                col("name", table="d", alias="dname"),
                col("name", table="e", alias="ename"),
            ],
            from_=Join(
                join_type="full",
                left=TableRef(name="dept", alias="d"),
                right=TableRef(name="emp", alias="e"),
                using=["id"],
            ),
        )
        r = self._run(q)
        rows = r.ordered_rows()
        # 3 个仅左（dept 1,2,3）+ 4 个仅右（emp 10..13），无匹配
        self.assertEqual(len(rows), 7)
        merged_ids = [row[0] for row in rows]
        self.assertEqual(merged_ids, [1, 2, 3, 10, 11, 12, 13])
        # 左行：右列为 NULL；右行：左列为 NULL
        self.assertEqual(rows[0], [1, "Eng", None])
        self.assertEqual(rows[3], [10, None, "Ann"])

    def test_full_outer_with_actual_matches_uses_coalesce(self):
        # 让两张表共享 id=1,2 以便产生匹配行
        cat = self.catalog
        cat.add(_table("t1", ["id", "v1"], [
            {"id": 1, "v1": "a"}, {"id": 2, "v1": "b"},
        ], [("id",)]))
        cat.add(_table("t2", ["id", "v2"], [
            {"id": 2, "v2": "x"}, {"id": 3, "v2": "y"},
        ], [("id",)]))
        q = SelectNode(
            projections=[
                col("id", alias="mid"),
                col("v1", table="a", alias="v1"),
                col("v2", table="b", alias="v2"),
            ],
            from_=Join(
                join_type="full",
                left=TableRef(name="t1", alias="a"),
                right=TableRef(name="t2", alias="b"),
                using=["id"],
            ),
        )
        r = self._run(q)
        rows = {row[0]: row[1:] for row in r.ordered_rows()}
        self.assertEqual(rows[1], ["a", None])
        self.assertEqual(rows[2], ["b", "x"])   # 匹配行
        self.assertEqual(rows[3], [None, "y"])

    def test_right_join_extends_left_with_nulls(self):
        cat = self.catalog
        cat.add(_table("t1", ["id", "v1"], [
            {"id": 1, "v1": "a"},
        ], [("id",)]))
        cat.add(_table("t2", ["id", "v2"], [
            {"id": 1, "v2": "x"}, {"id": 2, "v2": "y"},
        ], [("id",)]))
        q = SelectNode(
            projections=[
                col("id", alias="mid"),
                col("v1", table="a", alias="v1"),
                col("v2", table="b", alias="v2"),
            ],
            from_=Join(
                join_type="right",
                left=TableRef(name="t1", alias="a"),
                right=TableRef(name="t2", alias="b"),
                using=["id"],
            ),
        )
        r = self._run(q)
        rows = {row[0]: row[1:] for row in r.ordered_rows()}
        self.assertEqual(rows[1], ["a", "x"])
        self.assertEqual(rows[2], [None, "y"])


class LateralTests(ExecutorTestCase):
    def test_lateral_left_join_runs_per_left_row(self):
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
            projections=[
                col("id", table="e", alias="eid"),
                col("bonus", table="lb", alias="bonus"),
            ],
            from_=Join(
                join_type="left",
                left=TableRef(name="emp", alias="e"),
                right=lat,
                on=lit(True),
            ),
        )
        r = self._run(q)
        by_id = {row[0]: row[1] for row in r.ordered_rows()}
        self.assertEqual(by_id[10], 5)
        self.assertEqual(by_id[12], 7)
        self.assertIsNone(by_id[11])
        self.assertIsNone(by_id[13])

    def test_lateral_inner_join_drops_zero_match_left_rows(self):
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
            projections=[col("id", table="e", alias="eid")],
            from_=Join(
                join_type="inner",
                left=TableRef(name="emp", alias="e"),
                right=lat,
                on=lit(True),
            ),
        )
        r = self._run(q)
        self.assertEqual([row[0] for row in r.ordered_rows()], [10, 12])

    def test_left_to_right_lateral_chain(self):
        # 第一个 LATERAL 暴露 bonus，第二个 LATERAL 可同时看到 e 与第一个结果
        lat1 = DerivedTable(
            query=SelectNode(
                projections=[col("bonus", table="p", alias="bonus")],
                from_=TableRef(name="pay", alias="p"),
                where=eq(col("emp_id", table="p"), col("id", table="e")),
            ),
            alias="l1",
            lateral=True,
        )
        lat2 = DerivedTable(
            query=SelectNode(
                projections=[
                    FuncCall("coalesce", [
                        col("bonus", table="l1"), lit(0)
                    ], alias="adjusted"),
                ],
                from_=TableRef(name="pay", alias="p2"),
                where=eq(col("emp_id", table="p2"), col("id", table="e")),
            ),
            alias="l2",
            lateral=True,
        )
        q = SelectNode(
            projections=[
                col("id", table="e", alias="eid"),
                col("bonus", table="l1", alias="bonus"),
                col("adjusted", table="l2", alias="adjusted"),
            ],
            from_=Join(
                join_type="left",
                left=Join(
                    join_type="left",
                    left=TableRef(name="emp", alias="e"),
                    right=lat1,
                    on=lit(True),
                ),
                right=lat2,
                on=lit(True),
            ),
        )
        r = self._run(q)
        by_id = {row[0]: row for row in r.ordered_rows()}
        self.assertEqual(by_id[10], [10, 5, 5])
        self.assertEqual(by_id[11], [11, None, None])
        self.assertEqual(by_id[12], [12, 7, 7])


class ScalarSubqueryTests(ExecutorTestCase):
    def test_scalar_subquery_zero_and_one_match(self):
        scalar = SelectNode(
            projections=[col("bonus", table="p", alias="bonus")],
            from_=TableRef(name="pay", alias="p"),
            where=eq(col("emp_id", table="p"), col("id", table="e")),
        )
        q = SelectNode(
            projections=[
                col("id", table="e", alias="eid"),
                SubqueryExpr(query=scalar, alias="b"),
            ],
            from_=TableRef(name="emp", alias="e"),
        )
        r = self._run(q)
        by_id = {row[0]: row[1] for row in r.ordered_rows()}
        self.assertEqual(by_id[10], 5)
        self.assertIsNone(by_id[11])

    def test_scalar_subquery_multiple_rows_raises(self):
        scalar = SelectNode(
            projections=[col("bonus", table="p", alias="bonus")],
            from_=TableRef(name="pay_dup", alias="p"),
            where=eq(col("emp_id", table="p"), col("id", table="e")),
        )
        q = SelectNode(
            projections=[SubqueryExpr(query=scalar, alias="b")],
            from_=TableRef(name="emp", alias="e"),
        )
        with self.assertRaises(RuntimeError):
            self._run(q)


class NestedAliasAndColumnOrderTests(ExecutorTestCase):
    def test_nested_derived_alias_and_merged_same_name_results(self):
        # 派生表 d 输出自己的 name（来自 emp.name），与 dept.name 同名；
        # 再用 USING 风格同名列连接，核对列序与同名歧义位置。
        emp_d = DerivedTable(
            query=SelectNode(
                projections=[
                    col("id", table="e", alias="id"),
                    col("dept_id", table="e", alias="dept_id"),
                    col("name", table="e", alias="name"),
                ],
                from_=TableRef(name="emp", alias="e"),
            ),
            alias="d",
        )
        q = SelectNode(
            projections=[
                col("id", table="d", alias="emp_id"),
                col("name", table="d", alias="emp_name"),
                col("name", table="de", alias="dept_name"),
            ],
            from_=Join(
                join_type="inner",
                left=emp_d,
                right=TableRef(name="dept", alias="de"),
                on=eq(col("dept_id", table="d"), col("id", table="de")),
            ),
        )
        r = self._run(q)
        self.assertEqual(r.names(), ["emp_id", "emp_name", "dept_name"])
        self.assertEqual(
            r.ordered_rows(),
            [[10, "Ann", "Eng"], [11, "Bob", "Eng"], [12, "Cyd", "Sales"]],
        )

    def test_duplicate_output_names_keep_positions_distinct(self):
        q = SelectNode(
            projections=[
                col("name", table="d", alias="label"),
                col("name", table="e", alias="label"),
            ],
            from_=Join(
                join_type="inner",
                left=TableRef(name="dept", alias="d"),
                right=TableRef(name="emp", alias="e"),
                on=eq(col("id", table="d"), col("dept_id", table="e")),
            ),
        )
        r = self._run(q)
        # 同名输出两列，位置仍可分别核对
        self.assertEqual(r.names(), ["label", "label"])
        self.assertEqual(
            r.ordered_rows(),
            [["Eng", "Ann"], ["Eng", "Bob"], ["Sales", "Cyd"]],
        )


def _table(name, cols, rows, keys):
    from querybind import TableData
    return TableData(name, cols, rows, unique_keys=keys)


if __name__ == "__main__":
    unittest.main()
