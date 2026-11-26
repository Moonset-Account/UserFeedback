import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tenant_guard import analyze_file, analyze_function, analyze_program, current_tenant, lit, tup, var
from tenant_guard.__main__ import main as cli_main


class BoundaryAndInputTests(unittest.TestCase):
    def test_member_tenant_source_is_recognized(self):
        program = {
            "caches": {"shared": {}},
            "functions": {
                "main": {
                    "body": [
                        {
                            "id": "r1",
                            "kind": "cache_read",
                            "cache": "shared",
                            "key": {"kind": "tuple", "elements": [
                                {"kind": "member", "object": "ctx", "property": "tenant_id"},
                                {"kind": "var", "name": "id"},
                            ]},
                        }
                    ]
                }
            },
        }
        # id is undefined, but the independent tenant component is sufficient.
        report = analyze_program(program)
        self.assertEqual(report.status, "pass", report.to_json())
        self.assertEqual(report.sink("r1")["basis"][0]["tenant_path"], [0])

    def test_alias_assigned_on_only_one_branch_locates_merge_node(self):
        program = {
            "caches": {"shared": {}},
            "functions": {
                "main": {
                    "body": [
                        {
                            "id": "if-merge",
                            "kind": "if",
                            "then": [
                                {"id": "only-safe", "kind": "assign", "target": "k", "value": current_tenant()}
                            ],
                        },
                        {"id": "r1", "kind": "cache_read", "cache": "shared", "key": var("k")},
                    ]
                }
            },
        }
        report = analyze_program(program)
        sink = report.sink("r1")
        self.assertEqual(report.status, "fail")
        self.assertEqual(sink["earliest_loss"]["node_id"], "if-merge")
        self.assertEqual(sink["earliest_loss"]["code"], "alias_unassigned_on_path")

    def test_user_defined_normalizer_can_strip_tenant_at_index(self):
        program = {
            "caches": {"shared": {}},
            "functions": {
                "normalize": {
                    "params": ["key"],
                    "body": [
                        {
                            "id": "choice",
                            "kind": "if",
                            "then": [
                                {
                                    "id": "strip-component",
                                    "kind": "return",
                                    "value": {
                                        "kind": "index",
                                        "base": var("key"),
                                        "index": lit(1),
                                    },
                                }
                            ],
                            "else": [
                                {"id": "keep", "kind": "return", "value": var("key")}
                            ],
                        }
                    ],
                },
                "main": {
                    "body": [
                        {"id": "n1", "kind": "assign", "target": "raw", "value": tup(current_tenant(), lit("id"))},
                        {"id": "n2", "kind": "assign", "target": "k", "value": {"kind": "call", "callee": "normalize", "args": [var("raw")]}},
                        {"id": "r1", "kind": "cache_read", "cache": "shared", "key": var("k")},
                    ]
                },
            },
        }
        report = analyze_program(program)
        sink = report.sink("r1")
        self.assertEqual(sink["status"], "unsafe")
        self.assertEqual(sink["earliest_loss"]["node_id"], "strip-component")
        self.assertEqual(sink["earliest_loss"]["code"], "tenant_dimension_discarded")

    def test_external_function_without_summary_is_unknown_at_call(self):
        program = {
            "caches": {"shared": {}},
            "functions": {
                "main": {
                    "body": [
                        {"id": "ext-call", "kind": "assign", "target": "k", "value": {"kind": "call", "callee": "mystery", "args": []}},
                        {"id": "w1", "kind": "cache_write", "cache": "shared", "key": var("k")},
                    ]
                }
            },
        }
        report = analyze_program(program)
        self.assertEqual(report.sink("w1")["earliest_loss"]["node_id"], "ext-call")
        self.assertEqual(report.sink("w1")["earliest_loss"]["code"], "external_call_without_summary")

    def test_for_loop_equivalent_state_converges(self):
        program = {
            "caches": {"shared": {}},
            "functions": {
                "main": {
                    "body": [
                        {"id": "n1", "kind": "assign", "target": "k", "value": tup(current_tenant())},
                        {
                            "id": "for1",
                            "kind": "for",
                            "variable": "item",
                            "iterable": tup(lit("a"), lit("b")),
                            "body": [
                                {"id": "repeat", "kind": "assign", "target": "k", "value": var("k")},
                            ],
                        },
                        {"id": "r1", "kind": "cache_read", "cache": "shared", "key": var("k")},
                    ]
                }
            },
        }
        report = analyze_program(program)
        self.assertEqual(report.status, "pass", report.to_json())
        loop = [item for item in report.to_dict()["diagnostics"]["loops"] if item["node_id"] == "for1"][0]
        self.assertTrue(loop["converged"])
        self.assertLessEqual(loop["iterations"], 3)

    def test_static_false_branch_is_not_a_reaching_path(self):
        program = {
            "caches": {"shared": {}},
            "functions": {
                "main": {
                    "body": [
                        {
                            "id": "if-static",
                            "kind": "if",
                            "condition": lit(False),
                            "then": [
                                {"id": "impossible-bad", "kind": "cache_write", "cache": "shared", "key": lit("global")}
                            ],
                            "else": [
                                {"id": "safe", "kind": "assign", "target": "k", "value": tup(current_tenant())}
                            ],
                        },
                        {"id": "r1", "kind": "cache_read", "cache": "shared", "key": var("k")},
                    ]
                }
            },
        }
        report = analyze_program(program)
        self.assertEqual(report.status, "pass", report.to_json())
        self.assertEqual(report.sink("impossible-bad")["status"], "unreachable")
        self.assertEqual(report.sink("r1")["status"], "safe")

    def test_scalar_exclusive_cache_annotation(self):
        program = {
            "caches": {"private": "current"},
            "functions": {
                "main": {
                    "body": [
                        {"id": "r1", "kind": "cache_read", "cache": "private", "key": lit("id")},
                    ]
                }
            },
        }
        self.assertTrue(analyze_program(program).ok)

    def test_break_path_is_joined_after_loop(self):
        program = {
            "caches": {"shared": {}},
            "functions": {
                "main": {
                    "body": [
                        {"id": "n1", "kind": "assign", "target": "k", "value": tup(current_tenant())},
                        {
                            "id": "loop1",
                            "kind": "while",
                            "condition": lit(True),
                            "body": [
                                {
                                    "id": "if-break",
                                    "kind": "if",
                                    "then": [
                                        {"id": "bad-break-key", "kind": "assign", "target": "k", "value": lit("global")},
                                        {"id": "br", "kind": "break"},
                                    ],
                                    "else": [
                                        {"id": "repeat-safe", "kind": "assign", "target": "k", "value": var("k")}
                                    ],
                                }
                            ],
                        },
                        {"id": "r1", "kind": "cache_read", "cache": "shared", "key": var("k")},
                    ]
                }
            },
        }
        report = analyze_program(program)
        sink = report.sink("r1")
        self.assertEqual(report.status, "fail")
        self.assertEqual(sink["earliest_loss"]["node_id"], "bad-break-key")

    def test_analyze_function_argument_context(self):
        program = {
            "caches": {"shared": {}},
            "functions": {
                "store": {
                    "params": ["key"],
                    "body": [
                        {"id": "w1", "kind": "cache_write", "cache": "shared", "key": var("key")},
                    ],
                }
            },
        }
        safe = analyze_function(program, "store", [tup(current_tenant())])
        unsafe = analyze_function(program, "store", [lit("global")])
        self.assertTrue(safe.ok)
        self.assertFalse(unsafe.ok)
        self.assertEqual(unsafe.sink("w1")["first_loss_node_id"], "param:key")

    def test_repository_example_file(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "tenant_review.json"
        report = analyze_file(example)
        self.assertEqual(report.summary["total"], 3)
        self.assertEqual(report.summary["safe"], 2)
        self.assertEqual(report.summary["unsafe"], 1)
        self.assertEqual(report.to_dict()["first_loss_node_id"], "digested")

    def test_json_file_and_cli(self):
        program = {
            "caches": {"shared": {}},
            "functions": {
                "main": {
                    "body": [
                        {"id": "bad", "kind": "assign", "target": "k", "value": lit("x")},
                        {"id": "w1", "kind": "cache_write", "cache": "shared", "key": var("k")},
                    ]
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.json"
            path.write_text(json.dumps(program), encoding="utf-8")
            report = analyze_file(path)
            self.assertFalse(report.ok)
            json.loads(report.to_json())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main([str(path)])
            self.assertEqual(exit_code, 2)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "fail")
            self.assertEqual(payload["sinks"][0]["node_id"], "w1")


if __name__ == "__main__":
    unittest.main()
