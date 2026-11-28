"""端到端演示：分层不动点、推导解释、事实更正后的扩张与撤回。

运行：python3 -I demo.py
仅使用标准库与本地 datalog 包，不联网、不安装依赖。
"""

from datalog import Atom, Literal, NOT, QueryEngine, Rule, Var, StratificationError

X, Y, Z = Var("X"), Var("Y"), Var("Z")


def build() -> QueryEngine:
    engine = QueryEngine()
    for node in ("a", "b", "c", "d"):
        engine.add_fact("node", (node,))
    engine.add_fact("edge", ("a", "b"))
    engine.add_fact("edge", ("b", "c"))
    engine.add_rule(
        Rule(Atom("reach", X, X), [Literal(Atom("node", X))]), location="reach.refl"
    )
    engine.add_rule(
        Rule(Atom("reach", X, Y), [Literal(Atom("edge", X, Y))]), location="reach.base"
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


def main() -> None:
    engine = build().evaluate()
    print("== 分层报告 ==")
    for level, info in engine.strata().items():
        print(f"第 {level} 层: 谓词={info['predicates']} 规则={info['rule_locations']}")

    print("\n== 初始结果 ==")
    print("reach       =", engine.rows("reach"))
    print("unreachable =", engine.rows("unreachable"))

    print("\n== unreachable(d) 的推导树（否定检查标注已封闭层）==")
    print(engine.format_proof(engine.explain("unreachable", ("d",))))

    print("\n== 更正：加入事实 edge(c, d) 后重算 ==")
    engine.add_fact("edge", ("c", "d"))
    engine.evaluate()
    print("reach       =", engine.rows("reach"))
    print("unreachable =", engine.rows("unreachable"))
    print("(a,d) 在 reach 中:", engine.contains("reach", ("a", "d")))
    print("unreachable(d) 已撤回:", not engine.contains("unreachable", ("d",)))

    print("\n== 新行 reach(a,d) 的有限正向推导 ==")
    print(engine.format_proof(engine.explain("reach", ("a", "d"))))

    print("\n== 含否定依赖的环（应被拒绝并返回规则位置）==")
    bad = QueryEngine()
    bad.add_fact("node", ("a",))
    bad.add_rule(
        Rule(
            Atom("p", X),
            [Literal(Atom("node", X)), NOT(Atom("p", X))],
        ),
        location="bad.self_neg",
    )
    try:
        bad.evaluate()
    except StratificationError as exc:
        print("StratificationError:", exc)
        print("cycle =", exc.cycle)
        print("rule_locations =", exc.rule_locations)
        print("negative_edges =", exc.negative_edges)


if __name__ == "__main__":
    main()
