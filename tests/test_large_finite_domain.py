"""较大有限域上的终止性、确定性与去重稳定性（合成数据）。"""

import time
import unittest

from datalog import Atom, Literal, QueryEngine, Rule, Var

X, Y, Z = Var("X"), Var("Y"), Var("Z")


def build_chain(n: int) -> QueryEngine:
    engine = QueryEngine()
    for i in range(n):
        engine.add_fact("node", (i,))
    for i in range(n - 1):
        engine.add_fact("edge", (i, i + 1))
    engine.add_rule(
        Rule(Atom("reach", X, X), [Literal(Atom("node", X))]), location="r.refl"
    )
    engine.add_rule(
        Rule(Atom("reach", X, Y), [Literal(Atom("edge", X, Y))]), location="r.base"
    )
    engine.add_rule(
        Rule(
            Atom("reach", X, Y),
            [Literal(Atom("reach", X, Z)), Literal(Atom("edge", Z, Y))],
        ),
        location="r.rec",
    )
    return engine


class LargerFiniteDomainTest(unittest.TestCase):
    def test_chain_closure_is_complete_and_terminates(self):
        n = 60
        engine = build_chain(n)
        start = time.perf_counter()
        engine.evaluate()
        elapsed = time.perf_counter() - start
        # 链上任意 i <= j 都可达，共 n(n+1)/2 行；朴素不动点必须终止。
        expected = {(i, j) for i in range(n) for j in range(i, n)}
        self.assertEqual(set(engine.rows("reach")), expected)
        self.assertEqual(len(engine.rows("reach")), n * (n + 1) // 2)
        self.assertLess(elapsed, 10.0)

    def test_dense_graph_dedup_and_determinism(self):
        n = 25
        engine = QueryEngine()
        for i in range(n):
            engine.add_fact("node", (i,))
        for i in range(n):
            for j in range(n):
                if i != j:
                    engine.add_fact("edge", (i, j))
        engine.add_rule(
            Rule(Atom("reach", X, Y), [Literal(Atom("edge", X, Y))]), location="d.base"
        )
        engine.add_rule(
            Rule(
                Atom("reach", X, Y),
                [Literal(Atom("reach", X, Z)), Literal(Atom("edge", Z, Y))],
            ),
            location="d.rec",
        )
        engine.evaluate()
        # 完全图：递归规则经两步路径还能推出 reach(i,i)，故全部 n*n 个
        # 有序对都成立；尽管推导路径极多，每个有序对只保留一行。
        rows = engine.rows("reach")
        self.assertEqual(len(rows), n * n)
        self.assertEqual(len(set(rows)), len(rows))
        # 再求值一次结果完全一致（确定性、幂等）。
        again = engine.evaluate().rows("reach")
        self.assertEqual(again, rows)


if __name__ == "__main__":
    unittest.main()
