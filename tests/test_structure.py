"""程序结构校验、可达性区分与显式版本断言。"""

import unittest

from authflow import ProgramBuilder, analyze
from authflow.errors import ProgramStructureError


class ReachabilityTest(unittest.TestCase):
    def test_unreachable_calls_are_separated_and_not_findings(self):
        # 入口链上是合法的授权写入；另有一个从入口不可达的写入孤岛
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .auth_check(
                "check", object_id="oid-1", operation="write", target="order"
            )
            .call("write_ok", target="order", operation="write",
                  is_write=True)
            .call("orphan_write", target="order", operation="write",
                  is_write=True)
            .connect("entry", "check")
            .connect("check", "write_ok")
            .build()
        )
        result = analyze(program)

        self.assertEqual(result.constrained_writes, ["write_ok"])
        self.assertEqual(result.reachable_calls, ["write_ok"])
        self.assertEqual(result.unreachable_calls, ["orphan_write"])
        # 不可达调用不产生 Finding，也不产生判定记录
        self.assertNotIn("orphan_write", result.evaluations)
        self.assertEqual(
            [f.node_id for f in result.findings], []
        )

    def test_unreachable_region_is_fully_excluded(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .call("reachable_read", target="order", operation="read")
            .call("orphan_read", target="order", operation="read")
            .connect("entry", "reachable_read")
            .build()
        )
        result = analyze(program)
        self.assertEqual(result.reachable_calls, ["reachable_read"])
        self.assertEqual(result.unreachable_calls, ["orphan_read"])
        self.assertNotIn("orphan_read", result.input_envs)
        self.assertNotIn("orphan_read", result.output_envs)


class StructureValidationTest(unittest.TestCase):
    def test_duplicate_node_id_rejected(self):
        builder = ProgramBuilder().entry("n")
        with self.assertRaises(ProgramStructureError):
            builder.merge("n")

    def test_missing_entry_rejected(self):
        builder = ProgramBuilder().merge("m")
        with self.assertRaises(ProgramStructureError):
            builder.build()

    def test_edge_to_unknown_node_rejected(self):
        builder = (
            ProgramBuilder()
            .entry("entry")
            .connect("entry", "ghost")
        )
        with self.assertRaises(ProgramStructureError):
            builder.build()

    def test_branch_requires_true_and_false_edges(self):
        builder = (
            ProgramBuilder()
            .entry("entry")
            .branch("br")
            .merge("join")
            .connect("entry", "br")
            .connect("br", "join", "true")
        )
        with self.assertRaises(ProgramStructureError):
            builder.build()

    def test_cycle_rejected(self):
        builder = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .call("a", target="order", operation="read")
            .call("b", target="order", operation="read")
            .connect("entry", "a")
            .connect("a", "b")
            .connect("b", "a")
        )
        with self.assertRaises(ProgramStructureError):
            builder.build()

    def test_auth_check_on_unbound_target_rejected(self):
        program = (
            ProgramBuilder()
            .entry("entry")
            .auth_check(
                "check", object_id="oid-1", operation="write", target="order"
            )
            .connect("entry", "check")
            .build()
        )
        with self.assertRaises(ProgramStructureError):
            analyze(program)

    def test_auth_check_object_mismatch_rejected(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .auth_check(
                "check", object_id="oid-2", operation="write", target="order"
            )
            .connect("entry", "check")
            .build()
        )
        with self.assertRaises(ProgramStructureError):
            analyze(program)

    def test_delayed_version_requires_target(self):
        program = (
            ProgramBuilder()
            .entry("entry")
            .auth_check("check", object_id="oid-1", operation="write")
            .connect("entry", "check")
            .build()
        )
        with self.assertRaises(ProgramStructureError):
            analyze(program)


class ExplicitVersionTest(unittest.TestCase):
    def test_explicit_version_binding(self):
        # 显式版本断言：检查直接声明 v1，不依赖延迟绑定
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .auth_check(
                "check",
                object_id="oid-1",
                operation="write",
                version=1,
                target="order",
            )
            .call("write", target="order", operation="write", is_write=True)
            .connect("entry", "check")
            .connect("check", "write")
            .build()
        )
        result = analyze(program)
        self.assertEqual(result.constrained_writes, ["write"])

    def test_explicit_version_mismatch_with_binding_rejected(self):
        program = (
            ProgramBuilder()
            .entry("entry", {"order": ("oid-1", 1)})
            .auth_check(
                "check",
                object_id="oid-1",
                operation="write",
                version=2,
                target="order",
            )
            .connect("entry", "check")
            .build()
        )
        # 显式版本与绑定版本不一致属于结构错误，避免静默产生错误事实
        with self.assertRaises(ProgramStructureError):
            analyze(program)


if __name__ == "__main__":
    unittest.main()
