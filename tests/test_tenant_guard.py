import unittest

from tenant_guard import (
    analyze_program,
    call,
    current_tenant,
    lit,
    tup,
    unknown,
    var,
)


def base_program(**overrides):
    program = {
        "caches": {"shared": {}},
        "functions": {},
    }
    program.update(overrides)
    return program


class TenantGuardTests(unittest.TestCase):
    def test_safe_tuple_with_current_tenant_dimension(self):
        program = base_program(
            functions={
                "main": {
                    "body": [
                        {"id": "n1", "kind": "assign", "target": "k", "value": tup(current_tenant(), lit("order-42"))},
                        {"id": "r1", "kind": "cache_read", "cache": "shared", "key": var("k")},
                    ]
                }
            }
        )
        report = analyze_program(program)
        sink = report.sink("r1")
        self.assertEqual(report.status, "pass")
        self.assertEqual(sink["status"], "safe")
        self.assertEqual(sink["earliest_loss"], None)
        self.assertEqual(sink["basis"][0]["tenant_path"], [0])
        node_ids = [node["node_id"] for node in sink["basis"][0]["chain"]]
        self.assertIn("n1", node_ids)
        self.assertIn("r1", node_ids)

    def test_unsafe_plain_global_key_locates_introduction_node(self):
        program = base_program(
            functions={
                "main": {
                    "body": [
                        {"id": "make-key", "kind": "assign", "target": "k", "value": lit("global-counter")},
                        {"id": "w1", "kind": "cache_write", "cache": "shared", "key": var("k")},
                    ]
                }
            }
        )
        report = analyze_program(program)
        sink = report.sink("w1")
        self.assertEqual(report.status, "fail")
        self.assertEqual(sink["status"], "unsafe")
        self.assertEqual(sink["earliest_loss"]["node_id"], "make-key")
        self.assertEqual(sink["earliest_loss"]["code"], "missing_tenant_dimension")

    def test_normalizer_erasure_is_earliest_loss_and_no_valid_witness_remains(self):
        program = base_program(
            normalizers={"digest": {"mode": "erase"}},
            functions={
                "main": {
                    "body": [
                        {"id": "n1", "kind": "assign", "target": "raw", "value": tup(current_tenant(), lit("id"))},
                        {"id": "erase-1", "kind": "assign", "target": "k", "value": call("digest", var("raw"))},
                        {"id": "r1", "kind": "cache_read", "cache": "shared", "key": var("k")},
                    ]
                }
            },
        )
        report = analyze_program(program)
        self.assertEqual(report.status, "fail")
        self.assertEqual(report.to_dict()["first_loss_node_id"], "erase-1")
        sink = report.sink("r1")
        self.assertEqual(sink["status"], "unsafe")
        self.assertEqual(sink["earliest_loss"]["node_id"], "erase-1")
        self.assertEqual(sink["earliest_loss"]["code"], "normalizer_erased_tenant")
        self.assertTrue(sink["basis"])
        for item in sink["basis"]:
            self.assertNotEqual(item["evidence_status"], "current_tenant")
            self.assertIsNone(item["tenant_path"])

    def test_merging_function_reports_merge_specific_loss(self):
        program = base_program(
            normalizers={"merge_aliases": {"mode": "merge"}},
            functions={
                "main": {
                    "body": [
                        {"id": "m1", "kind": "assign", "target": "k", "value": call("merge_aliases", current_tenant())},
                        {"id": "r1", "kind": "cache_read", "cache": "shared", "key": var("k")},
                    ]
                }
            },
        )
        sink = analyze_program(program).sink("r1")
        self.assertEqual(sink["status"], "unsafe")
        self.assertEqual(sink["earliest_loss"]["node_id"], "m1")
        self.assertEqual(sink["earliest_loss"]["code"], "normalizer_merged_distinctions")

    def test_preserving_normalizer_keeps_proof(self):
        program = base_program(
            normalizers={"canon": {"mode": "preserve"}},
            functions={
                "main": {
                    "body": [
                        {"id": "n1", "kind": "assign", "target": "raw", "value": tup(current_tenant(), lit("id"))},
                        {"id": "n2", "kind": "assign", "target": "k", "value": call("canon", var("raw"))},
                        {"id": "r1", "kind": "cache_read", "cache": "shared", "key": var("k")},
                    ]
                }
            },
        )
        report = analyze_program(program)
        self.assertEqual(report.status, "pass", report.to_json())
        self.assertEqual(report.sink("r1")["status"], "safe")

    def test_branch_aliases_can_each_supply_tenant_source(self):
        program = base_program(
            functions={
                "main": {
                    "body": [
                        {
                            "id": "if1",
                            "kind": "if",
                            "condition": var("choice"),
                            "then": [
                                {"id": "ta", "kind": "assign", "target": "a", "value": current_tenant()},
                                {"id": "tk", "kind": "assign", "target": "k", "value": tup(var("a"), lit("x"))},
                            ],
                            "else": [
                                {"id": "eb", "kind": "assign", "target": "b", "value": current_tenant()},
                                {"id": "ek", "kind": "assign", "target": "k", "value": tup(var("b"), lit("x"))},
                            ],
                        },
                        {"id": "r1", "kind": "cache_read", "cache": "shared", "key": var("k")},
                    ]
                }
            }
        )
        report = analyze_program(program)
        sink = report.sink("r1")
        self.assertEqual(report.status, "pass", report.to_json())
        chains = [" ".join(node["node_id"] for node in item["chain"]) for item in sink["basis"]]
        self.assertTrue(any("ta" in chain for chain in chains), chains)
        self.assertTrue(any("eb" in chain for chain in chains), chains)

    def test_one_unsafe_branch_cannot_be_covered_by_safe_branch(self):
        program = base_program(
            functions={
                "main": {
                    "body": [
                        {
                            "id": "if1",
                            "kind": "if",
                            "then": [
                                {"id": "safe-assign", "kind": "assign", "target": "k", "value": tup(current_tenant())}
                            ],
                            "else": [
                                {"id": "bad-assign", "kind": "assign", "target": "k", "value": lit("global")}
                            ],
                        },
                        {"id": "w1", "kind": "cache_write", "cache": "shared", "key": var("k")},
                    ]
                }
            }
        )
        report = analyze_program(program)
        sink = report.sink("w1")
        self.assertEqual(report.status, "fail")
        self.assertEqual(sink["earliest_loss"]["node_id"], "bad-assign")
        statuses = {item["evidence_status"] for item in sink["basis"]}
        self.assertIn("current_tenant", statuses)
        self.assertIn("not_tenant", statuses)

    def test_terminating_unsafe_branch_does_not_reach_later_sink(self):
        program = base_program(
            functions={
                "main": {
                    "body": [
                        {
                            "id": "if1",
                            "kind": "if",
                            "then": [{"id": "stop", "kind": "raise"}],
                            "else": [
                                {"id": "safe", "kind": "assign", "target": "k", "value": tup(current_tenant())}
                            ],
                        },
                        {"id": "r1", "kind": "cache_read", "cache": "shared", "key": var("k")},
                    ]
                }
            }
        )
        report = analyze_program(program)
        self.assertEqual(report.status, "pass", report.to_json())

    def test_explicit_current_tenant_exclusive_cache_allows_missing_dimension(self):
        program = base_program(
            caches={"private": {"exclusive_to_current_tenant": True}},
            functions={
                "main": {
                    "body": [
                        {"id": "n1", "kind": "assign", "target": "k", "value": lit("only-id")},
                        {"id": "r1", "kind": "cache_read", "cache": "private", "key": var("k")},
                    ]
                }
            },
        )
        report = analyze_program(program)
        sink = report.sink("r1")
        self.assertEqual(report.status, "pass")
        self.assertEqual(sink["basis"][0]["evidence_status"], "cache_exclusive")

    def test_named_exclusive_cache_requires_matching_current_tenant(self):
        matching = base_program(
            current_tenant_id="acme",
            caches={"acme-only": {"exclusive_tenant": "acme"}},
            functions={
                "main": {
                    "body": [
                        {"id": "n1", "kind": "assign", "target": "k", "value": lit("id")},
                        {"id": "r1", "kind": "cache_read", "cache": "acme-only", "key": var("k")},
                    ]
                }
            },
        )
        self.assertEqual(analyze_program(matching).status, "pass")

        mismatched = base_program(
            current_tenant_id="beta",
            caches={"acme-only": {"exclusive_tenant": "acme"}},
            functions=matching["functions"],
        )
        report = analyze_program(mismatched)
        self.assertEqual(report.status, "fail")
        self.assertEqual(report.sink("r1")["reason_code"] if "reason_code" in report.sink("r1") else report.sink("r1")["context_results"][0]["reason_code"], "missing_tenant_and_cache_not_exclusive_for_current_tenant")

    def test_interprocedural_forwarding_has_cross_function_basis(self):
        program = base_program(
            functions={
                "store": {
                    "params": ["key"],
                    "body": [
                        {"id": "w1", "kind": "cache_write", "cache": "shared", "key": var("key")},
                    ],
                },
                "main": {
                    "body": [
                        {"id": "n1", "kind": "assign", "target": "k", "value": tup(current_tenant(), lit("id"))},
                        {"id": "c1", "kind": "call", "callee": "store", "args": [var("k")]},
                    ]
                },
            }
        )
        report = analyze_program(program)
        sink = report.sink("w1")
        self.assertEqual(report.status, "pass", report.to_json())
        chain_functions = {node["function"] for item in sink["basis"] for node in item["chain"]}
        chain_ids = {node["node_id"] for item in sink["basis"] for node in item["chain"]}
        self.assertIn("main", chain_functions)
        self.assertIn("store", chain_functions)
        self.assertIn("n1", chain_ids)
        self.assertIn("c1", chain_ids)
        self.assertIn("param:key", chain_ids)

    def test_loop_equivalent_state_converges_and_remains_safe(self):
        program = base_program(
            functions={
                "main": {
                    "body": [
                        {"id": "n1", "kind": "assign", "target": "k", "value": tup(current_tenant())},
                        {
                            "id": "loop1",
                            "kind": "while",
                            "condition": lit(True),
                            "body": [
                                {"id": "alias-step", "kind": "assign", "target": "k", "value": var("k")},
                            ],
                        },
                        {"id": "r1", "kind": "cache_read", "cache": "shared", "key": var("k")},
                    ]
                }
            }
        )
        report = analyze_program(program)
        self.assertEqual(report.status, "pass", report.to_json())
        loop = report.to_dict()["diagnostics"]["loops"][0]
        self.assertTrue(loop["converged"])
        self.assertLessEqual(loop["iterations"], 3)

    def test_recursive_forwarding_of_tenant_key_converges_safe(self):
        program = base_program(
            functions={
                "forward": {
                    "params": ["k"],
                    "body": [
                        {
                            "id": "if-rec",
                            "kind": "if",
                            "then": [
                                {"id": "base-ret", "kind": "return", "value": var("k")},
                            ],
                            "else": [
                                {
                                    "id": "rec-ret",
                                    "kind": "return",
                                    "value": call("forward", var("k")),
                                }
                            ],
                        }
                    ],
                },
                "main": {
                    "body": [
                        {"id": "n1", "kind": "assign", "target": "tenant_key", "value": tup(current_tenant())},
                        {
                            "id": "n2",
                            "kind": "assign",
                            "target": "k2",
                            "value": call("forward", var("tenant_key")),
                        },
                        {"id": "r1", "kind": "cache_read", "cache": "shared", "key": var("k2")},
                    ]
                },
            }
        )
        report = analyze_program(program)
        self.assertEqual(report.status, "pass", report.to_json())
        sink = report.sink("r1")
        functions = {node["function"] for item in sink["basis"] for node in item["chain"]}
        self.assertIn("forward", functions)
        self.assertLessEqual(report.summary["contexts_analyzed"], 4)

    def test_unknown_recursive_branch_is_not_covered_by_safe_path(self):
        program = base_program(
            functions={
                "rec": {
                    "params": ["k"],
                    "body": [
                        {
                            "id": "if-rec",
                            "kind": "if",
                            "then": [
                                {
                                    "id": "bad-recursive-return",
                                    "kind": "return",
                                    "value": call("rec", unknown("erased remote key")),
                                }
                            ],
                            "else": [
                                {"id": "safe-return", "kind": "return", "value": var("k")},
                            ],
                        }
                    ],
                },
                "main": {
                    "body": [
                        {"id": "n1", "kind": "assign", "target": "k", "value": current_tenant()},
                        {"id": "n2", "kind": "assign", "target": "k2", "value": call("rec", var("k"))},
                        {"id": "r1", "kind": "cache_read", "cache": "shared", "key": var("k2")},
                    ]
                },
            }
        )
        report = analyze_program(program)
        sink = report.sink("r1")
        self.assertEqual(report.status, "fail")
        self.assertEqual(sink["status"], "unsafe")
        self.assertEqual(sink["earliest_loss"]["node_id"], "bad-recursive-return")
        statuses = {item["evidence_status"] for item in sink["basis"]}
        self.assertIn("current_tenant", statuses)
        self.assertIn("unknown", statuses)

    def test_interleaved_safe_and_unsafe_reads_writes(self):
        program = base_program(
            caches={"shared": {}, "private": {"exclusive_to_current_tenant": True}},
            functions={
                "main": {
                    "body": [
                        {"id": "safe-key", "kind": "assign", "target": "a", "value": tup(current_tenant(), lit(1))},
                        {"id": "r-safe", "kind": "cache_read", "cache": "shared", "key": var("a")},
                        {"id": "bad-key", "kind": "assign", "target": "b", "value": lit("global")},
                        {"id": "w-bad", "kind": "cache_write", "cache": "shared", "key": var("b")},
                        {"id": "r-private", "kind": "cache_read", "cache": "private", "key": var("b")},
                        {"id": "w-safe", "kind": "cache_write", "cache": "shared", "key": var("a")},
                    ]
                }
            },
        )
        report = analyze_program(program)
        self.assertEqual(report.sink("r-safe")["status"], "safe")
        self.assertEqual(report.sink("w-bad")["status"], "unsafe")
        self.assertEqual(report.sink("r-private")["status"], "safe")
        self.assertEqual(report.sink("w-safe")["status"], "safe")
        self.assertEqual(report.status, "fail")
        self.assertEqual(report.summary["safe"], 3)
        self.assertEqual(report.summary["unsafe"], 1)


if __name__ == "__main__":
    unittest.main()
