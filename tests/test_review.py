"""验收用例：初始化覆盖审查的交叉约束。

覆盖：
- 可接受结果：清零分支与字段写分支汇合后，复制区间在所有路径上均已初始化；
- 应被拒绝结果：字段间对齐空洞只在清零分支被覆盖，汇合后复制被判 UNSAFE；
- 边界/交错场景：复制终点恰好落在已初始化边界（安全）与越出一字节（拒绝）、
  相邻写入拼接、嵌套分支中只有部分子路径初始化、动态长度留待核实。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bufreview.analysis import Analyzer  # noqa: E402
from bufreview.cli import main as cli_main  # noqa: E402
from bufreview.model import load_spec  # noqa: E402


def analyze(spec_dict):
    return Analyzer(load_spec(spec_dict)).run()


def diamond_spec(fields, field_ops, copies, size=32):
    """清零整个缓冲区的分支与只写字段的分支汇合后执行复制。"""
    return {
        "buffer": {"size": size, "fields": fields},
        "entry": "entry",
        "nodes": [
            {"id": "entry", "ops": [], "succ": ["zero_all", "write_fields"]},
            {"id": "zero_all", "ops": [{"op": "zero_buffer"}], "succ": ["merge"]},
            {"id": "write_fields", "ops": field_ops, "succ": ["merge"]},
            {"id": "merge", "ops": copies, "succ": []},
        ],
    }


FULL_FIELDS = [
    {"name": "hdr", "offset": 0, "size": 8},
    {"name": "body", "offset": 8, "size": 16},
    {"name": "tail", "offset": 24, "size": 8},
]
WRITE_ALL = [{"op": "write_field", "field": f} for f in ("hdr", "body", "tail")]


class AcceptableTests(unittest.TestCase):
    def test_zero_branch_and_field_branch_merge_is_safe(self):
        """可接受：清零分支与完整字段写分支汇合，复制整个缓冲区。"""
        report = analyze(diamond_spec(
            FULL_FIELDS, WRITE_ALL, [{"op": "copy", "offset": 0, "length": 32}]))
        self.assertEqual(report.verdict, "ACCEPTABLE")
        self.assertEqual(len(report.findings), 1)
        finding = report.findings[0]
        self.assertEqual(finding.status, "SAFE")
        self.assertEqual(finding.safe_intervals, [(0, 32)])
        self.assertEqual(finding.unsafe_gaps, [])

    def test_adjacent_writes_merge_into_continuous_coverage(self):
        """边界交错：相邻的两次原始写入拼接成连续区间，复制恰好到边界。"""
        ops = [
            {"op": "write_field", "field": "hdr"},
            {"op": "write_field", "field": "body"},
            {"op": "write", "offset": 24, "length": 4},
            {"op": "write", "offset": 28, "length": 4},
        ]
        report = analyze(diamond_spec(
            FULL_FIELDS, ops, [{"op": "copy", "offset": 0, "length": 32}]))
        self.assertEqual(report.verdict, "ACCEPTABLE")
        self.assertEqual(report.findings[0].status, "SAFE")


class RejectedTests(unittest.TestCase):
    def test_alignment_gap_rejected_and_path_reported(self):
        """应被拒绝：对齐空洞 [8,12) 只在清零分支被覆盖。"""
        fields = [
            {"name": "hdr", "offset": 0, "size": 8},
            {"name": "body", "offset": 12, "size": 12},  # [8,12) 为对齐空洞
            {"name": "tail", "offset": 24, "size": 8},
        ]
        report = analyze(diamond_spec(
            fields, WRITE_ALL, [{"op": "copy", "offset": 0, "length": 32}]))
        self.assertEqual(report.verdict, "REJECTED")
        finding = report.findings[0]
        self.assertEqual(finding.status, "UNSAFE")
        self.assertEqual(len(finding.unsafe_gaps), 1)
        gap = finding.unsafe_gaps[0]
        self.assertEqual((gap.start, gap.end), (8, 12))
        # 只有“只写字段”的路径未覆盖该空洞，清零路径不应出现
        self.assertEqual(gap.uninit_paths,
                         [["entry", "write_fields", "merge"]])
        # 安全区间应分列空洞两侧
        self.assertEqual(finding.safe_intervals, [(0, 8), (12, 32)])

    def test_write_on_single_branch_not_trusted(self):
        """应被拒绝：字段只在汇合前的一条分支写入。"""
        spec = diamond_spec(
            FULL_FIELDS, WRITE_ALL, [{"op": "copy", "offset": 0, "length": 32}])
        # 把清零分支改为只写 hdr，body/tail 在该路径上未初始化
        spec["nodes"][1] = {
            "id": "zero_all",
            "ops": [{"op": "write_field", "field": "hdr"}],
            "succ": ["merge"],
        }
        report = analyze(spec)
        self.assertEqual(report.verdict, "REJECTED")
        gaps = report.findings[0].unsafe_gaps
        self.assertEqual([(g.start, g.end) for g in gaps], [(8, 32)])
        self.assertEqual(gaps[0].uninit_paths, [["entry", "zero_all", "merge"]])


class BoundaryInterleavedTests(unittest.TestCase):
    def test_copy_end_exactly_on_boundary_vs_one_byte_past(self):
        """边界交错：同一汇合点两次复制，终点在边界上安全、越出一字节拒绝。"""
        # 字段分支只写 hdr[0,8) 与 body[8,24)，[24,32) 仅清零分支覆盖
        ops = [{"op": "write_field", "field": "hdr"},
               {"op": "write_field", "field": "body"}]
        copies = [
            {"op": "copy", "offset": 0, "length": 24},  # 恰好到字段边界
            {"op": "copy", "offset": 0, "length": 25},  # 越出一字节
        ]
        report = analyze(diamond_spec(FULL_FIELDS, ops, copies))
        self.assertEqual(report.verdict, "REJECTED")
        exact, past = report.findings
        self.assertEqual(exact.status, "SAFE")
        self.assertEqual(exact.safe_intervals, [(0, 24)])
        self.assertEqual(past.status, "UNSAFE")
        self.assertEqual([(g.start, g.end) for g in past.unsafe_gaps], [(24, 25)])
        self.assertEqual(past.unsafe_gaps[0].uninit_paths,
                         [["entry", "write_fields", "merge"]])

    def test_nested_branches_only_some_subpaths_initialize(self):
        """交错路径：嵌套分支中只有部分子路径初始化 body。"""
        spec = {
            "buffer": {"size": 16, "fields": [
                {"name": "hdr", "offset": 0, "size": 8},
                {"name": "body", "offset": 8, "size": 8},
            ]},
            "entry": "entry",
            "nodes": [
                {"id": "entry", "ops": [], "succ": ["zero_all", "branch"]},
                {"id": "zero_all", "ops": [{"op": "zero_buffer"}], "succ": ["merge"]},
                {"id": "branch", "ops": [], "succ": ["b1", "b2"]},
                {"id": "b1", "ops": [{"op": "write_field", "field": "hdr"}],
                 "succ": ["merge"]},
                {"id": "b2", "ops": [{"op": "write_field", "field": "hdr"},
                                     {"op": "write_field", "field": "body"}],
                 "succ": ["merge"]},
                {"id": "merge", "ops": [{"op": "copy", "offset": 0, "length": 16}],
                 "succ": []},
            ],
        }
        report = analyze(spec)
        self.assertEqual(report.verdict, "REJECTED")
        finding = report.findings[0]
        self.assertEqual(finding.safe_intervals, [(0, 8)])
        self.assertEqual(len(finding.unsafe_gaps), 1)
        gap = finding.unsafe_gaps[0]
        self.assertEqual((gap.start, gap.end), (8, 16))
        # 恰好只有经过 b1 的路径未初始化 body
        self.assertEqual(gap.uninit_paths, [["entry", "branch", "b1", "merge"]])


class DynamicLengthTests(unittest.TestCase):
    def test_dynamic_length_without_layout_is_pending(self):
        """动态长度缺少布局：留待核实。"""
        spec = {
            "buffer": {"size": None, "fields": []},
            "entry": "entry",
            "nodes": [
                {"id": "entry", "ops": [{"op": "zero_buffer"}], "succ": ["out"]},
                {"id": "out",
                 "ops": [{"op": "copy", "offset": 0, "length": "dynamic"}],
                 "succ": []},
            ],
        }
        report = analyze(spec)
        self.assertEqual(report.verdict, "PENDING")
        finding = report.findings[0]
        self.assertEqual(finding.status, "PENDING")
        self.assertIn("留待核实", finding.reason)

    def test_dynamic_length_with_layout_resolves_to_buffer_size(self):
        """动态长度但有静态布局：按缓冲区长度求值，全路径清零则安全。"""
        spec = {
            "buffer": {"size": 16, "fields": []},
            "entry": "entry",
            "nodes": [
                {"id": "entry", "ops": [{"op": "zero_buffer"}], "succ": ["out"]},
                {"id": "out",
                 "ops": [{"op": "copy", "offset": 0, "length": "dynamic"}],
                 "succ": []},
            ],
        }
        report = analyze(spec)
        self.assertEqual(report.verdict, "ACCEPTABLE")
        self.assertEqual(report.findings[0].safe_intervals, [(0, 16)])

    def test_zero_buffer_with_dynamic_size_covers_static_copy(self):
        """缓冲区长度动态时清零整个缓冲区，静态复制区间被覆盖。"""
        spec = {
            "buffer": {"size": None, "fields": []},
            "entry": "entry",
            "nodes": [
                {"id": "entry", "ops": [{"op": "zero_buffer"}], "succ": ["out"]},
                {"id": "out", "ops": [{"op": "copy", "offset": 0, "length": 16}],
                 "succ": []},
            ],
        }
        report = analyze(spec)
        self.assertEqual(report.verdict, "ACCEPTABLE")
        self.assertEqual(report.findings[0].status, "SAFE")


class CliTests(unittest.TestCase):
    def _run_cli(self, spec_dict):
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8") as fh:
            json.dump(spec_dict, fh)
            path = fh.name
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli_main([path])
            return code, json.loads(buf.getvalue())
        finally:
            os.unlink(path)

    def test_cli_acceptable_exit_0(self):
        spec = diamond_spec(
            FULL_FIELDS, WRITE_ALL, [{"op": "copy", "offset": 0, "length": 32}])
        code, out = self._run_cli(spec)
        self.assertEqual(code, 0)
        self.assertEqual(out["verdict"], "ACCEPTABLE")
        self.assertEqual(out["findings"][0]["status"], "SAFE")

    def test_cli_rejected_exit_1_reports_gap_and_path(self):
        fields = [
            {"name": "hdr", "offset": 0, "size": 8},
            {"name": "body", "offset": 12, "size": 12},
            {"name": "tail", "offset": 24, "size": 8},
        ]
        spec = diamond_spec(
            fields, WRITE_ALL, [{"op": "copy", "offset": 0, "length": 32}])
        code, out = self._run_cli(spec)
        self.assertEqual(code, 1)
        self.assertEqual(out["verdict"], "REJECTED")
        gap = out["findings"][0]["unsafe_gaps"][0]
        self.assertEqual((gap["start"], gap["end"]), (8, 12))
        self.assertEqual(gap["uninitialized_paths"],
                         [["entry", "write_fields", "merge"]])

    def test_cli_pending_exit_2(self):
        spec = {
            "buffer": {"size": None, "fields": []},
            "entry": "entry",
            "nodes": [
                {"id": "entry",
                 "ops": [{"op": "copy", "offset": 0, "length": "dynamic"}],
                 "succ": []},
            ],
        }
        code, out = self._run_cli(spec)
        self.assertEqual(code, 2)
        self.assertEqual(out["verdict"], "PENDING")


class ValidationTests(unittest.TestCase):
    def test_unknown_field_rejected_at_load(self):
        with self.assertRaises(ValueError):
            load_spec({
                "buffer": {"size": 8, "fields": []},
                "nodes": [{"id": "entry",
                           "ops": [{"op": "write_field", "field": "nope"}],
                           "succ": []}],
            })

    def test_bad_successor_rejected_at_load(self):
        with self.assertRaises(ValueError):
            load_spec({
                "buffer": {"size": 8, "fields": []},
                "nodes": [{"id": "entry", "ops": [], "succ": ["ghost"]}],
            })


if __name__ == "__main__":
    unittest.main()
