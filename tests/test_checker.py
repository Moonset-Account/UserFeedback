"""End-to-end and unit tests covering the required verification points.

Required properties under test:
  1. the same function keeps sanitization state independent per field;
  2. state transfers correctly across call/return edges;
  3. the analysis terminates with loops and mutual recursion;
  4. branch merge keeps both sanitized and open alternatives;
  5. deliverables split into newly_allowed / unchanged / undetermined
     (plus newly_blocked for removed rule tuples);
  6. shortest rule-change evidence is minimal;
  7. malformed documents raise LoaderError instead of crashing.
"""

import unittest

from sanitizer_check.analysis import Engine
from sanitizer_check.errors import AnalysisError, LoaderError
from sanitizer_check.loader import parse_graph, parse_rule_table
from sanitizer_check.model import PathState, diff_rule_tables
from sanitizer_check.report import build_report
from sanitizer_check.verdict import (
    ALLOWED,
    BLOCKED,
    NEWLY_ALLOWED,
    NEWLY_BLOCKED,
    UNCHANGED,
    UNDETERMINED,
    UNDETERMINED_BUCKET,
    UNREACHABLE,
    classify,
    evaluate_sink,
)

OLD = parse_rule_table({
    "version": "v1",
    "sanitizers": {
        "log_redactor": {"categories": ["log"]},
        "html_escaper": {"categories": ["html"]},
        "shell_quoter": {"categories": ["shell"]},
    },
})

NEW = parse_rule_table({
    "version": "v2",
    "sanitizers": {
        "log_redactor": {"categories": ["log", "html"]},
        "html_escaper": {"categories": ["html"]},
        "shell_quoter": {"categories": ["shell"]},
    },
})


def node(nid, kind, function="f", **kw):
    base = {"id": nid, "kind": kind, "function": function}
    base.update(kw)
    return base


def edge(src, dst):
    return {"src": src, "dst": dst}


def run(graph_doc, old=OLD, new=NEW, roots=()):
    graph = parse_graph(graph_doc)
    envs = Engine(graph).analyze(declared_roots=roots)
    return graph, classify(graph, old, new, envs)


def bucket_map(comparison):
    return {item["name"]: item for item in comparison["items"]}


class BranchMergeTests(unittest.TestCase):
    def test_keeps_both_sanitized_and_open_alternatives(self):
        doc = {
            "graph_id": "g",
            "functions": [{"id": "f", "entry": "e", "returns": ["r"]}],
            "nodes": [
                node("e", "entry"),
                node("s", "source", field="x", source_label="SRC"),
                node("b", "branch"),
                node("san", "stmt", field="x", sanitizers=["html_escaper"]),
                node("m", "merge"),
                node("sink", "sink", field="x", category="html"),
                node("r", "return"),
            ],
            "edges": [
                edge("e", "s"), edge("s", "b"), edge("b", "san"),
                edge("b", "m"), edge("san", "m"), edge("m", "sink"),
                edge("sink", "r"),
            ],
        }
        graph, comparison = run(doc)
        item = comparison["items"][0]
        verdicts = {(tuple(a["sanitizers_on_path"]), a["path_is_open"])
                    for a in item["alternatives"]}
        self.assertIn((("html_escaper",), False), verdicts)
        self.assertIn(((), True), verdicts)
        # An open branch makes the overall verdict blocked in both versions.
        self.assertEqual(item["old_verdict"], BLOCKED)
        self.assertEqual(item["new_verdict"], BLOCKED)
        self.assertEqual(item["classification"], UNCHANGED)

    def test_log_sanitizer_on_one_branch_only_effective_for_log(self):
        doc = {
            "graph_id": "g",
            "functions": [{"id": "f", "entry": "e", "returns": ["r"]}],
            "nodes": [
                node("e", "entry"),
                node("s", "source", field="x", source_label="SRC"),
                node("b", "branch"),
                node("san", "stmt", field="x", sanitizers=["log_redactor"]),
                node("m", "merge"),
                node("sink_log", "sink", field="x", category="log"),
                node("sink_html", "sink", field="x", category="html"),
                node("r", "return"),
            ],
            "edges": [
                edge("e", "s"), edge("s", "b"), edge("b", "san"),
                edge("b", "m"), edge("san", "m"),
                edge("m", "sink_log"), edge("m", "sink_html"),
                edge("sink_log", "r"), edge("sink_html", "r"),
            ],
        }
        graph, comparison = run(doc)
        items = bucket_map(comparison)
        for sink in ("sink_log:x", "sink_html:x"):
            alts = items[sink]["alternatives"]
            self.assertEqual(len(alts), 2)
        html_item = items["sink_html:x"]
        # Under NEW, log_redactor covers html on the sanitized branch, but the
        # raw branch stays open -> still blocked, with a covered alternative.
        covered = [a for a in html_item["alternatives"]
                   if not a["path_is_open"]]
        self.assertTrue(covered)
        self.assertEqual(covered[0]["effective_sanitizers"], ["log_redactor"])


class ClassificationTests(unittest.TestCase):
    def _all_sanitized_doc(self, sanitizer, category):
        return {
            "graph_id": "g",
            "functions": [{"id": "f", "entry": "e", "returns": ["r"]}],
            "nodes": [
                node("e", "entry"),
                node("s", "source", field="x", source_label="SRC"),
                node("san", "stmt", field="x", sanitizers=[sanitizer]),
                node("sink", "sink", field="x", category=category),
                node("r", "return"),
            ],
            "edges": [edge("e", "s"), edge("s", "san"),
                      edge("san", "sink"), edge("sink", "r")],
        }

    def test_newly_allowed_with_minimal_evidence(self):
        doc = self._all_sanitized_doc("log_redactor", "html")
        graph, comparison = run(doc)
        item = comparison["items"][0]
        self.assertEqual(item["old_verdict"], BLOCKED)
        self.assertEqual(item["new_verdict"], ALLOWED)
        self.assertEqual(item["classification"], NEWLY_ALLOWED)
        self.assertEqual(
            item["minimal_rule_evidence"],
            [{"change": "added", "sanitizer": "log_redactor",
              "category": "html"}])
        self.assertEqual(item["witness_path"],
                         ["s", "san", "sink"])

    def test_newly_blocked_when_rule_tuple_removed(self):
        new_rules = parse_rule_table({
            "version": "v2",
            "sanitizers": {
                "log_redactor": {"categories": ["log"]},
                "html_escaper": {"categories": []},
                "shell_quoter": {"categories": ["shell"]},
            },
        })
        doc = self._all_sanitized_doc("html_escaper", "html")
        graph, comparison = run(doc, new=new_rules)
        item = comparison["items"][0]
        self.assertEqual(item["old_verdict"], ALLOWED)
        self.assertEqual(item["new_verdict"], BLOCKED)
        self.assertEqual(item["classification"], NEWLY_BLOCKED)
        self.assertEqual(
            item["minimal_rule_evidence"],
            [{"change": "removed", "sanitizer": "html_escaper",
              "category": "html"}])

    def test_unchanged_allowed_when_never_restricted(self):
        doc = self._all_sanitized_doc("shell_quoter", "shell")
        graph, comparison = run(doc)
        item = comparison["items"][0]
        self.assertEqual(item["classification"], UNCHANGED)
        self.assertEqual(item["minimal_rule_evidence"], [])

    def test_undetermined_due_to_unmodeled_node(self):
        doc = {
            "graph_id": "g",
            "assumptions": ["external service absent"],
            "functions": [{"id": "f", "entry": "e", "returns": ["r"]}],
            "nodes": [
                node("e", "entry"),
                node("u", "unmodeled", field="x",
                     reason="remote response"),
                node("sink", "sink", field="x", category="html"),
                node("r", "return"),
            ],
            "edges": [edge("e", "u"), edge("u", "sink"), edge("sink", "r")],
        }
        graph, comparison = run(doc)
        item = comparison["items"][0]
        self.assertEqual(item["old_verdict"], UNDETERMINED)
        self.assertEqual(item["new_verdict"], UNDETERMINED)
        self.assertEqual(item["classification"], UNDETERMINED_BUCKET)
        self.assertTrue(any("unmodeled" in n for n in item["incomplete_reasons"]))

    def test_undetermined_due_to_unresolved_call(self):
        doc = {
            "graph_id": "g",
            "functions": [{"id": "f", "entry": "e", "returns": ["r"]}],
            "nodes": [
                node("e", "entry"),
                node("c", "call", callee="missing.fn",
                     bindings=[["p", "x"]], ret_field="y"),
                node("sink", "sink", field="y", category="shell"),
                node("r", "return"),
            ],
            "edges": [edge("e", "c"), edge("c", "sink"), edge("sink", "r")],
        }
        graph, comparison = run(doc)
        item = comparison["items"][0]
        self.assertEqual(item["classification"], UNDETERMINED_BUCKET)
        self.assertTrue(any("unresolved callee" in n
                            for n in item["incomplete_reasons"]))

    def test_unreachable_sink(self):
        doc = {
            "graph_id": "g",
            "functions": [{"id": "f", "entry": "e", "returns": ["r"]}],
            "nodes": [
                node("e", "entry"),
                node("sink", "sink", field="x", category="html"),
                node("r", "return"),
            ],
            "edges": [edge("e", "r")],
            "watch": [{"name": "dead", "sink": "sink", "field": "x"}],
        }
        graph, comparison = run(doc)
        item = comparison["items"][0]
        self.assertEqual(item["old_verdict"], UNREACHABLE)
        self.assertEqual(item["new_verdict"], UNREACHABLE)

    def test_rule_diff_is_symmetric_and_minimal(self):
        added, removed = diff_rule_tables(OLD, NEW)
        self.assertEqual(added, [("log_redactor", "html")])
        self.assertEqual(removed, [])
        added_back, removed_back = diff_rule_tables(NEW, OLD)
        self.assertEqual(added_back, [])
        self.assertEqual(removed_back, [("log_redactor", "html")])


class FieldIndependenceTests(unittest.TestCase):
    def test_same_function_fields_analyzed_independently(self):
        doc = {
            "graph_id": "g",
            "functions": [{"id": "f", "entry": "e", "returns": ["r"]}],
            "nodes": [
                node("e", "entry"),
                node("sa", "source", field="a", source_label="SRC_A"),
                node("sb", "source", field="b", source_label="SRC_B"),
                node("san_a", "stmt", field="a",
                     sanitizers=["html_escaper"]),
                node("sink_a", "sink", field="a", category="html"),
                node("sink_b", "sink", field="b", category="html"),
                node("r", "return"),
            ],
            "edges": [
                edge("e", "sa"), edge("e", "sb"),
                edge("sa", "san_a"), edge("san_a", "sink_a"),
                edge("sb", "sink_b"),
                edge("sink_a", "r"), edge("sink_b", "r"),
            ],
        }
        graph = parse_graph(doc)
        envs = Engine(graph).analyze()
        allowed = evaluate_sink(
            envs["sink_a"]["a"], OLD, "html")["verdict"]
        blocked = evaluate_sink(
            envs["sink_b"]["b"], OLD, "html")["verdict"]
        self.assertEqual(allowed, ALLOWED)
        self.assertEqual(blocked, BLOCKED)
        # No state leaks between fields.
        self.assertNotIn("b", envs["sink_a"])
        a_states = envs["sink_b"].get("a", frozenset())
        self.assertEqual(a_states, frozenset())


class CallReturnTests(unittest.TestCase):
    def _doc(self):
        return {
            "graph_id": "g",
            "functions": [
                {"id": "main", "entry": "m_e", "returns": ["m_r"]},
                {"id": "callee", "entry": "c_e", "returns": ["c_r"]},
            ],
            "nodes": [
                node("m_e", "entry", function="main"),
                node("m_s", "source", function="main", field="req",
                     source_label="SRC"),
                node("call", "call", function="main", callee="callee",
                     bindings=[["p", "req"]], ret_field="out"),
                node("m_sink", "sink", function="main", field="out",
                     category="shell"),
                node("m_r", "return", function="main"),
                node("c_e", "entry", function="callee", field="p"),
                node("c_san", "stmt", function="callee", field="p",
                     sanitizers=["shell_quoter"]),
                node("c_r", "return", function="callee", field="p"),
            ],
            "edges": [
                edge("m_e", "m_s"), edge("m_s", "call"),
                edge("call", "m_sink"), edge("m_sink", "m_r"),
                edge("c_e", "c_san"), edge("c_san", "c_r"),
            ],
        }

    def test_sanitizer_inside_callee_flows_back_to_caller(self):
        graph = parse_graph(self._doc())
        envs = Engine(graph).analyze(declared_roots=("main",))
        verdict = evaluate_sink(envs["m_sink"]["out"], OLD, "shell")
        self.assertEqual(verdict["verdict"], ALLOWED)
        states = envs["m_sink"]["out"]
        self.assertEqual({tuple(s.sanitizers) for s in states},
                         {("shell_quoter",)})

    def test_witness_expands_callee_and_tracks_formal(self):
        graph, comparison = run(self._doc(), roots=("main",))
        item = comparison["items"][0]
        self.assertEqual(
            item["witness_path"],
            ["m_s", "call", "c_e", "c_san", "c_r", "call", "m_sink"])
        san_steps = [s for s in item["step_explanation"]
                     if s.get("sanitizers_applied_here")]
        self.assertEqual(len(san_steps), 1)
        self.assertEqual(san_steps[0]["node"], "c_san")
        self.assertEqual(san_steps[0]["observed_fields"], ["p"])

    def test_caller_local_sanitizer_not_attributed_to_callee(self):
        doc = self._doc()
        doc["nodes"].insert(
            3,
            node("m_pre", "stmt", function="main", field="req",
                 sanitizers=["log_redactor"]))
        doc["edges"] = [
            edge("m_e", "m_s"), edge("m_s", "m_pre"),
            edge("m_pre", "call"), edge("call", "m_sink"),
            edge("m_sink", "m_r"),
            edge("c_e", "c_san"), edge("c_san", "c_r"),
        ]
        graph, comparison = run(doc, roots=("main",))
        item = comparison["items"][0]
        sans = {(s["node"], tuple(s["sanitizers_applied_here"]))
                for s in item["step_explanation"]
                if s.get("sanitizers_applied_here")}
        self.assertIn(("m_pre", ("log_redactor",)), sans)
        self.assertIn(("c_san", ("shell_quoter",)), sans)
        # The shell verdict must not rely on the log-only sanitizer.
        shell_effective = [
            a["effective_sanitizers"] for a in item["alternatives"]]
        for eff in shell_effective:
            self.assertNotIn("log_redactor", eff)


class TerminationTests(unittest.TestCase):
    def test_intraprocedural_loop_terminates_and_keeps_both_states(self):
        doc = {
            "graph_id": "loop",
            "functions": [{"id": "f", "entry": "e", "returns": ["r"]}],
            "nodes": [
                node("e", "entry"),
                node("s", "source", field="v", source_label="SRC"),
                node("b", "branch"),
                node("san", "stmt", field="v",
                     sanitizers=["shell_quoter"]),
                node("m", "merge"),
                node("sink", "sink", field="v", category="shell"),
                node("r", "return"),
            ],
            "edges": [
                edge("e", "s"), edge("s", "b"),
                edge("b", "san"), edge("b", "m"),
                edge("san", "m"), edge("m", "b"),
                edge("m", "sink"), edge("sink", "r"),
            ],
        }
        graph = parse_graph(doc)
        envs = Engine(graph).analyze()
        states = envs["sink"]["v"]
        # Exactly two distinct path states; repeated loop iterations add none.
        self.assertEqual(len(states), 2)
        self.assertEqual({tuple(x.sanitizers) for x in states},
                         {(), ("shell_quoter",)})

    def test_mutual_recursion_terminates(self):
        doc = {
            "graph_id": "rec",
            "functions": [
                {"id": "even", "entry": "ev_e", "returns": ["ev_r"]},
                {"id": "odd", "entry": "od_e", "returns": ["od_r"]},
            ],
            "nodes": [
                node("ev_e", "entry", function="even", field="n"),
                node("ev_b", "branch", function="even"),
                node("ev_call", "call", function="even", callee="odd",
                     bindings=[["n", "n"]], ret_field="n"),
                node("ev_r", "return", function="even", field="n"),
                node("od_e", "entry", function="odd", field="n"),
                node("od_b", "branch", function="odd"),
                node("od_call", "call", function="odd", callee="even",
                     bindings=[["n", "n"]], ret_field="n"),
                node("od_r", "return", function="odd", field="n"),
            ],
            "edges": [
                edge("ev_e", "ev_b"), edge("ev_b", "ev_call"),
                edge("ev_b", "ev_r"), edge("ev_call", "ev_r"),
                edge("od_e", "od_b"), edge("od_b", "od_call"),
                edge("od_b", "od_r"), edge("od_call", "od_r"),
            ],
        }
        graph = parse_graph(doc)
        engine = Engine(graph)
        # Must return rather than spin; bounded by the finite lattice.
        envs = engine.analyze(declared_roots=("even",))
        self.assertIn("ev_r", envs)

    def test_state_lattice_is_finite(self):
        # The per-field domain is bounded regardless of path count.
        ps = PathState(tainted=True)
        seen = set()
        current = frozenset([ps])
        for name in ("a", "b", "c", "d", "e"):
            current = frozenset(x.with_sanitizer(name) for x in current)
            seen.add(current)
        self.assertEqual(len(seen), 5)
        for states in seen:
            for x in states:
                self.assertTrue(set(x.sanitizers).issubset(set("abcde")))


class ReportTests(unittest.TestCase):
    def test_report_structure_and_counts(self):
        doc = {
            "graph_id": "g",
            "functions": [{"id": "f", "entry": "e", "returns": ["r"]}],
            "nodes": [
                node("e", "entry"),
                node("s", "source", field="x", source_label="SRC"),
                node("san", "stmt", field="x",
                     sanitizers=["log_redactor"]),
                node("sink", "sink", field="x", category="html"),
                node("r", "return"),
            ],
            "edges": [edge("e", "s"), edge("s", "san"),
                      edge("san", "sink"), edge("sink", "r")],
        }
        graph = parse_graph(doc)
        envs = Engine(graph).analyze()
        comparison = classify(graph, OLD, NEW, envs)
        report = build_report(graph, OLD, NEW, comparison)
        self.assertEqual(report["graph_id"], "g")
        self.assertEqual(report["rule_versions"],
                         {"old": "v1", "new": "v2"})
        self.assertEqual(report["summary"][NEWLY_ALLOWED], 1)
        self.assertEqual(
            report["rule_diff"]["added_pairs"],
            [["log_redactor", "html"]])


class LoaderErrorTests(unittest.TestCase):
    def test_bad_rule_document(self):
        with self.assertRaises(LoaderError):
            parse_rule_table({"sanitizers": {}})
        with self.assertRaises(LoaderError):
            parse_rule_table({"version": "x",
                              "sanitizers": {"s": "not-an-object"}})

    def test_bad_graph_document(self):
        with self.assertRaises(LoaderError):
            parse_graph({"nodes": [{"id": "x", "kind": "ghost",
                                    "function": "f"}]})
        with self.assertRaises(LoaderError):
            parse_graph({
                "nodes": [node("a", "entry"), node("a", "branch")],
                "edges": [], "functions": [],
            })
        with self.assertRaises(LoaderError):
            parse_graph({
                "nodes": [node("a", "entry"), node("b", "sink",
                                                    field="x", category="c")],
                "edges": [edge("a", "missing")],
                "functions": [],
            })
        with self.assertRaises(LoaderError):
            parse_graph({
                "nodes": [node("a", "entry"),
                          node("b", "sink", field="x", category="c")],
                "edges": [edge("a", "b")],
                "functions": [],
                "watch": [{"sink": "b"}],
            })


if __name__ == "__main__":
    unittest.main()
