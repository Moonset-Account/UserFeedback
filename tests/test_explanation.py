"""结果解释：有限正向推导树、否定检查及其已封闭层标注。"""

import unittest

from datalog import (
    Atom,
    Literal,
    NegationCheck,
    NOT,
    ProofNode,
    QueryEngine,
    Rule,
    Var,
)

X, Y, Z = Var("X"), Var("Y"), Var("Z")


def build_engine() -> QueryEngine:
    engine = QueryEngine()
    engine.add_fact("edge", ("a", "b"))
    engine.add_fact("edge", ("b", "c"))
    engine.add_fact("edge", ("c", "d"))
    engine.add_fact("node", ("a",))
    engine.add_fact("node", ("b",))
    engine.add_fact("node", ("c",))
    engine.add_fact("node", ("d",))
    engine.add_rule(
        Rule(Atom("reach", X, Y), [Literal(Atom("edge", X, Y))]),
        location="reach.base",
    )
    engine.add_rule(
        Rule(
            Atom("reach", X, Y),
            [Literal(Atom("reach", X, Z)), Literal(Atom("edge", Z, Y))],
        ),
        location="reach.rec",
    )
    engine.add_rule(
        Rule(
            Atom("unreachable", Y),
            [Literal(Atom("node", Y)), NOT(Atom("reach", "a", Y))],
        ),
        location="unreach.r1",
    )
    return engine


class ExplanationTest(unittest.TestCase):
    def setUp(self):
        # e 是与 a 不连通的孤立节点，unreachable(e) 因缺席而成立。
        self.engine = build_engine()
        self.engine.add_fact("node", ("e",))
        self.engine.evaluate()

    def test_fact_proof_is_leaf_at_stratum_zero(self):
        proof = self.engine.explain("edge", ("a", "b"))
        self.assertIsInstance(proof, ProofNode)
        self.assertTrue(proof.is_fact())
        self.assertEqual(proof.stratum, 0)
        self.assertEqual(proof.children, ())

    def test_derived_row_has_finite_positive_proof_tree(self):
        proof = self.engine.explain("reach", ("a", "d"))
        self.assertFalse(proof.is_fact())
        self.assertEqual(proof.predicate, "reach")
        self.assertEqual(proof.tuple_value, ("a", "d"))
        # 递归推导：reach(a,d) :- reach(a,?), edge(?,d)
        self.assertEqual(proof.rule.location, "reach.rec")
        children = proof.children
        self.assertEqual(len(children), 2)
        reach_child, edge_child = children
        self.assertIsInstance(reach_child, ProofNode)
        self.assertEqual(reach_child.predicate, "reach")
        self.assertTrue(edge_child.is_fact())
        self.assertEqual(edge_child.tuple_value, ("c", "d"))

        def all_leaves_are_facts(node: ProofNode) -> bool:
            if node.is_fact():
                return True
            return all(
                isinstance(c, NegationCheck)
                or (isinstance(c, ProofNode) and all_leaves_are_facts(c))
                for c in node.children
            )

        self.assertTrue(all_leaves_are_facts(proof))

    def test_proof_depth_is_bounded_by_chain_length(self):
        # 长度 3 的链：推导树中的基础事实叶子数恰为路径边数。
        proof = self.engine.explain("reach", ("a", "d"))

        def fact_count(node: ProofNode) -> int:
            if node.is_fact():
                return 1
            return sum(
                fact_count(c) for c in node.children if isinstance(c, ProofNode)
            )

        self.assertEqual(fact_count(proof), 3)

    def test_negation_check_grounds_variables_and_records_stratum(self):
        # 孤立节点 x 没有出边：先用辅助 IDB has_edge 绑定键，再做缺席检查，
        # 检查元组中的参数必须已被绑定为具体常量。
        engine = QueryEngine()
        engine.add_fact("node", ("x",))
        engine.add_rule(
            Rule(Atom("has_edge", X), [Literal(Atom("edge", X, Y))]),
            location="sink.aux",
        )
        engine.add_rule(
            Rule(
                Atom("sink", X),
                [Literal(Atom("node", X)), NOT(Atom("has_edge", X))],
            ),
            location="sink.r1",
        )
        engine.evaluate()
        proof = engine.explain("sink", ("x",))
        checks = proof.negation_checks()
        self.assertEqual(len(checks), 1)
        check = checks[0]
        self.assertEqual(check.predicate, "has_edge")
        self.assertEqual(check.tuple_value, ("x",))
        self.assertTrue(all(not isinstance(v, Var) for v in check.tuple_value))
        self.assertEqual(check.stratum, 0)
        self.assertEqual(engine.predicate_stratum("has_edge"), 0)
        self.assertTrue(check.absent)
        self.assertIn("第 0 层已封闭", str(check))

    def test_negated_relation_stratum_is_strictly_lower(self):
        proof = self.engine.explain("unreachable", ("e",))
        self.assertEqual(proof.stratum, 1)
        checks = proof.negation_checks()
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].predicate, "reach")
        self.assertEqual(checks[0].tuple_value, ("a", "e"))
        self.assertEqual(checks[0].stratum, 0)
        self.assertLess(checks[0].stratum, proof.stratum)

    def test_explain_unknown_row_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.engine.explain("reach", ("z", "z"))

    def test_format_proof_is_readable_and_indented(self):
        proof = self.engine.explain("reach", ("a", "c"))
        text = self.engine.format_proof(proof)
        self.assertIn("reach('a', 'c')", text)
        self.assertIn("规则 reach", text)
        self.assertIn("基础事实", text)
        self.assertIn("\n  ", text)

    def test_every_output_row_has_an_explanation(self):
        for predicate in ("edge", "reach", "node", "unreachable"):
            for row in self.engine.rows(predicate):
                proof = self.engine.explain(predicate, row)
                self.assertEqual(proof.tuple_value, row)

    def test_negation_proof_renders_closed_stratum(self):
        proof = self.engine.explain("unreachable", ("e",))
        text = self.engine.format_proof(proof)
        self.assertIn("NOT reach('a', 'e')", text)
        self.assertIn("第 0 层已封闭", text)


if __name__ == "__main__":
    unittest.main()
