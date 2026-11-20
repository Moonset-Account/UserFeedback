"""relengine 删除语句的合成数据测试。

覆盖点（对应题面）：
- 批量显式删除、缺失主键、重复主键去重
- CHECK 策略语句末检查：返回具体引用行/目标行，整句回退
- 调用方把阻塞行加入显式删除集后第二次一次通过（跨表与自引用两种）
- CASCADE 自引用子树、两表互级联、双路径菱形只删一次
- 级联闭包完成后再做语句末检查，阻塞时级联目标一并回退
- 复合主键/复合外键、空外键不参与引用

注意：本文件仅为本地合成数据单元测试，按任务要求未执行、未做运行验证。
"""

import unittest

from relengine import (
    CASCADE,
    CHECK,
    BlockedReference,
    Database,
    DeleteBlockedError,
)


class DeleteStatementTests(unittest.TestCase):
    def test_batch_explicit_delete_with_missing_and_duplicate_keys(self):
        db = Database()
        db.create_table("item", "id")
        for i in (1, 2, 3):
            db.insert("item", {"id": i})

        # 1 重复指定、99 不存在；实际只删 1 和 2。
        result = db.delete("item", [1, 1, 2, 99])

        self.assertEqual(result.count, 2)
        self.assertTrue(result.was_deleted("item", (1,)))
        self.assertTrue(result.was_deleted("item", (2,)))
        self.assertEqual(db.count("item"), 1)
        self.assertEqual(result.missing, (("item", (99,)),))

    def test_check_fk_blocks_with_details_and_rolls_back_then_retry_passes(self):
        db = self._build_dept_emp()

        # 第一次：只删部门一端，员工幸存引用 -> 阻塞。
        with self.assertRaises(DeleteBlockedError) as ctx:
            db.delete_rows([("dept", "d1")])

        err = ctx.exception
        self.assertEqual(len(err.violations), 1)
        v: BlockedReference = err.violations[0]
        self.assertEqual(v.foreign_key, "emp_dept_fk")
        self.assertEqual(v.referencing_table, "emp")
        self.assertEqual(v.referencing_key, ("e1",))
        self.assertEqual(v.referencing_row["emp_id"], "e1")
        self.assertEqual(v.referenced_table, "dept")
        self.assertEqual(v.referenced_key, ("d1",))
        self.assertEqual(v.referenced_row["dept_id"], "d1")
        self.assertIn("e1", v.describe())

        # 回退：所有行原封不动。
        self.assertEqual(db.count("dept"), 2)
        self.assertEqual(db.count("emp"), 2)

        # 第二次：把阻塞行一并放入显式删除集，跨表一次通过。
        result = db.delete_rows([("dept", "d1"), ("emp", "e1")])
        self.assertEqual(result.count, 2)
        self.assertEqual(db.count("dept"), 1)
        self.assertEqual(db.count("emp"), 1)
        # 未涉及的数据幸存。
        self.assertEqual(db.get_row("emp", "e2")["dept_id"], "d2")

    def test_self_referential_check_fk_retry_flow(self):
        db = Database()
        db.create_table("employee", "emp_id")
        db.add_foreign_key(
            "emp_manager_fk",
            "employee", ("manager_id",),
            "employee", ("emp_id",),
            CHECK,
        )
        db.insert("employee", {"emp_id": "ceo"})
        db.insert("employee", {"emp_id": "cto", "manager_id": "ceo"})

        with self.assertRaises(DeleteBlockedError) as ctx:
            db.delete("employee", "ceo")
        self.assertEqual(ctx.exception.violations[0].referencing_key, ("cto",))
        self.assertEqual(db.count("employee"), 2)  # 回退

        result = db.delete_rows([("employee", "ceo"), ("employee", "cto")])
        self.assertEqual(result.count, 2)
        self.assertEqual(db.count("employee"), 0)

    def test_null_foreign_key_does_not_reference_anything(self):
        db = Database()
        db.create_table("employee", "emp_id")
        db.add_foreign_key(
            "emp_manager_fk",
            "employee", ("manager_id",),
            "employee", ("emp_id",),
            CHECK,
        )
        db.insert("employee", {"emp_id": "ceo"})
        db.insert("employee", {"emp_id": "free_agent", "manager_id": None})

        result = db.delete("employee", "ceo")
        self.assertEqual(result.count, 1)
        self.assertEqual(db.count("employee"), 1)

    def test_self_referential_cascade_deletes_subtree_once(self):
        db = Database()
        db.create_table("node", "nid")
        db.add_foreign_key(
            "node_parent_fk",
            "node", ("parent_id",),
            "node", ("nid",),
            CASCADE,
        )
        db.insert("node", {"nid": "root"})
        db.insert("node", {"nid": "a", "parent_id": "root"})
        db.insert("node", {"nid": "b", "parent_id": "root"})
        db.insert("node", {"nid": "c", "parent_id": "a"})
        db.insert("node", {"nid": "orphan", "parent_id": None})

        result = db.delete("node", "root")

        self.assertEqual(result.count, 4)
        self.assertEqual(
            sorted(result.keys_by_table("node")),
            [("a",), ("b",), ("c",), ("root",)],
        )
        self.assertEqual(db.count("node"), 1)  # orphan 幸存

    def test_mutual_cascade_between_two_tables_deletes_each_once(self):
        db = Database()
        db.create_table("team", "team_id")
        db.create_table("employee", "emp_id")
        # employee.team_id -> team，team.lead_id -> employee，两条均级联。
        db.add_foreign_key(
            "emp_team_fk",
            "employee", ("team_id",),
            "team", ("team_id",),
            CASCADE,
        )
        db.add_foreign_key(
            "team_lead_fk",
            "team", ("lead_id",),
            "employee", ("emp_id",),
            CASCADE,
        )
        db.insert("team", {"team_id": "t1", "lead_id": "e1"})
        db.insert("employee", {"emp_id": "e1", "team_id": "t1"})
        db.insert("team", {"team_id": "t2", "lead_id": "e2"})
        db.insert("employee", {"emp_id": "e2", "team_id": "t2"})

        result = db.delete("team", "t1")

        self.assertEqual(result.count, 2)
        self.assertTrue(result.was_deleted("team", ("t1",)))
        self.assertTrue(result.was_deleted("employee", ("e1",)))
        # 互级联不能把任一行删两次，也不能波及第二组。
        self.assertEqual(db.count("team"), 1)
        self.assertEqual(db.count("employee"), 1)

    def test_diamond_two_paths_reach_same_row_only_deleted_once(self):
        db = Database()
        db.create_table("root_item", "rid")
        db.create_table("mid_item", "mid")
        db.create_table("leaf_item", "lid")
        db.add_foreign_key(
            "mid_root_fk",
            "mid_item", ("rid",),
            "root_item", ("rid",),
            CASCADE,
        )
        # leaf 通过两条外键指向同一个 mid：两条级联路径汇合于同一行。
        db.add_foreign_key(
            "leaf_mid_a_fk",
            "leaf_item", ("mid_a",),
            "mid_item", ("mid",),
            CASCADE,
        )
        db.add_foreign_key(
            "leaf_mid_b_fk",
            "leaf_item", ("mid_b",),
            "mid_item", ("mid",),
            CASCADE,
        )
        db.insert("root_item", {"rid": "r1"})
        db.insert("mid_item", {"mid": "m1", "rid": "r1"})
        db.insert("leaf_item", {"lid": "l1", "mid_a": "m1", "mid_b": "m1"})

        result = db.delete("root_item", "r1")

        self.assertEqual(result.count, 3)
        self.assertEqual(result.keys_by_table("leaf_item"), [("l1",)])
        self.assertEqual(db.count("leaf_item"), 0)

    def test_cascade_closure_checked_at_statement_end_with_full_rollback(self):
        db = Database()
        db.create_table("dept", "dept_id")
        db.create_table("emp", "emp_id")
        db.add_foreign_key(
            "emp_dept_cascade_fk",
            "emp", ("dept_id",),
            "dept", ("dept_id",),
            CASCADE,
        )
        db.add_foreign_key(
            "emp_dept_check_fk",
            "emp", ("home_dept_id",),
            "dept", ("dept_id",),
            CHECK,
        )
        db.insert("dept", {"dept_id": "d1"})
        db.insert("dept", {"dept_id": "d2"})
        # e1 经 CASCADE 外键挂 d1；e2 经 CHECK 外键挂 d2。
        db.insert("emp", {"emp_id": "e1", "dept_id": "d1", "home_dept_id": None})
        db.insert("emp", {"emp_id": "e2", "dept_id": None, "home_dept_id": "d2"})

        with self.assertRaises(DeleteBlockedError) as ctx:
            db.delete_rows([("dept", "d1"), ("dept", "d2")])

        self.assertEqual(ctx.exception.violations[0].referencing_key, ("e2",))
        # 级联计划中的 e1 与两个部门全部回退。
        self.assertEqual(db.count("dept"), 2)
        self.assertEqual(db.count("emp"), 2)

        # 第二次把 CHECK 阻塞行也显式删除，整句一次通过。
        result = db.delete_rows(
            [("dept", "d1"), ("dept", "d2"), ("emp", "e1"), ("emp", "e2")]
        )
        self.assertEqual(result.count, 4)
        self.assertEqual(db.count("dept"), 0)
        self.assertEqual(db.count("emp"), 0)

    def test_composite_primary_and_foreign_key_check_then_retry(self):
        db = Database()
        db.create_table("ship", ("ship_id", "port"))
        db.create_table("part", "part_id")
        db.add_foreign_key(
            "part_ship_fk",
            "part", ("ship_id", "ship_port"),
            "ship", ("ship_id", "port"),
            CHECK,
        )
        db.insert("ship", {"ship_id": "s1", "port": "p1"})
        db.insert("part", {"part_id": "pa", "ship_id": "s1", "ship_port": "p1"})

        with self.assertRaises(DeleteBlockedError) as ctx:
            db.delete_rows([("ship", ("s1", "p1"))])
        v = ctx.exception.violations[0]
        self.assertEqual(v.referencing_key, ("pa",))
        self.assertEqual(v.referenced_key, ("s1", "p1"))
        self.assertEqual(db.count("ship"), 1)
        self.assertEqual(db.count("part"), 1)

        result = db.delete_rows(
            [("ship", ("s1", "p1")), ("part", "pa")]
        )
        self.assertEqual(result.count, 2)
        self.assertEqual(db.count("ship"), 0)
        self.assertEqual(db.count("part"), 0)

    def test_mixed_strategies_cascade_survives_but_check_blocks_and_rolls_back(self):
        db = Database()
        db.create_table("dept", "dept_id")
        db.create_table("cascade_emp", "emp_id")
        db.create_table("check_emp", "emp_id")
        # 同一被引用表上挂两种策略，分属两张引用表，互不干扰。
        db.add_foreign_key(
            "cascade_emp_dept_fk",
            "cascade_emp", ("dept_id",),
            "dept", ("dept_id",),
            CASCADE,
        )
        db.add_foreign_key(
            "check_emp_dept_fk",
            "check_emp", ("dept_id",),
            "dept", ("dept_id",),
            CHECK,
        )
        db.insert("dept", {"dept_id": "d1"})
        db.insert("dept", {"dept_id": "d2"})
        db.insert("cascade_emp", {"emp_id": "c1", "dept_id": "d1"})
        db.insert("check_emp", {"emp_id": "k1", "dept_id": "d1"})

        with self.assertRaises(DeleteBlockedError) as ctx:
            db.delete("dept", "d1")

        # 只有 CHECK 表的幸存行构成阻塞；CASCADE 表的行已在删除计划内。
        self.assertEqual(len(ctx.exception.violations), 1)
        v = ctx.exception.violations[0]
        self.assertEqual(v.referencing_table, "check_emp")
        self.assertEqual(v.referencing_key, ("k1",))
        self.assertEqual(v.referenced_key, ("d1",))
        # 级联计划中的 c1 与目标 d1 全部随整句回退。
        self.assertEqual(db.count("dept"), 2)
        self.assertEqual(db.count("cascade_emp"), 1)
        self.assertEqual(db.count("check_emp"), 1)

        # 第二次把 CHECK 阻塞行也显式删除，整句一次通过。
        result = db.delete_rows(
            [("dept", "d1"), ("check_emp", "k1")]
        )
        self.assertEqual(result.count, 3)
        self.assertEqual(db.count("dept"), 1)
        self.assertEqual(db.count("cascade_emp"), 0)
        self.assertEqual(db.count("check_emp"), 0)

    # ------------------------------------------------------------------ 夹具

    @staticmethod
    def _build_dept_emp() -> Database:
        db = Database()
        db.create_table("dept", "dept_id")
        db.create_table("emp", "emp_id")
        db.add_foreign_key(
            "emp_dept_fk",
            "emp", ("dept_id",),
            "dept", ("dept_id",),
            CHECK,
        )
        db.insert("dept", {"dept_id": "d1"})
        db.insert("dept", {"dept_id": "d2"})
        db.insert("emp", {"emp_id": "e1", "dept_id": "d1"})
        db.insert("emp", {"emp_id": "e2", "dept_id": "d2"})
        return db


if __name__ == "__main__":
    unittest.main()
