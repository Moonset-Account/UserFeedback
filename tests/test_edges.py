"""补充边界：断链角色、多事实交互、结果确定性与环境快照。"""

import unittest

from authflow import FactKey, ProgramBuilder, analyze
from authflow.analysis import (
    REASON_AUTHORIZED,
    REASON_DIVERGENT_VARIABLE,
    REASON_NO_AUTHORIZATION,
    REASON_OBJECT_REPLACED,
    REASON_OPERATION_MISMATCH,
    REASON_OVERWRITTEN_BINDING,
    REASON_STALE_VERSION,
)


class ChainRoleTest(unittest.TestCase):
    def test_merge_divergence_chain_marks_merge_break(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .branch("br")
            .call("rebind_t", rebindings={"order": ("oid-2", 1)})
            .call("rebind_f", rebindings={"order": ("oid-3", 1)})
            .merge("join")
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "br")
            .branch_edges("br", "rebind_t", "rebind_f")
            .connect("rebind_t", "join")
            .connect("rebind_f", "join")
            .connect("join", "write")
            .build()
        )
        result = analyze(program)
        finding = result.findings[0]
        self.assertEqual(finding.reason, REASON_DIVERGENT_VARIABLE)
        merge_link = next(
            link for link in finding.chain if link.node_id == "join"
        )
        self.assertTrue(merge_link.is_break)
        self.assertEqual(merge_link.role, "merge")
        write_link = finding.chain[-1]
        self.assertEqual(write_link.role, "unauthorized_write")

    def test_replacement_without_prior_fact_chain(self):
        # 从未授权过的对象被替换后写入：归为外部来源覆盖
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .call(
                "replace",
                target="order",
                replaces=frozenset({"oid-1"}),
                rebindings={"order": ("oid-2", 1)},
            )
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "replace")
            .connect("replace", "write")
            .build()
        )
        result = analyze(program)
        finding = result.findings[0]
        self.assertEqual(finding.reason, REASON_OVERWRITTEN_BINDING)
        self.assertEqual(
            [link.node_id for link in finding.chain],
            ["replace", "write"],
        )

    def test_replacement_invalidation_is_recorded_even_without_write(self):
        # 只有授权 + 替换、没有写入：失效事实仍被记录，且没有 Finding
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
            .connect("entry", "check")
            .connect("check", "replace")
            .build()
        )
        result = analyze(program)
        self.assertEqual(result.findings, [])
        self.assertEqual(
            [item.reason for item in result.invalidated_facts],
            [REASON_OBJECT_REPLACED],
        )


class MultiFactTest(unittest.TestCase):
    def test_two_operations_one_authorized_one_not(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .auth_check(
                "check_read",
                object_id="oid-1",
                operation="read",
                target="order",
            )
            .auth_check(
                "check_write",
                object_id="oid-1",
                operation="write",
                target="order",
            )
            .call("w", target="order", operation="write", is_write=True)
            .call("d", target="order", operation="delete", is_write=True)
            .connect("entry", "check_read")
            .connect("check_read", "check_write")
            .connect("check_write", "w")
            .connect("w", "d")
            .build()
        )
        result = analyze(program)
        self.assertEqual(result.constrained_writes, ["w"])
        delete_finding = next(f for f in result.findings if f.node_id == "d")
        self.assertEqual(
            delete_finding.reason, REASON_OPERATION_MISMATCH
        )

    def test_fact_survives_unrelated_object_advance(self):
        program = (
            ProgramBuilder()
            .entry(
                "entry",
                {"order": ("oid-1", 1), "invoice": ("oid-2", 1)},
            )
            .auth_check(
                "check", object_id="oid-1", operation="write", target="order"
            )
            .call(
                "advance_other",
                target="invoice",
                advances={"oid-2": 2},
                rebindings={"invoice": ("oid-2", 2)},
            )
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "check")
            .connect("check", "advance_other")
            .connect("advance_other", "write")
            .build()
        )
        result = analyze(program)
        self.assertEqual(result.constrained_writes, ["write"])
        self.assertEqual(
            result.evaluations["write"].reason, REASON_AUTHORIZED
        )

    def test_advance_to_same_version_does_not_invalidate(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .auth_check(
                "check", object_id="oid-1", operation="write", target="order"
            )
            .call(
                "noop_advance",
                target="order",
                advances={"oid-1": 1},
                rebindings={"order": ("oid-1", 1)},
            )
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "check")
            .connect("check", "noop_advance")
            .connect("noop_advance", "write")
            .build()
        )
        result = analyze(program)
        self.assertEqual(result.constrained_writes, ["write"])
        self.assertEqual(result.invalidated_facts, [])


class DeterminismTest(unittest.TestCase):
    def test_repeated_runs_identical(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .branch("br")
            .call("rebind_t", rebindings={"order": ("oid-2", 1)})
            .call("rebind_f", rebindings={"order": ("oid-3", 1)})
            .merge("join")
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "br")
            .branch_edges("br", "rebind_t", "rebind_f")
            .connect("rebind_t", "join")
            .connect("rebind_f", "join")
            .connect("join", "write")
            .build()
        )
        first = analyze(program)
        second = analyze(program)
        self.assertEqual(
            [(f.node_id, f.reason) for f in first.findings],
            [(f.node_id, f.reason) for f in second.findings],
        )
        self.assertEqual(
            first.reachable_calls, second.reachable_calls
        )
        self.assertEqual(
            first.unreachable_calls, second.unreachable_calls
        )


class NoAuthorizationChainTest(unittest.TestCase):
    def test_plain_no_authorization_chain_contains_write_only(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "write")
            .build()
        )
        result = analyze(program)
        finding = result.findings[0]
        self.assertEqual(finding.reason, REASON_NO_AUTHORIZATION)
        self.assertEqual(
            [link.node_id for link in finding.chain], ["write"]
        )
        self.assertTrue(finding.chain[0].is_break)

    def test_input_and_output_envs_captured_for_reachable_nodes(self):
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
        self.assertEqual(
            set(result.input_envs), {"entry", "check", "write"}
        )
        self.assertEqual(
            set(result.output_envs), {"entry", "check", "write"}
        )
        # 输入环境在检查节点之前不含事实；输出环境在检查之后含事实
        self.assertNotIn(
            FactKey("oid-1", 1, "write"), result.input_envs["check"].facts
        )
        self.assertIn(
            FactKey("oid-1", 1, "write"), result.output_envs["check"].facts
        )


if __name__ == "__main__":
    unittest.main()
