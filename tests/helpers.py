"""测试共用的构造工具。"""

from __future__ import annotations

from migration_planner import (
    ColumnSpec,
    ForeignKeySpec,
    Insert,
    PlanResult,
    TableSpec,
    Update,
)


def col(name: str, nullable: bool = True, cost: float | None = None) -> ColumnSpec:
    return ColumnSpec(name, nullable=nullable, defer_cost=cost)


def pk(name: str = "id") -> ColumnSpec:
    return ColumnSpec(name, nullable=False)


def fk(name: str, columns: tuple[str, ...], ref_table: str,
       ref_columns: tuple[str, ...]) -> ForeignKeySpec:
    return ForeignKeySpec(name, columns, ref_table, ref_columns)


def table(name: str, key: tuple[str, ...], columns, foreign_keys=()) -> TableSpec:
    return TableSpec(name, key, tuple(columns), tuple(foreign_keys))


def assert_plan_healthy(testcase, result: PlanResult) -> None:
    """结果可解且基本不变量成立。"""
    testcase.assertTrue(result.feasible, result.reason)
    testcase.assertEqual(
        len(result.statements), len(result.inserts) + len(result.updates)
    )
    # INSERT 数等于批内行数（每个节点恰好插一次）。
    testcase.assertTrue(all(isinstance(s, Insert) for s in result.inserts))
    testcase.assertTrue(all(isinstance(s, Update) for s in result.updates))
    # 每条 UPDATE 都对应一条暂缓记录，且数量相等。
    testcase.assertEqual(len(result.updates), len(result.deferred))
    updated = {(u.table, u.key, u.fk_name) for u in result.updates}
    deferred = {(d.table, d.row_key, d.fk_name) for d in result.deferred}
    testcase.assertEqual(updated, deferred)
    # 代价与暂缓集合一致。
    testcase.assertAlmostEqual(
        result.total_cost, sum(d.cost for d in result.deferred)
    )
    # 任何 UPDATE 都必须出现在其源行 INSERT 之后。
    insert_pos = {
        (ins.table, ins.key): i for i, ins in enumerate(result.statements)
        if isinstance(ins, Insert)
    }
    for i, stmt in enumerate(result.statements):
        if isinstance(stmt, Update):
            testcase.assertGreater(i, insert_pos[(stmt.table, stmt.key)])
