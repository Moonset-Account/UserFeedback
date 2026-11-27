"""事务边界：提交保留事实、回滚恢复快照并使事务内事实失效、嵌套语义。"""

import unittest

from authflow import FactKey, ProgramBuilder, analyze
from authflow.analysis import (
    REASON_AUTHORIZED,
    REASON_TX_ROLLBACK,
)


def _finding(result, node_id):
    for finding in result.findings:
        if finding.node_id == node_id:
            return finding
    raise AssertionError(f"未找到写入节点 {node_id} 的判定")


class TransactionCommitTest(unittest.TestCase):
    def test_commit_keeps_facts_created_inside_transaction(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .tx_begin("begin")
            .auth_check(
                "check", object_id="oid-1", operation="write", target="order"
            )
            .tx_commit("commit")
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "begin")
            .connect("begin", "check")
            .connect("check", "commit")
            .connect("commit", "write")
            .build()
        )
        result = analyze(program)
        self.assertEqual(result.constrained_writes, ["write"])
        self.assertIn(
            FactKey("oid-1", 1, "write"), result.facts_at_exit()
        )


class TransactionRollbackTest(unittest.TestCase):
    def _rollback_program(self):
        # begin -> check(inside) -> rollback -> write
        return (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .tx_begin("begin")
            .auth_check(
                "check_inside",
                object_id="oid-1",
                operation="write",
                target="order",
            )
            .tx_rollback("rollback")
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "begin")
            .connect("begin", "check_inside")
            .connect("check_inside", "rollback")
            .connect("rollback", "write")
            .build()
        )

    def test_rollback_invalidates_fact_created_inside(self):
        result = analyze(self._rollback_program())
        finding = _finding(result, "write")
        self.assertEqual(finding.reason, REASON_TX_ROLLBACK)
        # 断链起点是事务内的授权检查，且回滚节点被标记为断点
        self.assertEqual(
            finding.fact_origins, frozenset({"check_inside"})
        )

    def test_rollback_emits_invalidation_record(self):
        result = analyze(self._rollback_program())
        rollback_items = [
            item
            for item in result.invalidated_facts
            if item.reason == REASON_TX_ROLLBACK
        ]
        self.assertEqual(len(rollback_items), 1)
        self.assertEqual(
            rollback_items[0].fact,
            FactKey("oid-1", 1, "write"),
        )
        self.assertEqual(
            rollback_items[0].origins, frozenset({"check_inside"})
        )
        self.assertEqual(rollback_items[0].node_id, "rollback")

    def test_rollback_chain_points_at_rollback_node(self):
        result = analyze(self._rollback_program())
        chain = _finding(result, "write").chain
        ids = [link.node_id for link in chain]
        self.assertEqual(ids[0], "check_inside")
        self.assertIn("rollback", ids)
        self.assertEqual(ids[-1], "write")
        rollback_link = next(link for link in chain if link.node_id == "rollback")
        self.assertTrue(rollback_link.is_break)
        self.assertEqual(rollback_link.role, "rollback")

    def test_rollback_restores_pre_transaction_binding(self):
        # 事务内把 order 推进到 v2 并授权 v2；回滚后恢复 v1 绑定，
        # 且 v2 事实失效，对 v1 句柄的写入无授权。
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .tx_begin("begin")
            .call(
                "advance",
                advances={"oid-1": 2},
                rebindings={"order": ("oid-1", 2)},
            )
            .auth_check(
                "check_v2",
                object_id="oid-1",
                operation="write",
                target="order",
            )
            .tx_rollback("rollback")
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "begin")
            .connect("begin", "advance")
            .connect("advance", "check_v2")
            .connect("check_v2", "rollback")
            .connect("rollback", "write")
            .build()
        )
        result = analyze(program)
        finding = _finding(result, "write")
        self.assertEqual(finding.version, 1)
        self.assertEqual(
            [item.fact for item in result.invalidated_facts
             if item.reason == REASON_TX_ROLLBACK],
            [FactKey("oid-1", 2, "write")],
        )

    def test_fact_before_transaction_survives_rollback(self):
        # 事务前已有授权；事务内无新增事实；回滚后事务前事实仍可用。
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .auth_check(
                "check_before",
                object_id="oid-1",
                operation="write",
                target="order",
            )
            .tx_begin("begin")
            .call("observe", target="order", operation="read")
            .tx_rollback("rollback")
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "check_before")
            .connect("check_before", "begin")
            .connect("begin", "observe")
            .connect("observe", "rollback")
            .connect("rollback", "write")
            .build()
        )
        result = analyze(program)
        self.assertEqual(result.constrained_writes, ["write"])
        self.assertEqual(
            result.evaluations["write"].reason, REASON_AUTHORIZED
        )
        # 没有事务内事实，因此回滚不产生失效记录
        self.assertEqual(
            [
                item
                for item in result.invalidated_facts
                if item.reason == REASON_TX_ROLLBACK
            ],
            [],
        )


class NestedTransactionTest(unittest.TestCase):
    def test_inner_rollback_keeps_outer_state(self):
        # 外层事务开始 -> 外层授权 -> 内层事务开始 -> 内层重绑定并授权
        # -> 内层回滚 -> 外层状态恢复，外层授权仍约束写入
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .tx_begin("outer_begin")
            .auth_check(
                "outer_check",
                object_id="oid-1",
                operation="write",
                target="order",
            )
            .tx_begin("inner_begin")
            .call(
                "inner_rebind",
                rebindings={"order": ("oid-77", 1)},
            )
            .auth_check(
                "inner_check",
                object_id="oid-77",
                operation="write",
                target="order",
            )
            .tx_rollback("inner_rollback")
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "outer_begin")
            .connect("outer_begin", "outer_check")
            .connect("outer_check", "inner_begin")
            .connect("inner_begin", "inner_rebind")
            .connect("inner_rebind", "inner_check")
            .connect("inner_check", "inner_rollback")
            .connect("inner_rollback", "write")
            .build()
        )
        result = analyze(program)
        self.assertEqual(result.constrained_writes, ["write"])
        invalidated = [
            item
            for item in result.invalidated_facts
            if item.reason == REASON_TX_ROLLBACK
        ]
        self.assertEqual(len(invalidated), 1)
        self.assertEqual(
            invalidated[0].fact, FactKey("oid-77", 1, "write")
        )

    def test_outer_rollback_discards_inner_committed_facts(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .tx_begin("outer_begin")
            .tx_begin("inner_begin")
            .auth_check(
                "inner_check",
                object_id="oid-1",
                operation="write",
                target="order",
            )
            .tx_commit("inner_commit")
            .tx_rollback("outer_rollback")
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "outer_begin")
            .connect("outer_begin", "inner_begin")
            .connect("inner_begin", "inner_check")
            .connect("inner_check", "inner_commit")
            .connect("inner_commit", "outer_rollback")
            .connect("outer_rollback", "write")
            .build()
        )
        result = analyze(program)
        finding = _finding(result, "write")
        self.assertEqual(finding.reason, REASON_TX_ROLLBACK)
        rollback_items = [
            item
            for item in result.invalidated_facts
            if item.node_id == "outer_rollback"
        ]
        self.assertEqual(
            [item.fact for item in rollback_items],
            [FactKey("oid-1", 1, "write")],
        )

    def test_unmatched_rollback_raises(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .tx_rollback("lone_rollback")
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "lone_rollback")
            .connect("lone_rollback", "write")
            .build()
        )
        with self.assertRaises(ValueError):
            analyze(program)


if __name__ == "__main__":
    unittest.main()
