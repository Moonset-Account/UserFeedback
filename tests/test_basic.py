"""正常场景：alloc/seq 默认值、求和、位置保留、图校验与文件加载。"""

import json
import sys
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from budget_guard import (  # noqa: E402
    analyze,
    load_evidence,
    load_operation_graph,
)
from budget_guard.errors import GraphValidationError  # noqa: E402
from budget_guard.loader import evidence_limit  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


class TestAllocAndSeq(unittest.TestCase):
    def test_single_alloc_explicit_costs(self):
        rep = analyze({"op": "alloc", "bytes": 2048, "records": 3})
        self.assertEqual(rep.total.bytes, 2048)
        self.assertEqual(rep.total.records, 3)
        self.assertTrue(rep.total.finite)
        self.assertEqual(rep.blocking_issues, [])

    def test_alloc_defaults(self):
        # 未给 bytes -> 0；未给 records -> 每次展开默认 1 条记录
        rep = analyze({"op": "alloc"})
        self.assertEqual(rep.total.bytes, 0)
        self.assertEqual(rep.total.records, 1)

    def test_seq_sums_both_dimensions(self):
        g = {
            "op": "seq",
            "steps": [
                {"op": "alloc", "bytes": 100, "records": 2},
                {"op": "alloc", "bytes": 250, "records": 5},
                {"op": "alloc", "bytes": 50, "records": 1},
            ],
        }
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 400)
        self.assertEqual(rep.total.records, 8)

    def test_empty_seq_is_zero(self):
        rep = analyze({"op": "seq", "steps": []})
        self.assertEqual(rep.total.bytes, 0)
        self.assertEqual(rep.total.records, 0)

    def test_nested_seq(self):
        g = {
            "root": {
                "op": "seq",
                "steps": [
                    {"op": "alloc", "bytes": 10, "records": 1},
                    {"op": "seq", "steps": [
                        {"op": "alloc", "bytes": 20, "records": 1},
                        {"op": "alloc", "bytes": 30, "records": 1},
                    ]},
                ],
            }
        }
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 60)
        self.assertEqual(rep.total.records, 3)

    def test_top_level_array_is_implicit_seq(self):
        # “一组操作描述”可以直接以数组给出
        rep = analyze([
            {"op": "alloc", "bytes": 4, "records": 1},
            {"op": "alloc", "bytes": 6, "records": 2},
        ])
        self.assertEqual(rep.total.bytes, 10)
        self.assertEqual(rep.total.records, 3)

    def test_root_string_names_definition(self):
        g = {"root": "main", "definitions": {
            "main": {"op": "alloc", "bytes": 13, "records": 1}
        }}
        rep = analyze(g)
        self.assertEqual(rep.total.bytes, 13)

    def test_op_aliases(self):
        rep = analyze({
            "op": "sequence",
            "children": [
                {"op": "object", "size_bytes": 8, "records": 2},
                {"op": "allocate", "size": 2},
            ],
        })
        self.assertEqual(rep.total.bytes, 10)
        self.assertEqual(rep.total.records, 3)  # 2 + default 1

    def test_report_is_json_serializable(self):
        rep = analyze({"op": "seq", "id": "s", "steps": [
            {"op": "alloc", "bytes": 1, "records": 1}
        ]})
        text = json.dumps(rep.as_dict(), ensure_ascii=False)
        self.assertIn('"total"', text)
        again = json.loads(text)
        self.assertEqual(again["total"]["bytes"], 1)


class TestPositionsAndIds(unittest.TestCase):
    def test_explicit_pos_is_preserved(self):
        rep = analyze({
            "op": "segments",
            "id": "seg7",
            "pos": "archive.meta.json#/ops/7",
            "count": {"evidence": "missing_key"},
            "body": {"op": "alloc", "bytes": 1},
        })
        v = rep.rejectable_branches[0]
        self.assertEqual(v.pos, "archive.meta.json#/ops/7")
        self.assertEqual(v.branch_id, "seg7")

    def test_synthetic_path_when_no_pos_no_id(self):
        g = {"op": "seq", "steps": [
            {"op": "segments", "count": {"evidence": "k"},
             "body": {"op": "alloc", "bytes": 1}}
        ]}
        rep = analyze(g)
        iss = rep.blocking_issues[0]
        self.assertTrue(iss.pos)
        self.assertIn("steps[0]", iss.pos)
        self.assertEqual(iss.region_id, iss.pos)

    def test_location_alias(self):
        # 错误形态（bytes 字符串）应抛 GraphValidationError，并带位置
        with self.assertRaises(GraphValidationError) as ctx:
            analyze({"op": "alloc", "bytes": "bad", "location": "line-42"})
        self.assertIn("line-42", str(ctx.exception))


class TestValidation(unittest.TestCase):
    def test_top_level_not_object_or_array(self):
        with self.assertRaises(GraphValidationError):
            analyze(42)  # type: ignore[arg-type]

    def test_top_level_array_with_bad_node_raises(self):
        with self.assertRaises(GraphValidationError):
            analyze([{"not_op": True}])

    def test_top_level_without_op_or_root(self):
        with self.assertRaises(GraphValidationError):
            analyze({"definitions": {}})

    def test_node_not_object(self):
        with self.assertRaises(GraphValidationError):
            analyze({"op": "seq", "steps": ["not-a-node"]})

    def test_missing_op(self):
        with self.assertRaises(GraphValidationError):
            analyze({"bytes": 10})

    def test_unknown_op(self):
        with self.assertRaises(GraphValidationError):
            analyze({"op": "frobnicate"})

    def test_negative_literal_rejected(self):
        with self.assertRaises(GraphValidationError):
            analyze({"op": "alloc", "bytes": -3})

    def test_bool_as_count_rejected(self):
        with self.assertRaises(GraphValidationError):
            analyze({"op": "segments", "count": True,
                     "body": {"op": "alloc"}})

    def test_bad_evidence_spec_shape(self):
        with self.assertRaises(GraphValidationError):
            analyze({"op": "alloc", "bytes": {"evidence": 123}})

    def test_branch_requires_arms(self):
        with self.assertRaises(GraphValidationError):
            analyze({"op": "branch"})

    def test_segments_requires_count_and_body(self):
        with self.assertRaises(GraphValidationError):
            analyze({"op": "segments", "body": {"op": "alloc"}})
        with self.assertRaises(GraphValidationError):
            analyze({"op": "segments", "count": 3})

    def test_recurse_requires_depth(self):
        with self.assertRaises(GraphValidationError):
            analyze({"op": "recurse", "body": {"op": "alloc"}})

    def test_call_requires_string_target(self):
        with self.assertRaises(GraphValidationError):
            analyze({"op": "call"})

    def test_reject_must_be_string_ids(self):
        with self.assertRaises(TypeError):
            analyze({"op": "alloc"}, reject=[123])  # type: ignore[list-item]


class TestLoaderAndEvidence(unittest.TestCase):
    def test_load_graph_from_fixture_file(self):
        g = load_operation_graph(FIXTURES / "simple_graph.json")
        rep = analyze(g, load_evidence(FIXTURES / "evidence.json"))
        self.assertEqual(rep.total.bytes, 120)  # 见 fixture 说明

    def test_load_evidence_none_and_dict(self):
        self.assertEqual(load_evidence(None), {})
        self.assertEqual(load_evidence({"a": 1}), {"a": 1})

    def test_load_missing_file_raises(self):
        with self.assertRaises(GraphValidationError):
            load_operation_graph(FIXTURES / "does_not_exist.json")

    def test_load_bad_json_raises(self):
        with self.assertRaises(GraphValidationError):
            load_evidence(FIXTURES / "not_json.txt")

    def test_evidence_limit_forms(self):
        self.assertEqual(evidence_limit(16), (16, None))
        val, err = evidence_limit(-1)
        self.assertIsNone(val)
        self.assertIn("负", err)
        val, err = evidence_limit(True)
        self.assertIsNone(val)
        self.assertIn("布尔", err)
        self.assertEqual(evidence_limit({"limit": 9})[0], 9)
        self.assertEqual(evidence_limit({"max": 11})[0], 11)
        bad, err = evidence_limit({"source": "x"})
        self.assertIsNone(bad)
        self.assertTrue(err)
        bad, err = evidence_limit("lots")
        self.assertIsNone(bad)
        self.assertTrue(err)

    def test_analyze_accepts_paths_directly(self):
        rep = analyze(
            FIXTURES / "simple_graph.json",
            FIXTURES / "evidence.json",
        )
        # 1 (header) + 10 segment records
        self.assertEqual(rep.total.records, 11)


if __name__ == "__main__":
    unittest.main(verbosity=2)
