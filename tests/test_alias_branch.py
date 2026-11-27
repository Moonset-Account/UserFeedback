"""别名、对象副本、分支合流（must 事实交集）与交错路径场景。"""

import unittest

from authflow import FactKey, ProgramBuilder, analyze
from authflow.analysis import (
    REASON_AUTHORIZED,
    REASON_DIVERGENT_OBJECT,
    REASON_DIVERGENT_VARIABLE,
    REASON_NO_AUTHORIZATION,
    REASON_OBJECT_COPY,
    REASON_STALE_VERSION,
)


def _finding(result, node_id):
    for finding in result.findings:
        if finding.node_id == node_id:
            return finding
    raise AssertionError(f"未找到写入节点 {node_id} 的判定")


class AliasTest(unittest.TestCase):
    def test_alias_shares_fact_and_authorizes_write(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .auth_check(
                "check", object_id="oid-1", operation="write", target="order"
            )
            .alias("alias_node", lhs="handle", rhs="order")
            .call("write", target="handle", operation="write", is_write=True)
            .connect("entry", "check")
            .connect("check", "alias_node")
            .connect("alias_node", "write")
            .build()
        )
        result = analyze(program)
        self.assertEqual(result.constrained_writes, ["write"])
        self.assertEqual(result.findings, [])

    def test_multihop_alias_chain_keeps_fact(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"a": ("oid-1", 1)})
            .auth_check(
                "check", object_id="oid-1", operation="write", target="a"
            )
            .alias("al1", lhs="b", rhs="a")
            .alias("al2", lhs="c", rhs="b")
            .call("write", target="c", operation="write", is_write=True)
            .connect("entry", "check")
            .connect("check", "al1")
            .connect("al1", "al2")
            .connect("al2", "write")
            .build()
        )
        result = analyze(program)
        self.assertEqual(result.constrained_writes, ["write"])

    def test_alias_advanced_version_does_not_inherit_old_fact(self):
        # 别名把 lhs 推进到新版本：引用同一对象，但版本高于授权版本
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .auth_check(
                "check", object_id="oid-1", operation="write", target="order"
            )
            .alias(
                "alias_node",
                lhs="handle",
                rhs="order",
                new_version=2,
            )
            .call("write", target="handle", operation="write", is_write=True)
            .connect("entry", "check")
            .connect("check", "alias_node")
            .connect("alias_node", "write")
            .build()
        )
        result = analyze(program)
        finding = _finding(result, "write")
        self.assertEqual(finding.reason, REASON_STALE_VERSION)
        self.assertEqual(finding.version, 2)


class ObjectCopyTest(unittest.TestCase):
    def test_copy_does_not_inherit_fact(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .auth_check(
                "check", object_id="oid-1", operation="write", target="order"
            )
            .alias(
                "copy_node",
                lhs="snapshot",
                rhs="order",
                is_copy=True,
                new_object_id="oid-1-copy",
            )
            .call(
                "write",
                target="snapshot",
                operation="write",
                is_write=True,
            )
            .connect("entry", "check")
            .connect("check", "copy_node")
            .connect("copy_node", "write")
            .build()
        )
        result = analyze(program)
        finding = _finding(result, "write")
        self.assertEqual(finding.reason, REASON_OBJECT_COPY)
        self.assertEqual(finding.object_id, "oid-1-copy")
        roles = [(link.role, link.is_break) for link in finding.chain]
        self.assertIn(("copy", True), roles)
        self.assertIn(("unauthorized_write", True), roles)
        # 原对象的事实仍然存活，副本与原对象被区分对待
        self.assertIn(
            FactKey("oid-1", 1, "write"), result.facts_at_exit()
        )

    def test_copy_requires_new_object_id(self):
        builder = ProgramBuilder().entry("entry")
        with self.assertRaises(ValueError):
            builder.alias(
                "bad_copy", lhs="x", rhs="y", is_copy=True
            )


class BranchMergeTest(unittest.TestCase):
    def test_fact_on_only_one_branch_lost_at_merge(self):
        # 分支：true 上做授权检查，false 上没有 -> 合流后 must 事实消失
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .branch("br", condition="needs_check")
            .auth_check(
                "check", object_id="oid-1", operation="write", target="order"
            )
            .merge("join")
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "br")
            .branch_edges("br", "check", "join")
            .connect("check", "join")
            .connect("join", "write")
            .build()
        )
        result = analyze(program)
        finding = _finding(result, "write")
        # 事实只在 true 分支上成立，合流取交集后 must 事实不存在；
        # 绑定本身未变化、也没有版本推进事件，因此归为 no_authorization。
        self.assertEqual(finding.reason, REASON_NO_AUTHORIZATION)
        # 合流差异被记录
        self.assertEqual(len(result.merge_gaps), 1)
        gap = result.merge_gaps[0]
        self.assertEqual(gap.merge_node_id, "join")
        self.assertIn(
            FactKey("oid-1", 1, "write"), gap.fact_diffs
        )

    def test_fact_on_both_branches_survives_merge(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .branch("br")
            .auth_check(
                "check_t",
                object_id="oid-1",
                operation="write",
                target="order",
            )
            .auth_check(
                "check_f",
                object_id="oid-1",
                operation="write",
                target="order",
            )
            .merge("join")
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "br")
            .branch_edges("br", "check_t", "check_f")
            .connect("check_t", "join")
            .connect("check_f", "join")
            .connect("join", "write")
            .build()
        )
        result = analyze(program)
        self.assertEqual(result.constrained_writes, ["write"])
        self.assertEqual(result.findings, [])
        self.assertEqual(result.merge_gaps, [])

    def test_divergent_bindings_at_merge(self):
        # 两条路径把 order 重绑定到不同对象 -> 合流后变量状态发散
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .branch("br")
            .call(
                "rebind_t",
                rebindings={"order": ("oid-2", 1)},
            )
            .call(
                "rebind_f",
                rebindings={"order": ("oid-3", 1)},
            )
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
        finding = _finding(result, "write")
        self.assertEqual(finding.reason, REASON_DIVERGENT_VARIABLE)
        gap = result.merge_gaps[0]
        self.assertIn("order", gap.variable_diffs)
        self.assertIn("order", gap.unknown_variables)

    def test_divergent_versions_at_merge(self):
        # 同一对象在两条路径上被推进到不同版本
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .branch("br")
            .call(
                "adv_t",
                advances={"oid-1": 2},
                rebindings={"order": ("oid-1", 2)},
            )
            .call(
                "adv_f",
                advances={"oid-1": 3},
                rebindings={"order": ("oid-1", 3)},
            )
            .merge("join")
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "br")
            .branch_edges("br", "adv_t", "adv_f")
            .connect("adv_t", "join")
            .connect("adv_f", "join")
            .connect("join", "write")
            .build()
        )
        result = analyze(program)
        finding = _finding(result, "write")
        self.assertEqual(finding.reason, REASON_DIVERGENT_OBJECT)
        gap = result.merge_gaps[0]
        self.assertEqual(gap.object_version_diffs["oid-1"], (2, 3))
        self.assertIn("oid-1", gap.unknown_objects)


class InterleavingTest(unittest.TestCase):
    def test_auth_advance_merge_then_reauthorize_on_rebound_handle(self):
        # 分支 A：授权后推进到 v2（旧事实失效）；分支 B：原样汇合。
        # 合流后 order 状态发散；随后调用把 order 重绑定到一个具体的新
        # 句柄 oid-9，重新授权后写入应再次受约束。
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .branch("br")
            .auth_check(
                "check_v1",
                object_id="oid-1",
                operation="write",
                target="order",
            )
            .call(
                "advance",
                advances={"oid-1": 2},
                rebindings={"order": ("oid-1", 2)},
            )
            .merge("join")
            .call(
                "rebind",
                rebindings={"order": ("oid-9", 1)},
            )
            .auth_check(
                "check_new",
                object_id="oid-9",
                operation="write",
                target="order",
            )
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "br")
            .branch_edges("br", "check_v1", "join")
            .connect("check_v1", "advance")
            .connect("advance", "join")
            .connect("join", "rebind")
            .connect("rebind", "check_new")
            .connect("check_new", "write")
            .build()
        )
        result = analyze(program)
        # 合流证据保留：版本发散被记录
        self.assertTrue(result.merge_gaps)
        self.assertEqual(
            result.merge_gaps[0].merge_node_id, "join"
        )
        # 重绑定 + 重新授权后，写入重新受约束
        self.assertEqual(result.constrained_writes, ["write"])
        self.assertEqual(result.findings, [])
        self.assertEqual(
            result.evaluations["write"].reason, REASON_AUTHORIZED
        )

    def test_linear_interleaving_reauthorize_after_advance(self):
        # 线性交错：check(v1) -> advance(v2) -> check(v2) -> write 受约束
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .auth_check(
                "check_v1",
                object_id="oid-1",
                operation="write",
                target="order",
            )
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
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "check_v1")
            .connect("check_v1", "advance")
            .connect("advance", "check_v2")
            .connect("check_v2", "write")
            .build()
        )
        result = analyze(program)
        self.assertEqual(result.constrained_writes, ["write"])
        # 旧事实已失效，出口只剩 v2 事实
        self.assertEqual(
            result.facts_at_exit(),
            {FactKey("oid-1", 2, "write"): frozenset({"check_v2"})},
        )

    def test_non_write_calls_are_not_findings(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .call("observe", target="order", operation="read")
            .connect("entry", "observe")
            .build()
        )
        result = analyze(program)
        self.assertEqual(result.findings, [])
        self.assertEqual(result.reachable_calls, ["observe"])
        self.assertEqual(
            result.evaluations, {}
        )

    def test_authorized_constant_exposed(self):
        self.assertEqual(REASON_AUTHORIZED, "authorized")


if __name__ == "__main__":
    unittest.main()
