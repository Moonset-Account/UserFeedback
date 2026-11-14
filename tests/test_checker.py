import json
import os
import unittest

from checker import CheckError, analyze, analyze_exec, collect_atoms, count_params

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RULES = {
    "external_inputs": ["user_id", "sort_key"],
    "allowed_identifiers": ["asc", "desc", "name", "created_at"],
}


def lit(v):
    return {"type": "literal", "value": v}


def var(n):
    return {"type": "var", "name": n}


def param(i):
    return {"type": "param", "index": i}


def concat(*parts):
    return {"type": "concat", "parts": list(parts)}


def exec_node(eid, structure, bindings, location=None):
    return {"type": "exec", "id": eid, "location": location,
            "structure": structure, "bindings": bindings}


class VarAsBindingPassesTest(unittest.TestCase):
    """Same variable: as a bound value it must pass."""

    def test_var_in_bindings_passes(self):
        tree = exec_node("q1", concat(lit("SELECT * FROM users WHERE id = "),
                                      param(0)),
                         [var("user_id")], location="demo.py:10")
        report = analyze(tree, RULES)
        self.assertEqual(report["summary"]["risk_count"], 0)
        self.assertEqual(report["summary"]["mismatch_count"], 0)
        self.assertEqual(report["executions"][0]["status"], "pass")
        self.assertEqual(report["executions"][0]["findings"], [])


class VarAsStructureFlaggedTest(unittest.TestCase):
    """Same variable: concatenated into the query structure it is flagged."""

    def test_var_in_structure_flagged_with_source(self):
        tree = exec_node("q2", concat(lit("SELECT * FROM users WHERE id = "),
                                      var("user_id")),
                         [], location="demo.py:20")
        report = analyze(tree, RULES)
        ex = report["executions"][0]
        self.assertEqual(ex["status"], "fail")
        self.assertEqual(len(ex["findings"]), 1)
        f = ex["findings"][0]
        self.assertEqual(f["type"], "tainted_structure")
        self.assertEqual(f["source"], "user_id")
        self.assertTrue(f["external_input"])
        self.assertEqual(ex["location"], "demo.py:20")

    def test_non_external_var_still_flagged(self):
        tree = exec_node("q3", concat(lit("ORDER BY "), var("internal_var")), [])
        report = analyze(tree, RULES)
        f = report["executions"][0]["findings"][0]
        self.assertEqual(f["type"], "tainted_structure")
        self.assertEqual(f["source"], "internal_var")
        self.assertFalse(f["external_input"])


class CondBranchTest(unittest.TestCase):
    """Any feasible risky branch must be reported with its branch path."""

    def test_risky_branch_reported_safe_branches_not(self):
        structure = concat(
            lit("SELECT * FROM users ORDER BY name "),
            {"type": "cond", "branches": [
                {"label": "ascending", "value": lit("asc")},
                {"label": "custom", "value": var("sort_key")},
            ], "else": lit("desc")},
        )
        tree = exec_node("q4", structure, [])
        report = analyze(tree, RULES)
        findings = report["executions"][0]["findings"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["branch"], ["custom"])
        self.assertEqual(findings[0]["source"], "sort_key")
        self.assertTrue(findings[0]["external_input"])

    def test_all_literal_branches_in_allowed_set_pass(self):
        structure = concat(
            lit("ORDER BY "),
            {"type": "cond", "branches": [
                {"label": "a", "value": lit("asc")},
                {"label": "b", "value": lit("desc")},
            ]},
        )
        tree = exec_node("q5", structure, [])
        report = analyze(tree, RULES)
        self.assertEqual(report["executions"][0]["status"], "pass")

    def test_branch_literal_not_in_allowed_set_flagged(self):
        structure = concat(
            lit("ORDER BY "),
            {"type": "cond", "branches": [
                {"label": "ok", "value": lit("name")},
                {"label": "evil", "value": lit("name; DROP TABLE users--")},
            ]},
        )
        tree = exec_node("q6", structure, [])
        report = analyze(tree, RULES)
        findings = report["executions"][0]["findings"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "identifier_not_allowed")
        self.assertEqual(findings[0]["branch"], ["evil"])
        self.assertEqual(findings[0]["value"], "name; DROP TABLE users--")

    def test_nested_branch_path_reported(self):
        inner = {"type": "cond", "branches": [
            {"label": "inner", "value": var("sort_key")},
        ]}
        outer = {"type": "cond", "branches": [
            {"label": "outer", "value": inner},
        ], "else": lit("asc")}
        tree = exec_node("q7", concat(lit("ORDER BY "), outer), [])
        report = analyze(tree, RULES)
        findings = report["executions"][0]["findings"]
        self.assertEqual(findings[0]["branch"], ["outer", "inner"])


class BindingCountTest(unittest.TestCase):
    """Binding/param-slot count mismatches are listed separately."""

    def test_mismatch_listed_separately(self):
        tree = exec_node("q8",
                         concat(lit("WHERE id = "), param(0),
                                lit(" AND org = "), param(1)),
                         [var("user_id")], location="demo.py:40")
        report = analyze(tree, RULES)
        self.assertEqual(report["summary"]["mismatch_count"], 1)
        self.assertEqual(report["summary"]["risk_count"], 0)
        mm = report["binding_mismatches"][0]
        self.assertEqual(mm["id"], "q8")
        self.assertEqual(mm["param_slots"], 2)
        self.assertEqual(mm["bindings"], 1)
        chk = report["executions"][0]["binding_check"]
        self.assertFalse(chk["ok"])

    def test_matching_counts_ok(self):
        tree = exec_node("q9", concat(lit("WHERE id = "), param(0)),
                         [var("user_id")])
        report = analyze(tree, RULES)
        self.assertTrue(report["executions"][0]["binding_check"]["ok"])
        self.assertEqual(report["binding_mismatches"], [])


class MultipleExecTest(unittest.TestCase):
    """Each execution node is tracked separately with its location."""

    def test_multiple_execs_tracked_independently(self):
        tree = {"executions": [
            exec_node("safe", concat(lit("SELECT 1 WHERE id = "), param(0)),
                      [var("user_id")], location="a.py:1"),
            exec_node("unsafe", concat(lit("SELECT 1 WHERE id = "),
                                       var("user_id")),
                      [], location="b.py:2"),
        ]}
        report = analyze(tree, RULES)
        self.assertEqual(report["summary"]["exec_count"], 2)
        by_id = {e["id"]: e for e in report["executions"]}
        self.assertEqual(by_id["safe"]["status"], "pass")
        self.assertEqual(by_id["unsafe"]["status"], "fail")
        self.assertEqual(by_id["unsafe"]["location"], "b.py:2")


class MalformedInputTest(unittest.TestCase):
    def test_unknown_node_type_rejected(self):
        with self.assertRaises(CheckError):
            analyze(exec_node("bad", {"type": "lambda"}, []), RULES)

    def test_exec_without_structure_rejected(self):
        with self.assertRaises(CheckError):
            analyze({"type": "exec", "id": "bad", "bindings": []}, RULES)


class DemoFixtureTest(unittest.TestCase):
    """The shipped demo: same variable passes as binding, flagged as structure."""

    def test_demo_fixture(self):
        with open(os.path.join(ROOT, "examples", "rules.json"),
                  encoding="utf-8") as f:
            rules = json.load(f)
        with open(os.path.join(ROOT, "examples", "demo.json"),
                  encoding="utf-8") as f:
            tree = json.load(f)
        report = analyze(tree, rules)
        by_id = {e["id"]: e for e in report["executions"]}
        self.assertEqual(by_id["q_bind"]["status"], "pass")
        self.assertEqual(by_id["q_struct"]["status"], "fail")
        f0 = by_id["q_struct"]["findings"][0]
        self.assertEqual(f0["source"], "user_id")
        self.assertTrue(f0["external_input"])


if __name__ == "__main__":
    unittest.main()
