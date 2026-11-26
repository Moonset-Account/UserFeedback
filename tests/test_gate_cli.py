"""门禁预算判定与真实命令行子进程验证。"""

import json
import os
import subprocess
import sys
import unittest
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from budget_guard import analyze, evaluate  # noqa: E402
from budget_guard.model import B  # noqa: E402

EX_GRAPH = ROOT / "examples" / "operation_graph.json"
EX_EVIDENCE = ROOT / "examples" / "evidence.json"


class TestGate(unittest.TestCase):
    def test_within_budget(self):
        rep = analyze({"op": "alloc", "bytes": 10, "records": 2})
        d = evaluate(rep, max_bytes=10, max_records=2)
        self.assertTrue(d.allowed)
        self.assertEqual(d.violations, [])

    def test_bytes_exceeded(self):
        rep = analyze({"op": "alloc", "bytes": 11})
        d = evaluate(rep, max_bytes=10)
        self.assertFalse(d.allowed)
        v = d.violations[0]
        self.assertEqual(v.code, B.BYTES_BUDGET_EXCEEDED)
        self.assertEqual(v.bound, 11)
        self.assertEqual(v.limit, 10)

    def test_records_exceeded_independently(self):
        rep = analyze({"op": "alloc", "bytes": 1, "records": 5})
        d = evaluate(rep, max_bytes=100, max_records=4)
        self.assertFalse(d.allowed)
        self.assertEqual(d.violations[0].code, B.RECORDS_BUDGET_EXCEEDED)

    def test_unprovable_bound_violation(self):
        rep = analyze({"op": "segments",
                       "count": {"evidence": "n"},
                       "body": {"op": "alloc", "bytes": 1}})
        d = evaluate(rep, max_bytes=10**9)
        self.assertFalse(d.allowed)
        self.assertTrue(any(v.code == B.UNPROVABLE_BOUND
                            for v in d.violations))

    def test_no_budget_but_blocking_issue_still_denies(self):
        rep = analyze({"op": "call", "target": "missing"})
        self.assertFalse(evaluate(rep).allowed)

    def test_invalid_budget_type(self):
        rep = analyze({"op": "alloc"})
        with self.assertRaises(ValueError):
            evaluate(rep, max_bytes=-1)
        with self.assertRaises(ValueError):
            evaluate(rep, max_records=True)

    def test_decision_dict_serializable(self):
        rep = analyze({"op": "alloc", "bytes": 1})
        d = evaluate(rep, max_bytes=10)
        json.dumps(d.as_dict(), ensure_ascii=False)


class TestCliSubprocess(unittest.TestCase):
    """用真实子进程验证 python3 -m budget_guard 的退出码与 JSON 输出。"""

    def _run(self, *extra: str):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "budget_guard", *extra],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_example_graph_allowed_json(self):
        proc = self._run(
            str(EX_GRAPH), "--evidence", str(EX_EVIDENCE),
            "--max-bytes", "65536", "--max-records", "100",
            "--format", "json",
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        data = json.loads(proc.stdout)
        self.assertTrue(data["allowed"])
        # 与 README 中记录的手工推导一致
        self.assertEqual(data["total"]["bytes"], 37120)
        self.assertEqual(data["total"]["records"], 66)

    def test_example_graph_budget_denied_exit_3(self):
        proc = self._run(
            str(EX_GRAPH), "--evidence", str(EX_EVIDENCE),
            "--max-bytes", "100", "--format", "json",
        )
        self.assertEqual(proc.returncode, 3)
        data = json.loads(proc.stdout)
        self.assertFalse(data["allowed"])
        self.assertIn("BYTES_BUDGET_EXCEEDED",
                      [v["code"] for v in data["violations"]])

    def test_missing_evidence_denied(self):
        proc = self._run(str(EX_GRAPH), "--format", "json")
        self.assertEqual(proc.returncode, 3)
        data = json.loads(proc.stdout)
        self.assertFalse(data["allowed"])
        self.assertTrue(data["rejectable_branches"])

    def test_reject_branch_changes_exit_code(self):
        # 拒绝两个证据相关区域后，只剩固定的 alloc/ref 成本
        proc = self._run(
            str(EX_GRAPH),
            "--reject", "segmented",
            "--reject", "nested",
            "--max-bytes", "5000", "--max-records", "10",
            "--format", "json",
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        data = json.loads(proc.stdout)
        self.assertTrue(data["allowed"])
        # 4096 manifest + 256 anonymous tail
        self.assertEqual(data["total"]["bytes"], 4352)

    def test_text_format(self):
        proc = self._run(
            str(EX_GRAPH), "--evidence", str(EX_EVIDENCE),
            "--max-bytes", "65536", "--max-records", "100",
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("budget_guard", proc.stdout)
        self.assertIn("ALLOW", proc.stdout)

    def test_missing_graph_file_exit_4(self):
        proc = self._run(str(ROOT / "tests" / "fixtures" / "nope.json"))
        self.assertEqual(proc.returncode, 4)

    def test_unknown_reject_id_exit_4(self):
        proc = self._run(str(EX_GRAPH), "--reject", "ghost-region")
        self.assertEqual(proc.returncode, 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
