"""分层否定：合法分层、否定即失败、含否定依赖的环及规则位置返回。"""

import unittest

from datalog import (
    Atom,
    Literal,
    NOT,
    QueryEngine,
    Rule,
    StratificationError,
    Var,
)

X, Y, Z = Var("X"), Var("Y"), Var("Z")


class StratifiedNegationTest(unittest.TestCase):
    def test_negation_as_failure_with_closed_lower_stratum(self):
        # 找出没有出边的节点。严格范围限制下先用辅助 IDB has_edge(X)
        # 绑定键 X，再对其做 NOT 检查（题面：排除已经绑定键的匹配）。
        engine = QueryEngine()
        for node in ("a", "b", "c"):
            engine.add_fact("node", (node,))
        engine.add_fact("edge", ("a", "b"))
        engine.add_rule(
            Rule(Atom("has_edge", X), [Literal(Atom("edge", X, Y))]),
            location="sink.has_edge",
        )
        engine.add_rule(
            Rule(
                Atom("sink", X),
                [Literal(Atom("node", X)), NOT(Atom("has_edge", X))],
            ),
            location="sink.r1",
        )
        engine.evaluate()
        self.assertEqual(engine.rows("sink"), [("b",), ("c",)])
        self.assertEqual(engine.predicate_stratum("sink"), 1)
        self.assertEqual(engine.predicate_stratum("edge"), 0)

    def test_negation_interleaved_with_positive_join(self):
        # 已报名 enrollment(s, c)，通过 approval(s, c) 的学生才计入；
        # 缺席 approval 的报名行进入 pending(s, c)。
        engine = QueryEngine()
        engine.add_fact("enrollment", ("alice", "cs101"))
        engine.add_fact("enrollment", ("bob", "cs101"))
        engine.add_fact("enrollment", ("carol", "cs101"))
        engine.add_fact("approval", ("alice", "cs101"))
        engine.add_rule(
            Rule(
                Atom("approved_enrollment", X, Z),
                [
                    Literal(Atom("enrollment", X, Z)),
                    Literal(Atom("approval", X, Z)),
                ],
            ),
            location="enr.ok",
        )
        engine.add_rule(
            Rule(
                Atom("pending", X, Z),
                [
                    Literal(Atom("enrollment", X, Z)),
                    NOT(Atom("approval", X, Z)),
                ],
            ),
            location="enr.pending",
        )
        engine.evaluate()
        self.assertEqual(
            engine.rows("approved_enrollment"), [("alice", "cs101")]
        )
        self.assertEqual(
            engine.rows("pending"),
            [("bob", "cs101"), ("carol", "cs101")],
        )
        self.assertEqual(engine.predicate_stratum("pending"), 1)

    def test_chained_negation_creates_multiple_strata(self):
        # 第 1 层：reachable 之下取 complement；第 2 层再引用 complement。
        engine = QueryEngine()
        for node in ("a", "b", "c", "d"):
            engine.add_fact("node", (node,))
        engine.add_fact("edge", ("a", "b"))
        engine.add_fact("edge", ("b", "c"))
        engine.add_rule(
            Rule(Atom("has_edge", X), [Literal(Atom("edge", X, Z))]),
            location="edge.aux",
        )
        engine.add_rule(
            Rule(Atom("reach", X, X), [Literal(Atom("node", X))]),
            location="reach.refl",
        )
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
                Atom("unreachable_from_a", Y),
                [
                    Literal(Atom("node", Y)),
                    NOT(Atom("reach", "a", Y)),
                ],
            ),
            location="comp.r1",
        )
        engine.add_rule(
            Rule(
                Atom("isolated_report", Y),
                [
                    Literal(Atom("node", Y)),
                    NOT(Atom("reach", "a", Y)),
                    NOT(Atom("has_edge", Y)),
                ],
            ),
            location="comp.r2",
        )
        engine.evaluate()
        self.assertEqual(engine.predicate_stratum("reach"), 0)
        self.assertEqual(engine.predicate_stratum("unreachable_from_a"), 1)
        # isolated_report 否定了第 0 层的 reach 与 edge，仍为第 1 层；
        # 再构造一个引用第 1 层谓词并取反的规则，验证层数继续抬高。
        engine.add_rule(
            Rule(
                Atom("reachable_report", Y),
                [
                    Literal(Atom("node", Y)),
                    NOT(Atom("unreachable_from_a", Y)),
                ],
            ),
            location="comp.r3",
        )
        engine.evaluate()
        self.assertEqual(engine.predicate_stratum("reachable_report"), 2)
        self.assertEqual(
            engine.rows("unreachable_from_a"), [("d",)]
        )
        self.assertEqual(engine.rows("isolated_report"), [("d",)])

    def test_negative_cycle_self_recursion_reports_location(self):
        engine = QueryEngine()
        engine.add_fact("node", ("a",))
        engine.add_rule(
            Rule(
                Atom("p", X),
                [Literal(Atom("node", X)), NOT(Atom("p", X))],
            ),
            location="negcycle.self",
        )
        with self.assertRaises(StratificationError) as ctx:
            engine.evaluate()
        err = ctx.exception
        self.assertEqual(err.cycle, ["p", "p"])
        self.assertIn("negcycle.self", err.rule_locations)
        self.assertEqual(err.negative_edges, [("p", "p")])

    def test_negative_cycle_across_two_predicates(self):
        engine = QueryEngine()
        engine.add_fact("node", ("a",))
        engine.add_rule(
            Rule(
                Atom("p", X),
                [Literal(Atom("node", X)), NOT(Atom("q", X))],
            ),
            location="negcycle.p",
        )
        engine.add_rule(
            Rule(
                Atom("q", X),
                [Literal(Atom("node", X)), Literal(Atom("p", X))],
            ),
            location="negcycle.q",
        )
        with self.assertRaises(StratificationError) as ctx:
            engine.evaluate()
        err = ctx.exception
        self.assertEqual(err.cycle[0], err.cycle[-1])
        self.assertIn("p", err.cycle)
        self.assertIn("q", err.cycle)
        self.assertEqual(err.negative_edges, [("p", "q")])
        joined_locations = "|".join(err.rule_locations)
        self.assertIn("negcycle.p", joined_locations)
        self.assertIn("negcycle.q", joined_locations)

    def test_positive_dependency_through_negation_remains_stratifiable(self):
        # p :- NOT q；q 仅由事实定义，无环，应可分层而非误报。
        engine = QueryEngine()
        engine.add_fact("q", (1,))
        engine.add_fact("q", (2,))
        engine.add_fact("domain", (1,))
        engine.add_fact("domain", (2,))
        engine.add_fact("domain", (3,))
        engine.add_rule(
            Rule(
                Atom("p", X),
                [Literal(Atom("domain", X)), NOT(Atom("q", X))],
            ),
            location="ok.neg",
        )
        engine.evaluate()
        self.assertEqual(engine.rows("p"), [(3,)])
        self.assertEqual(engine.predicate_stratum("p"), 1)


if __name__ == "__main__":
    unittest.main()
