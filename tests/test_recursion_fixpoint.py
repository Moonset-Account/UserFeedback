"""正递归成环、不动点终止性与有限域上的闭包计算。"""

import time
import unittest

from datalog import Atom, Literal, QueryEngine, Rule, Var

X, Y, Z = Var("X"), Var("Y"), Var("Z")


def transitive_engine() -> QueryEngine:
    engine = QueryEngine()
    engine.add_fact("edge", ("a", "b"))
    engine.add_fact("edge", ("b", "c"))
    engine.add_fact("edge", ("c", "d"))
    engine.add_fact("edge", ("d", "b"))  # 成环：b -> c -> d -> b
    engine.add_rule(
        Rule(Atom("reach", X, Y), [Literal(Atom("edge", X, Y))]),
        location="reach.base",
    )
    engine.add_rule(
        Rule(
            Atom("reach", X, Y),
            [
                Literal(Atom("reach", X, Z)),
                Literal(Atom("edge", Z, Y)),
            ],
        ),
        location="reach.rec",
    )
    return engine


class RecursionFixpointTest(unittest.TestCase):
    def test_transitive_closure_over_a_cyclic_graph(self):
        engine = transitive_engine().evaluate()
        # a 可到达图中其余全部节点；b/c/d 在强连通环中互相可达。
        self.assertEqual(
            engine.rows("reach"),
            [
                ("a", "b"),
                ("a", "c"),
                ("a", "d"),
                ("b", "b"),
                ("b", "c"),
                ("b", "d"),
                ("c", "b"),
                ("c", "c"),
                ("c", "d"),
                ("d", "b"),
                ("d", "c"),
                ("d", "d"),
            ],
        )

    def test_positive_cycle_keeps_single_stratum_and_terminates(self):
        engine = transitive_engine().evaluate()
        report = engine.strata()
        # edge 与 reach 同处第 0 层：纯正向递归不产生新分层。
        self.assertEqual(engine.predicate_stratum("edge"), 0)
        self.assertEqual(engine.predicate_stratum("reach"), 0)
        self.assertIn("reach.base", report[0]["rule_locations"])
        self.assertIn("reach.rec", report[0]["rule_locations"])

    def test_fixpoint_terminates_quickly_on_finite_relations(self):
        engine = transitive_engine()
        start = time.perf_counter()
        engine.evaluate()
        elapsed = time.perf_counter() - start
        # 朴素集合不动点在 4 节点小图上应在毫秒级完成；核心是必须终止。
        self.assertLess(elapsed, 5.0)
        self.assertTrue(engine.contains("reach", ("d", "b")))

    def test_mutual_recursion_two_predicates(self):
        # even/odd 式互递归：connected(X,Y) 与 connected_back(Y,X) 互推。
        engine = QueryEngine()
        engine.add_fact("link", (1, 2))
        engine.add_fact("link", (2, 3))
        engine.add_rule(
            Rule(Atom("conn", X, Y), [Literal(Atom("link", X, Y))]),
            location="conn.base",
        )
        engine.add_rule(
            Rule(
                Atom("conn", X, Y),
                [
                    Literal(Atom("conn", X, Z)),
                    Literal(Atom("link", Z, Y)),
                ],
            ),
            location="conn.rec",
        )
        engine.evaluate()
        self.assertEqual(
            engine.rows("conn"),
            [(1, 2), (1, 3), (2, 3)],
        )

    def test_recompute_is_deterministic(self):
        first = transitive_engine().evaluate().rows("reach")
        second = transitive_engine().evaluate().rows("reach")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
