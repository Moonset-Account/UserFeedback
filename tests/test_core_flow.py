"""核心数据流场景：授权命中、版本陈旧、对象替换、操作不匹配、未绑定。"""

import unittest

from authflow import (
    FactKey,
    ProgramBuilder,
    analyze,
)
from authflow.analysis import (
    REASON_NO_AUTHORIZATION,
    REASON_OBJECT_REPLACED,
    REASON_OPERATION_MISMATCH,
    REASON_OVERWRITTEN_BINDING,
    REASON_STALE_VERSION,
    REASON_UNBOUND,
    REASON_VERSION_ADVANCED,
)


def _finding(result, node_id):
    for finding in result.findings:
        if finding.node_id == node_id:
            return finding
    raise AssertionError(f"未找到写入节点 {node_id} 的判定")


class HappyPathTest(unittest.TestCase):
    def test_authorized_write_is_constrained(self):
        # entry(order -> oid-1 v1) -> auth_check -> write
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .auth_check(
                "check", object_id="oid-1", operation="write", target="order"
            )
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "check")
            .connect("check", "write")
            .build()
        )
        result = analyze(program)

        self.assertEqual(result.constrained_writes, ["write"])
        self.assertEqual(result.findings, [])
        evaluation = result.evaluations["write"]
        self.assertTrue(evaluation.authorized)
        self.assertEqual(
            evaluation.matched_fact, FactKey("oid-1", 1, "write")
        )
        self.assertEqual(evaluation.fact_origins, frozenset({"check"}))
        self.assertEqual(result.reachable_calls, ["write"])
        self.assertEqual(result.unreachable_calls, [])
        # 事实在出口仍然存活
        self.assertIn(
            FactKey("oid-1", 1, "write"), result.facts_at_exit()
        )


class StaleVersionTest(unittest.TestCase):
    def _program(self):
        # check(v1) -> advance(oid-1 -> v2, rebind) -> write
        return (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .auth_check(
                "check", object_id="oid-1", operation="write", target="order"
            )
            .call(
                "advance",
                target="order",
                advances={"oid-1": 2},
                rebindings={"order": ("oid-1", 2)},
            )
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "check")
            .connect("check", "advance")
            .connect("advance", "write")
            .build()
        )

    def test_stale_version_finding(self):
        result = analyze(self._program())
        finding = _finding(result, "write")
        self.assertEqual(finding.reason, REASON_STALE_VERSION)
        self.assertEqual(finding.object_id, "oid-1")
        self.assertEqual(finding.version, 2)
        self.assertEqual(
            finding.matched_fact and finding.matched_fact.version, 1
        )

    def test_invalidation_record_for_advance(self):
        result = analyze(self._program())
        invalidations = [
            item
            for item in result.invalidated_facts
            if item.reason == REASON_VERSION_ADVANCED
        ]
        self.assertEqual(len(invalidations), 1)
        self.assertEqual(
            invalidations[0].fact, FactKey("oid-1", 1, "write")
        )
        self.assertEqual(invalidations[0].node_id, "advance")
        self.assertEqual(
            invalidations[0].origins, frozenset({"check"})
        )

    def test_broken_chain_marks_advance_and_write(self):
        result = analyze(self._program())
        chain = _finding(result, "write").chain
        node_ids = [link.node_id for link in chain]
        self.assertEqual(node_ids, ["check", "advance", "write"])
        self.assertEqual(chain[0].role, "auth_check")
        self.assertFalse(chain[0].is_break)
        self.assertTrue(chain[1].is_break)
        self.assertEqual(chain[1].role, "version_advance")
        self.assertTrue(chain[2].is_break)
        self.assertEqual(chain[2].role, "unauthorized_write")


class ObjectReplacementTest(unittest.TestCase):
    def test_replaced_object_fact_invalidated_and_write_unauthorized(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .auth_check(
                "check", object_id="oid-1", operation="write", target="order"
            )
            .call(
                "replace",
                target="order",
                replaces=frozenset({"oid-1"}),
                rebindings={"order": ("oid-2", 1)},
            )
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "check")
            .connect("check", "replace")
            .connect("replace", "write")
            .build()
        )
        result = analyze(program)

        invalidations = [
            item
            for item in result.invalidated_facts
            if item.reason == REASON_OBJECT_REPLACED
        ]
        self.assertEqual(len(invalidations), 1)
        self.assertEqual(
            invalidations[0].fact, FactKey("oid-1", 1, "write")
        )

        finding = _finding(result, "write")
        self.assertEqual(
            finding.reason, REASON_OVERWRITTEN_BINDING
        )
        self.assertEqual(finding.object_id, "oid-2")
        break_roles = {link.role for link in finding.chain if link.is_break}
        self.assertIn("replacement", break_roles)
        self.assertIn("unauthorized_write", break_roles)


class OperationMismatchTest(unittest.TestCase):
    def test_different_operation_does_not_authorize(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .auth_check(
                "check", object_id="oid-1", operation="read", target="order"
            )
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "check")
            .connect("check", "write")
            .build()
        )
        result = analyze(program)
        finding = _finding(result, "write")
        self.assertEqual(finding.reason, REASON_OPERATION_MISMATCH)
        self.assertEqual(
            finding.matched_fact, FactKey("oid-1", 1, "read")
        )


class UnboundTest(unittest.TestCase):
    def test_write_without_binding(self):
        program = (
            ProgramBuilder()
            .entry("entry")
            .call("write", target="ghost", operation="write", is_write=True)
            .connect("entry", "write")
            .build()
        )
        result = analyze(program)
        finding = _finding(result, "write")
        self.assertEqual(finding.reason, REASON_UNBOUND)
        self.assertIsNone(finding.object_id)

    def test_alias_of_unbound_rhs_drops_binding(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .alias("alias_node", lhs="snapshot", rhs="ghost")
            .call(
                "write", target="snapshot", operation="write", is_write=True
            )
            .connect("entry", "alias_node")
            .connect("alias_node", "write")
            .build()
        )
        result = analyze(program)
        self.assertEqual(
            _finding(result, "write").reason, REASON_UNBOUND
        )

    def test_no_fact_at_all_is_no_authorization(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "write")
            .build()
        )
        result = analyze(program)
        finding = _finding(result, "write")
        self.assertEqual(finding.reason, REASON_NO_AUTHORIZATION)
        self.assertEqual(finding.object_id, "oid-1")
        self.assertEqual(finding.version, 1)


if __name__ == "__main__":
    unittest.main()
