"""基础求值：多关系连接、常量选择、空关系、去重与静态安全检查。"""

import unittest

from datalog import (
    Atom,
    DatalogError,
    Literal,
    NOT,
    QueryEngine,
    Rule,
    SafetyError,
    Var,
)

X, Y, Z = Var("X"), Var("Y"), Var("Z")


class BasicEvaluationTest(unittest.TestCase):
    def test_multi_relation_join_with_constant_selection(self):
        # person(name, dept) ⨝ dept(dept, city)，并选择城市常量 "SH"。
        engine = QueryEngine()
        engine.add_fact("person", ("alice", "eng"))
        engine.add_fact("person", ("bob", "sales"))
        engine.add_fact("person", ("carol", "eng"))
        engine.add_fact("dept", ("eng", "SH"))
        engine.add_fact("dept", ("sales", "BJ"))

        engine.add_rule(
            Rule(
                Atom("works_in", X, Y),
                [
                    Literal(Atom("person", X, Z)),
                    Literal(Atom("dept", Z, Y)),
                ],
            ),
            location="join.r1",
        )
        engine.add_rule(
            Rule(
                Atom("sh_employee", X),
                [
                    Literal(Atom("person", X, Z)),
                    Literal(Atom("dept", Z, "SH")),
                ],
            ),
            location="join.r2",
        )
        engine.evaluate()

        self.assertEqual(
            engine.rows("works_in"),
            [("alice", "SH"), ("bob", "BJ"), ("carol", "SH")],
        )
        self.assertEqual(engine.rows("sh_employee"), [("alice",), ("carol",)])

    def test_repeated_variable_requires_equal_positions(self):
        engine = QueryEngine()
        engine.add_fact("pair", (1, 1))
        engine.add_fact("pair", (1, 2))
        engine.add_fact("pair", (2, 2))
        engine.add_rule(
            Rule(Atom("diag", X), [Literal(Atom("pair", X, X))]),
            location="diag.r1",
        )
        engine.evaluate()
        self.assertEqual(engine.rows("diag"), [(1,), (2,)])

    def test_empty_relation_produces_nothing(self):
        engine = QueryEngine()
        engine.add_fact("edge", ("a", "b"))
        engine.add_rule(
            Rule(Atom("target", X), [Literal(Atom("missing", X, Y))]),
            location="empty.r1",
        )
        engine.evaluate()
        self.assertEqual(engine.rows("target"), [])
        self.assertEqual(engine.rows("missing"), [])

    def test_duplicate_facts_and_duplicate_derivations_collapse(self):
        engine = QueryEngine()
        for _ in range(3):
            engine.add_fact("edge", ("a", "b"))
        engine.add_fact("edge", ("b", "c"))
        # 两条规则都能推出同一行，结果中也只能出现一次。
        engine.add_rule(
            Rule(Atom("path", X, Y), [Literal(Atom("edge", X, Y))]),
            location="dup.r1",
        )
        engine.add_rule(
            Rule(
                Atom("path", X, Y),
                [
                    Literal(Atom("edge", X, Z)),
                    Literal(Atom("edge", Z, Y)),
                ],
            ),
            location="dup.r2",
        )
        engine.evaluate()
        self.assertEqual(
            engine.rows("path"),
            [("a", "b"), ("a", "c"), ("b", "c")],
        )
        self.assertEqual(engine.rows("edge"), [("a", "b"), ("b", "c")])

    def test_arithmetic_constants_in_head_and_integer_terms(self):
        engine = QueryEngine()
        engine.add_fact("num", (10,))
        engine.add_fact("num", (20,))
        engine.add_rule(
            Rule(Atom("bounded", X), [Literal(Atom("num", X))]),
            location="num.r1",
        )
        engine.evaluate()
        self.assertEqual(engine.rows("bounded"), [(10,), (20,)])

    def test_arity_conflict_is_rejected(self):
        engine = QueryEngine()
        engine.add_fact("edge", ("a", "b"))
        engine.add_rule(
            Rule(Atom("edge", X), [Literal(Atom("node", X))]),
            location="arity.bad",
        )
        with self.assertRaises(SafetyError):
            engine.evaluate()

    def test_unbound_negation_variable_is_unsafe(self):
        engine = QueryEngine()
        engine.add_fact("node", ("a",))
        engine.add_rule(
            Rule(
                Atom("bad", X, Y),
                [Literal(Atom("node", X)), NOT(Atom("edge", X, Y))],
            ),
            location="safety.neg",
        )
        with self.assertRaises(SafetyError):
            engine.evaluate()

    def test_unbound_head_variable_is_unsafe(self):
        engine = QueryEngine()
        engine.add_fact("node", ("a",))
        engine.add_rule(
            Rule(
                Atom("bad", X, Y),
                [Literal(Atom("node", X))],
            ),
            location="safety.head",
        )
        with self.assertRaises(SafetyError):
            engine.evaluate()

    def test_query_before_evaluate_raises(self):
        engine = QueryEngine()
        engine.add_fact("node", ("a",))
        with self.assertRaises(DatalogError):
            engine.rows("node")


if __name__ == "__main__":
    unittest.main()
