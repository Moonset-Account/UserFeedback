"""端到端演示：非干涉性验证 + 非法降级节点定位。

运行方式（在工作区根目录）::

    python3 demo_e2e.py
"""

from secure_expr import (
    DowngradeViolation,
    If,
    Input,
    Label,
    Lit,
    Pair,
    Runtime,
    Throw,
    Level,
    noninterference_check,
)


def demo_noninterference() -> None:
    print("== 1. 非干涉性：两组仅秘密输入不同的样本 ==")
    flag = Input("b")
    secret = Input("s")
    # 安全程序：秘密只通过标签抬升影响可见性，不存在非法降级
    safe_program = Pair(
        Lit("public-ok"),
        If(flag, secret, Lit(0)),
    )
    inputs_a = {"b": (True, Level.SECRET), "s": (7, Level.SECRET)}
    inputs_b = {"b": (False, Level.SECRET), "s": (8, Level.SECRET)}
    result = noninterference_check(
        safe_program, inputs_a, inputs_b, [(), (0,), (1,)], Level.PUBLIC
    )
    print("consistent:", result.consistent)
    print("sample A observations:", [(o.kind, o.value) for o in result.observations_a])
    print("sample B observations:", [(o.kind, o.value) for o in result.observations_b])

    # 对照：高许可下两个样本的秘密取值确实不同
    high = noninterference_check(
        safe_program, inputs_a, inputs_b, [(1,)], Level.SECRET
    )
    print("at SECRET clearance they differ:", not high.consistent)
    print("secret values A/B:",
          high.observations_a[0].value, high.observations_b[0].value)


def demo_downgrade_localization() -> None:
    print("\n== 2. 非法降级节点定位 ==")
    flag = Input("b")
    secret = Input("s")
    bad_relabel = Label(secret, Level.PUBLIC)  # H -> L，违规节点
    vulnerable = Pair(
        Lit("public-ok"),
        If(flag, bad_relabel, Lit(0)),
    )
    runtime = Runtime(
        vulnerable, {"b": (True, Level.SECRET), "s": (7, Level.SECRET)}
    )
    # 未取用分支时不违规
    runtime_safe = Runtime(
        vulnerable, {"b": (False, Level.SECRET), "s": (7, Level.SECRET)}
    )
    print("untaken-branch observation:",
          runtime_safe.observe((1,), Level.SECRET))
    print("untaken-branch violations:", runtime_safe.violation_traces())

    # 取用违规分支：监控立即抛出并记录节点
    try:
        runtime.observe((1,), Level.PUBLIC)
    except DowngradeViolation as exc:
        print("caught violation node id:", exc.node_id,
              "| matches AST node:", exc.node_id == bad_relabel.node_id)
    print("audit log:", runtime.violation_traces())


def demo_exception_noninterference() -> None:
    print("\n== 3. 值隐藏与异常隐藏不可区分 ==")
    flag = Input("b")
    value_prog = If(flag, Lit(1), Lit(2))
    error_prog = If(flag, Throw(Lit("e")), Lit(2))
    inputs_true = {"b": (True, Level.SECRET)}
    rt_v = Runtime(value_prog, inputs_true)
    rt_e = Runtime(error_prog, inputs_true)
    print("hidden value:", rt_v.observe((), Level.PUBLIC))
    print("hidden error:", rt_e.observe((), Level.PUBLIC))
    print("indistinguishable:",
          rt_v.observe((), Level.PUBLIC) == rt_e.observe((), Level.PUBLIC))


if __name__ == "__main__":
    demo_noninterference()
    demo_downgrade_localization()
    demo_exception_noninterference()
