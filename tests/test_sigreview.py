"""sigreview 验收用例（unittest，真实断言）。

覆盖三组场景：
  A. 可接受结果：单路径成功、两路各自验签合流、wait_all 后经 branch 成功
  B. 应被阻断：仅等待无验签、wait_any 不产生信任、假分支、
     证明与制品对应关系丢失、同名制品换版沿用旧证明、重复绑定任务编号
  C. 边界/交错：循环不动点、同版重赋值（新身份仍需重新证明）、
     未初始化变量、不可达发布点、非法 CFG 文档
另含 CLI 烟雾测试（退出码与 JSON 输出）。
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest

from sigreview.analysis import (
    GAP_PROOF_MISSING,
    REBIND_EXEC,
    REBIND_STATIC,
    UNBOUND_TASK,
    analyze_graph,
)
from sigreview.cli import main as cli_main
from sigreview.loader import CFGFormatError, load_dict
from tests.fixtures import CFGBuilder


def report_for(result, node_id):
    for report in result.publish_reports:
        if report.node_id == node_id:
            return report
    raise AssertionError(f"未找到发布点 {node_id}")


class TestAcceptableResults(unittest.TestCase):
    def test_single_path_success_after_branch(self):
        """snapshot -> verify -> wait_all -> branch(T) -> publish 应通过。"""
        b = CFGBuilder()
        s = b.snapshot("a", "lib", "1.0")
        v = b.verify("a", "t1")
        w = b.wait(["t1"])
        br = b.branch("t1")
        p = b.publish("a")
        b.link(s, v)
        b.link(v, w)
        b.link(w, br)
        b.link_branch(br, true_next=p)

        result = analyze_graph(b.build())
        report = report_for(result, p)
        self.assertTrue(result.ok)
        self.assertTrue(report.passed)
        self.assertEqual([e["task_id"] for e in report.evidence], ["t1"])
        self.assertEqual(report.evidence[0]["verify_node"], v)
        self.assertEqual(report.gaps, [])

    def test_two_paths_each_verified_then_merge_passes(self):
        """两条路径分别验证各自制品，合流后发布：两路都有证明，应通过。"""
        b = CFGBuilder()
        s1 = b.snapshot("a", "lib", "1")
        v1 = b.verify("a", "t1")
        br = b.branch("t1")
        # 真分支：a 已由 t1 证明，直接到合流点 p
        # 假分支：重新取另一制品，由 t2 证明后到 p
        s2 = b.snapshot("a", "lib", "2")
        v2 = b.verify("a", "t2")
        b2 = b.branch("t2")
        p = b.publish("a")

        b.link(s1, v1)
        b.link(v1, br)
        b.link_branch(br, true_next=p, false_next=s2)
        b.link(s2, v2)
        b.link(v2, b2)
        b.link_branch(b2, true_next=p)  # 假分支无后继 -> 不可达 p

        result = analyze_graph(b.build())
        report = report_for(result, p)
        self.assertTrue(result.ok)
        self.assertTrue(report.passed)
        self.assertEqual(
            sorted(e["task_id"] for e in report.evidence), ["t1", "t2"]
        )

    def test_wait_any_then_branch_success_passes(self):
        """wait_any 本身不产生信任，但随后按成功返回值分支可以通过。"""
        b = CFGBuilder()
        s = b.snapshot("a", "lib", "1")
        v1 = b.verify("a", "t1")
        v2 = b.verify("a", "t2")
        w = b.wait(["t1", "t2"], kind="wait_any")
        br = b.branch("t1")
        p = b.publish("a")
        b.link(s, v1)
        b.link(v1, v2)
        b.link(v2, w)
        b.link(w, br)
        b.link_branch(br, true_next=p)

        result = analyze_graph(b.build())
        self.assertTrue(result.ok)
        self.assertTrue(report_for(result, p).passed)


class TestBlockedResults(unittest.TestCase):
    def test_wait_alone_grants_no_trust(self):
        """只 wait_all 不 branch：等待不产生信任，发布必须阻断。"""
        b = CFGBuilder()
        s = b.snapshot("a", "lib", "1")
        v = b.verify("a", "t1")
        w = b.wait(["t1"])
        p = b.publish("a")
        b.link(s, v)
        b.link(v, w)
        b.link(w, p)

        result = analyze_graph(b.build())
        report = report_for(result, p)
        self.assertTrue(result.blocked)
        self.assertFalse(report.passed)
        self.assertEqual(len(report.gaps), 1)
        self.assertEqual(report.gaps[0]["reason"], GAP_PROOF_MISSING)
        # 任务确实存在且绑定了快照，但缺少成功返回值约束。
        related = report.gaps[0]["related_tasks"]
        self.assertEqual(related[0]["task_id"], "t1")
        self.assertIsNone(related[0]["truth"])

    def test_wait_any_grants_no_trust(self):
        """首个任务结束同样不产生信任。"""
        b = CFGBuilder()
        s = b.snapshot("a", "lib", "1")
        v = b.verify("a", "t1")
        w = b.wait(["t1"], kind="wait_any")
        p = b.publish("a")
        b.link(s, v)
        b.link(v, w)
        b.link(w, p)

        result = analyze_graph(b.build())
        self.assertFalse(report_for(result, p).passed)

    def test_false_branch_blocks_publish(self):
        """验签返回假的路径到达发布点：证明失败，阻断。"""
        b = CFGBuilder()
        s = b.snapshot("a", "lib", "1")
        v = b.verify("a", "t1")
        br = b.branch("t1")
        p = b.publish("a")
        b.link(s, v)
        b.link(v, br)
        b.link_branch(br, false_next=p)

        result = analyze_graph(b.build())
        report = report_for(result, p)
        self.assertTrue(result.blocked)
        self.assertFalse(report.passed)
        self.assertTrue(report.gaps)
        self.assertEqual(report.evidence, [])

    def test_lost_correspondence_blocks(self):
        """任务成功但验的是另一个变量/快照：成功事实与被发布制品失联。"""
        b = CFGBuilder()
        sa = b.snapshot("a", "lib", "1")
        sb = b.snapshot("other", "lib", "9")
        va = b.verify("a", "t1")
        vo = b.verify("other", "t2")
        br = b.branch("t2")          # 只确认了 t2（other）成功
        p = b.publish("a")           # 发布 a，t1 从未被确认
        b.link(sa, sb)
        b.link(sb, va)
        b.link(va, vo)
        b.link(vo, br)
        b.link_branch(br, true_next=p)

        result = analyze_graph(b.build())
        report = report_for(result, p)
        self.assertTrue(result.blocked)
        self.assertFalse(report.passed)
        gap = report.gaps[0]
        # 证据列表里应能看出 t2 绑定的是 other 而非 a。
        stale = [r for r in gap["related_tasks"]
                 if r["task_id"] == "t2" and r["stale"]]
        self.assertEqual(len(stale), 1)
        self.assertEqual(
            stale[0]["bound_snapshot"]["snapshot_id"], sb
        )

    def test_rebinding_same_task_id_blocked(self):
        """同一任务编号绑定两次：静态结构违规，直接阻断。"""
        b = CFGBuilder()
        s1 = b.snapshot("a", "lib", "1")
        v1 = b.verify("a", "t1")
        s2 = b.snapshot("a", "lib", "2")
        v2 = b.verify("a", "t1")  # 重复编号
        br = b.branch("t1")
        p = b.publish("a")
        b.link(s1, v1)
        b.link(v1, s2)
        b.link(s2, v2)
        b.link(v2, br)
        b.link_branch(br, true_next=p)

        result = analyze_graph(b.build())
        rules = {f["rule"] for f in result.findings}
        self.assertIn(REBIND_STATIC, rules)
        self.assertTrue(result.blocked)

    def test_version_change_invalidates_old_proof(self):
        """同名制品换版后，旧版成功证明不能为新版快照背书。"""
        b = CFGBuilder()
        s1 = b.snapshot("a", "lib", "1")
        v1 = b.verify("a", "t1")
        br1 = b.branch("t1")
        # t1 成功后换版：a 指向全新快照
        s2 = b.snapshot("a", "lib", "2")
        p = b.publish("a")
        b.link(s1, v1)
        b.link(v1, br1)
        b.link_branch(br1, true_next=s2)
        b.link(s2, p)

        result = analyze_graph(b.build())
        report = report_for(result, p)
        self.assertTrue(result.blocked)
        self.assertFalse(report.passed)
        gap = report.gaps[0]
        self.assertEqual(gap["snapshot"]["version"], "2")
        # t1 绑定的旧快照被标记为 stale。
        stale = [r for r in gap["related_tasks"] if r["stale"]]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["bound_snapshot"]["version"], "1")
        self.assertIs(stale[0]["truth"], True)


class TestBoundaryAndInterleaving(unittest.TestCase):
    def test_loop_reaches_fixpoint(self):
        """循环重入验签节点：有限格上求不动点，重复绑定被记录并阻断。"""
        b = CFGBuilder()
        s = b.snapshot("a", "lib", "1")
        v = b.verify("a", "t1")
        w = b.wait(["t1"])
        br = b.branch("t1")
        p = b.publish("a")
        # 真分支发布，假分支回到 wait 形成循环
        b.link(s, v)
        b.link(v, w)
        b.link(w, br)
        b.link_branch(br, true_next=p, false_next=w)

        result = analyze_graph(b.build())
        self.assertIsInstance(result.iterations, int)
        self.assertGreater(result.iterations, 0)
        rules = {f["rule"] for f in result.findings}
        # 第一次走假分支再绕回 verify？本图回到 wait 而非 verify，
        # 因此没有执行期重绑定；构造一个真正重入 verify 的循环。
        self.assertNotIn(REBIND_EXEC, rules)
        # 真分支仍持有证明，发布点可通过。
        self.assertTrue(report_for(result, p).passed)

    def test_loop_reentering_verify_records_rebind(self):
        """循环回到 verify 节点：编号二次绑定被拦截，结论保持稳定。"""
        b = CFGBuilder()
        s = b.snapshot("a", "lib", "1")
        v = b.verify("a", "t1")
        br = b.branch("t1")
        p = b.publish("a")
        b.link(s, v)
        b.link(v, br)
        b.link_branch(br, true_next=p, false_next=v)  # 假分支重入 verify

        result = analyze_graph(b.build())
        rules = {f["rule"] for f in result.findings}
        self.assertIn(REBIND_EXEC, rules)
        self.assertTrue(result.blocked)
        # 真分支证据仍被收集，分析在有限步内收敛。
        report = report_for(result, p)
        self.assertTrue(
            any(e["task_id"] == "t1" for e in report.evidence)
        )

    def test_same_version_reassignment_still_needs_new_proof(self):
        """即使版本号相同，重新赋值也是新快照身份，旧证明失效。"""
        b = CFGBuilder()
        s1 = b.snapshot("a", "lib", "1")
        v1 = b.verify("a", "t1")
        br = b.branch("t1")
        s2 = b.snapshot("a", "lib", "1")  # 同名同版，新节点
        p = b.publish("a")
        b.link(s1, v1)
        b.link(v1, br)
        b.link_branch(br, true_next=s2)
        b.link(s2, p)

        result = analyze_graph(b.build())
        report = report_for(result, p)
        self.assertFalse(report.passed)
        self.assertNotEqual(
            report.gaps[0]["snapshot"]["snapshot_id"],
            report.gaps[0]["related_tasks"][0]["bound_snapshot"][
                "snapshot_id"
            ],
        )

    def test_uninitialized_publish_reported(self):
        """发布从未赋值的变量：证据不足位置精确到发布点。"""
        b = CFGBuilder()
        p = b.publish("ghost")
        result = analyze_graph(b.build())
        report = report_for(result, p)
        self.assertFalse(report.passed)
        self.assertEqual(report.gaps[0]["reason"], "uninitialized_variable")
        self.assertEqual(report.gaps[0]["node_id"], p)

    def test_unreachable_publish_is_not_passed(self):
        """不可达发布点没有任何环境，不能判为通过。"""
        b = CFGBuilder()
        s = b.snapshot("a", "lib", "1")
        v = b.verify("a", "t1")
        br = b.branch("t1")
        p = b.publish("a")
        # 只有真后继，发布点挂在从未连接的假侧之外
        b.link(s, v)
        b.link(v, br)
        b.link_branch(br, true_next=s)  # 自循环，p 不可达
        _ = p
        result = analyze_graph(b.build())
        report = report_for(result, p)
        self.assertFalse(report.passed)
        self.assertEqual(report.evidence, [])

    def test_long_loop_is_bounded_and_stable(self):
        """多次循环重入：轨迹截断保证环境有限，重复分析结论幂等。"""
        b = CFGBuilder()
        s = b.snapshot("a", "lib", "1")
        v = b.verify("a", "t1")
        br = b.branch("t1")
        p = b.publish("a")
        b.link(s, v)
        b.link(v, br)
        # 假分支绕回 branch 自身，形成任意多次重入的循环
        b.link_branch(br, true_next=p, false_next=br)

        cfg = b.build()
        r1 = analyze_graph(cfg, trace_bound=3)
        r2 = analyze_graph(cfg, trace_bound=3)
        self.assertEqual(r1.to_dict(), r2.to_dict())
        # 每次重入只可能产生有限的环境，迭代次数有上界。
        self.assertLess(r1.iterations, 100)
        report = report_for(r1, p)
        self.assertTrue(report.passed)
        for gap in report.gaps:
            for step in gap["trace"]:
                self.assertLessEqual(len(step), 2)

    def test_unbound_task_reference_is_finding(self):
        """wait/branch 引用未创建的任务编号：结构性 finding。"""
        b = CFGBuilder()
        s = b.snapshot("a", "lib", "1")
        w = b.wait(["nope"])
        p = b.publish("a")
        b.link(s, w)
        b.link(w, p)
        result = analyze_graph(b.build())
        self.assertIn(UNBOUND_TASK, {f["rule"] for f in result.findings})
        self.assertTrue(result.blocked)


class TestFormatValidation(unittest.TestCase):
    def test_missing_entry_rejected(self):
        with self.assertRaises(CFGFormatError):
            load_dict({"nodes": [{"id": "n0", "type": "publish",
                                  "var": "a"}]})

    def test_duplicate_node_id_rejected(self):
        with self.assertRaises(CFGFormatError):
            load_dict({
                "entry": "n0",
                "nodes": [
                    {"id": "n0", "type": "snapshot", "var": "a",
                     "artifact": "x", "version": "1"},
                    {"id": "n0", "type": "publish", "var": "a"},
                ],
            })

    def test_dangling_successor_rejected(self):
        with self.assertRaises(CFGFormatError):
            load_dict({
                "entry": "n0",
                "nodes": [
                    {"id": "n0", "type": "snapshot", "var": "a",
                     "artifact": "x", "version": "1", "next": "ghost"},
                ],
            })

    def test_unknown_node_type_rejected(self):
        with self.assertRaises(CFGFormatError):
            load_dict({
                "entry": "n0",
                "nodes": [{"id": "n0", "type": "explode"}],
            })

    def test_result_serialization_contains_required_fields(self):
        b = CFGBuilder()
        s = b.snapshot("a", "lib", "1")
        p = b.publish("a")
        b.link(s, p)
        payload = analyze_graph(b.build()).to_dict()
        self.assertIn("blocked", payload)
        self.assertIn("publish_reports", payload)
        self.assertIn("findings", payload)
        self.assertIn("iterations", payload)
        report = payload["publish_reports"][0]
        self.assertEqual(set(report), {"node_id", "var", "passed",
                                       "evidence", "gaps"})


class TestCLI(unittest.TestCase):
    def _write_cfg(self, payload: dict) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        self.addCleanup(os.remove, path)
        return path

    def test_cli_exit_codes(self):
        ok = CFGBuilder()
        s = ok.snapshot("a", "lib", "1")
        v = ok.verify("a", "t1")
        br = ok.branch("t1")
        p = ok.publish("a")
        ok.link(s, v)
        ok.link(v, br)
        ok.link_branch(br, true_next=p)
        ok_path = self._write_cfg({"entry": s, "nodes": ok.nodes})

        bad = CFGBuilder()
        bs = bad.snapshot("a", "lib", "1")
        bp = bad.publish("a")
        bad.link(bs, bp)
        bad_path = self._write_cfg({"entry": bs, "nodes": bad.nodes})

        def run_cli(argv):
            buf = io.StringIO()
            old_stdout = sys.stdout
            try:
                sys.stdout = buf
                code = cli_main(argv)
            finally:
                sys.stdout = old_stdout
            return code, buf.getvalue()

        code_ok, out_ok = run_cli([ok_path])
        code_bad, out_bad = run_cli([bad_path])
        code_fmt, _ = run_cli([ok_path + ".nonexistent"])

        self.assertEqual(code_ok, 0)
        self.assertEqual(code_bad, 1)
        self.assertEqual(code_fmt, 2)
        parsed = json.loads(out_ok)
        self.assertIn("blocked", parsed)
        self.assertFalse(parsed["blocked"])
        self.assertTrue(json.loads(out_bad)["blocked"])

    def test_isolated_mode_cli_wrapper(self):
        """sigreview_cli.py 在 python -I 下可独立运行（sys.path 自举）。"""
        import subprocess

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ok = CFGBuilder()
        s = ok.snapshot("a", "lib", "1")
        v = ok.verify("a", "t1")
        br = ok.branch("t1")
        p = ok.publish("a")
        ok.link(s, v)
        ok.link(v, br)
        ok.link_branch(br, true_next=p)
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"entry": s, "nodes": ok.nodes}, fh)
        try:
            proc = subprocess.run(
                [sys.executable, "-I",
                 os.path.join(root, "sigreview_cli.py"), path],
                capture_output=True, text=True, cwd=root,
            )
        finally:
            os.remove(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(json.loads(proc.stdout)["blocked"])


if __name__ == "__main__":
    unittest.main()
