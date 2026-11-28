"""事实更正后重算：正递归结果扩张与否定（缺席）结论撤回的交错验证。"""

import unittest

from datalog import Atom, Literal, NOT, QueryEngine, Rule, Var

X, Y, Z = Var("X"), Var("Y"), Var("Z")


def reachability_with_negation_engine() -> QueryEngine:
    """构造题面要求的交错场景。

    关系：
    - edge(X,Y)：基础有向边；
    - reach(X,Y)：正递归传递闭包（含 X=X 的自反基例）；
    - unreachable(Y)：node(Y) 且 NOT reach("a", Y)，
      即“从 a 缺席可达”才成立的下游否定结论。
    """
    engine = QueryEngine()
    engine.add_fact("node", ("a",))
    engine.add_fact("node", ("b",))
    engine.add_fact("node", ("c",))
    engine.add_fact("node", ("d",))
    engine.add_fact("edge", ("a", "b"))
    engine.add_fact("edge", ("b", "c"))
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
            Atom("unreachable", Y),
            [Literal(Atom("node", Y)), NOT(Atom("reach", "a", Y))],
        ),
        location="unreach.r1",
    )
    return engine


class FactCorrectionRecomputeTest(unittest.TestCase):
    def test_new_fact_grows_recursion_and_withdraws_negation_result(self):
        engine = reachability_with_negation_engine()
        engine.evaluate()

        # 初始：a 只能到达 a、b、c；d 因缺席可达而成立 unreachable。
        self.assertEqual(
            engine.rows("reach"),
            [
                ("a", "a"),
                ("a", "b"),
                ("a", "c"),
                ("b", "b"),
                ("b", "c"),
                ("c", "c"),
                ("d", "d"),
            ],
        )
        self.assertTrue(engine.contains("unreachable", ("d",)))
        self.assertEqual(engine.rows("unreachable"), [("d",)])

        # 更正：加入一条新基础事实 edge(c, d)。
        engine.add_fact("edge", ("c", "d"))
        engine.evaluate()  # 全量重算不动点

        # 1) 新事实沿正递归向上游扩张：reach 增加 (a,d)、(b,d)，
        #    且自反/原有行保持不变。
        self.assertTrue(engine.contains("edge", ("c", "d")))
        self.assertTrue(engine.contains("reach", ("c", "d")))
        self.assertTrue(engine.contains("reach", ("b", "d")))
        self.assertTrue(engine.contains("reach", ("a", "d")))
        self.assertIn(("a", "d"), engine.rows("reach"))
        self.assertIn(("b", "d"), engine.rows("reach"))
        # 原结果未丢失。
        for kept in (("a", "a"), ("a", "b"), ("a", "c"), ("b", "c")):
            self.assertTrue(engine.contains("reach", kept))

        # 2) 下游因“缺席”才成立的结论被撤去：unreachable(d) 不再成立。
        self.assertFalse(engine.contains("unreachable", ("d",)))
        self.assertEqual(engine.rows("unreachable"), [])

        # 3) 被撤回的行不再有解释；而新行 (a,d) 可展开一条有限正向推导，
        #    其叶子包含新加入的基础事实。
        with self.assertRaises(KeyError):
            engine.explain("unreachable", ("d",))
        proof = engine.explain("reach", ("a", "d"))

        def leaves(node):
            if node.is_fact():
                return [node.tuple_value]
            out = []
            for child in node.children:
                if hasattr(child, "is_fact"):
                    out.extend(leaves(child))
            return out

        self.assertIn(("c", "d"), leaves(proof))

    def test_retract_fact_restores_negation_result(self):
        engine = reachability_with_negation_engine()
        engine.add_fact("edge", ("c", "d"))
        engine.evaluate()
        self.assertEqual(engine.rows("unreachable"), [])
        self.assertTrue(engine.contains("reach", ("a", "d")))

        # 撤去事实后重算：否定结论应恢复。
        removed = engine.retract_fact("edge", ("c", "d"))
        self.assertTrue(removed)
        engine.evaluate()
        self.assertFalse(engine.contains("reach", ("a", "d")))
        self.assertEqual(engine.rows("unreachable"), [("d",)])

        # 撤去一个不存在的事实返回 False，且不破坏结果。
        self.assertFalse(engine.retract_fact("edge", ("z", "z")))

    def test_replace_facts_wholesale_recompute(self):
        engine = reachability_with_negation_engine()
        engine.evaluate()
        self.assertTrue(engine.contains("unreachable", ("d",)))

        # 整体更正 edge 关系，使 d 经新路径可达。
        engine.replace_facts(
            "edge",
            [("a", "b"), ("b", "c"), ("c", "d"), ("a", "d")],
        )
        engine.evaluate()
        self.assertEqual(engine.rows("unreachable"), [])
        self.assertTrue(engine.contains("reach", ("a", "d")))

    def test_duplicate_fact_correction_is_idempotent(self):
        engine = reachability_with_negation_engine()
        engine.evaluate()
        before = engine.rows("reach")
        engine.add_fact("edge", ("a", "b"))  # 已存在的事实
        engine.evaluate()
        self.assertEqual(engine.rows("reach"), before)
        self.assertEqual(engine.rows("unreachable"), [("d",)])

    def test_negation_withdrawal_layer_is_still_closed_after_recompute(self):
        engine = reachability_with_negation_engine()
        engine.evaluate()
        proof_before = engine.explain("unreachable", ("d",))
        check = proof_before.negation_checks()[0]
        self.assertEqual(check.predicate, "reach")
        self.assertEqual(check.stratum, 0)
        self.assertEqual(proof_before.stratum, 1)

        engine.add_fact("edge", ("c", "d"))
        engine.evaluate()
        # 重算后分层不变，只是缺席检查不再成功。
        self.assertEqual(engine.predicate_stratum("reach"), 0)
        self.assertEqual(engine.predicate_stratum("unreachable"), 1)
        self.assertFalse(engine.contains("unreachable", ("d",)))


if __name__ == "__main__":
    unittest.main()
