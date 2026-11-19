"""验收用例：权限拒绝传播分析。

覆盖矩阵
========

可接受结果（分析器应正确承认的正常/拒绝出口）
----------------------------------------------
* 顺序短路：Deny 后的 Return 不可达；
* 干净路径：从未拒绝的正常返回；
* 分支合流：同位置出口按“曾经拒绝 / 从未拒绝”拆成两个出口；
* 调用内联：被调函数出口、位置与污点历史在调用点保留；
* finally 正常结束：保留主体待传播结果；
* 隐式返回：函数体落到块尾等价正常返回。

应被阻止/拒绝的结果（题目交叉约束不允许出现的情形）
----------------------------------------------------
* 被 catch 吞掉的拒绝不得再以 ThrowExit 漏出；
* 类别不匹配的 catch 不得消费权限拒绝；
* finally 显式返回/抛出必须替换待传播结果，原结果不得残留；
* 递归调用 / 未知名函数 / 非法节点必须抛专用异常，分析不得静默继续。

边界与交错场景
--------------
* catch 把拒绝替换成普通错误 / 替换成另一次拒绝（事件链保留两次入口）；
* 嵌套 catch 的多层消费；
* finally 覆盖普通返回与覆盖拒绝两条路径；
* catch 纯普通错误时不产生拒绝事件。
"""

from __future__ import annotations

import unittest

from denialflow import (
    DENIED,
    ERROR,
    Analyzer,
    Branch,
    Call,
    Catch,
    Caught,
    Deny,
    Error,
    Finally,
    FinallyOverride,
    Function,
    InvalidNodeError,
    Pass,
    Program,
    RecursiveCallError,
    Return,
    ReturnExit,
    Sequence,
    ThrowExit,
    UnknownFunctionError,
    analyze,
)
from denialflow.analyzer import (
    FATE_FINALLY_DENIED,
    FATE_FINALLY_ERROR,
    FATE_FINALLY_RETURN,
    FATE_PROPAGATES_DENIED,
    FATE_TO_DENIED,
    FATE_TO_ERROR,
    FATE_TO_RETURN,
)


def _program(body, entry="main", extra=()):
    return Program((Function(entry, body),) + tuple(extra), entry=entry)


class StraightLineTests(unittest.TestCase):
    """顺序结构与叶节点的基础语义。"""

    def test_deny_propagates_and_shorts_circuit_sequence(self):
        # Deny 之后的 Return 永远不可达：拒绝必须漏出，且只有一个出口。
        tree = Sequence([Deny("check"), Return("unreachable")], "seq")
        report = analyze(_program(tree))

        self.assertEqual(len(report.exits), 1)
        exit_ = report.exits[0]
        self.assertIsInstance(exit_, ThrowExit)
        self.assertEqual(exit_.kind, DENIED)
        self.assertEqual(exit_.origin, "check")
        self.assertEqual(exit_.state.denials, ("check",))
        self.assertFalse(report.is_safe_to_proceed())
        self.assertEqual(report.denial_origins, ("check",))

        fate = report.fate_of("check")
        self.assertEqual(len(fate), 1)
        self.assertEqual(fate[0].kind, FATE_PROPAGATES_DENIED)
        self.assertIsNone(fate[0].consumed_at)
        self.assertEqual(report.unconsumed_origins(), ("check",))

    def test_clean_return_is_accepted_and_distinct_from_tainted(self):
        tree = Sequence([Pass("step1"), Return("ok")], "seq")
        report = analyze(_program(tree))

        self.assertEqual(len(report.exits), 1)
        self.assertIsInstance(report.exits[0], ReturnExit)
        self.assertEqual(report.exits[0].origin, "ok")
        self.assertFalse(report.exits[0].ever_denied)
        self.assertEqual(report.clean_returns(), report.exits)
        self.assertEqual(report.tainted_returns(), ())
        self.assertEqual(report.denial_origins, ())
        self.assertTrue(report.is_safe_to_proceed())

    def test_plain_error_does_not_register_denial_origin(self):
        tree = Sequence([Error("boom"), Return("unreachable")], "seq")
        report = analyze(_program(tree))

        self.assertEqual(len(report.exits), 1)
        exit_ = report.exits[0]
        self.assertIsInstance(exit_, ThrowExit)
        self.assertEqual(exit_.kind, ERROR)
        self.assertEqual(exit_.origin, "boom")
        self.assertEqual(report.denial_origins, ())
        self.assertEqual(report.error_exits(), (exit_,))
        # 普通错误不是权限拒绝，因此“没有拒绝漏出”判定仍为 True。
        self.assertTrue(report.is_safe_to_proceed())

    def test_implicit_return_at_function_end(self):
        report = analyze(_program(Pass("only-pass")))
        self.assertEqual(len(report.exits), 1)
        self.assertIsInstance(report.exits[0], ReturnExit)
        self.assertEqual(report.exits[0].origin, "main:<implicit-return>")


class CatchTests(unittest.TestCase):
    """catch 对权限拒绝的消费、替换与过滤。"""

    def test_catch_swallow_converts_denial_to_return(self):
        # 可接受结果：拒绝被捕获后改成正常返回，但出口必须保留污点。
        tree = Catch(
            body=Deny("check"),
            handler=Return("fallback"),
            location="try1",
        )
        report = analyze(_program(tree))

        self.assertEqual(len(report.exits), 1)
        exit_ = report.exits[0]
        self.assertIsInstance(exit_, ReturnExit)
        self.assertEqual(exit_.origin, "fallback")
        self.assertTrue(exit_.ever_denied)
        self.assertEqual(exit_.state.denials, ("check",))
        self.assertEqual(report.tainted_returns(), (exit_,))
        self.assertEqual(report.denial_exits(), ())
        self.assertTrue(report.is_safe_to_proceed())

        fate = report.fate_of("check")[0]
        self.assertEqual(fate.kind, FATE_TO_RETURN)
        self.assertEqual(fate.consumed_at, "try1")
        self.assertIsInstance(fate.events[0], Caught)
        self.assertEqual(fate.events[0].replacement, "return")
        self.assertEqual(report.consumed_origins(), ("check",))

    def test_catch_replaces_denial_with_plain_error(self):
        tree = Catch(
            body=Deny("check"),
            handler=Error("converted"),
            location="try1",
        )
        report = analyze(_program(tree))

        self.assertEqual(len(report.exits), 1)
        exit_ = report.exits[0]
        self.assertIsInstance(exit_, ThrowExit)
        self.assertEqual(exit_.kind, ERROR)
        self.assertEqual(exit_.origin, "converted")
        # 曾经拒绝的历史不能因为换成普通错误就消失。
        self.assertEqual(exit_.state.denials, ("check",))
        self.assertEqual(report.fate_of("check")[0].kind, FATE_TO_ERROR)
        self.assertEqual(report.denial_exits(), ())

    def test_catch_rethrows_new_denial_keeps_both_origins(self):
        tree = Catch(
            body=Deny("check1"),
            handler=Deny("check2"),
            location="try1",
        )
        report = analyze(_program(tree))

        self.assertEqual(len(report.exits), 1)
        exit_ = report.exits[0]
        self.assertIsInstance(exit_, ThrowExit)
        self.assertEqual(exit_.kind, DENIED)
        self.assertEqual(exit_.origin, "check2")
        # 两个拒绝入口都必须在历史里，顺序按出现先后。
        self.assertEqual(exit_.state.denials, ("check1", "check2"))

        fates = {(f.origin, f.kind) for f in report.fates}
        self.assertIn(("check1", FATE_TO_DENIED), fates)
        self.assertIn(("check2", FATE_PROPAGATES_DENIED), fates)
        caught = exit_.state.events[0]
        self.assertIsInstance(caught, Caught)
        self.assertEqual(caught.denials, ("check1",))
        self.assertEqual(caught.replacement, "denied")

    def test_filter_mismatch_must_not_consume_denial(self):
        # 只捕获普通错误的 catch 对权限拒绝无效：拒绝必须原样漏出。
        tree = Catch(
            body=Deny("check"),
            handler=Return("fallback"),
            location="try-error-only",
            catches=(ERROR,),
        )
        report = analyze(_program(tree))

        self.assertEqual(len(report.exits), 1)
        exit_ = report.exits[0]
        self.assertIsInstance(exit_, ThrowExit)
        self.assertEqual(exit_.kind, DENIED)
        self.assertEqual(exit_.origin, "check")
        self.assertEqual(exit_.state.events, ())
        self.assertEqual(report.fate_of("check")[0].kind, FATE_PROPAGATES_DENIED)
        self.assertIsNone(report.fate_of("check")[0].consumed_at)

    def test_catching_plain_error_emits_no_denial_event(self):
        tree = Catch(
            body=Error("boom"),
            handler=Return("recovered"),
            location="try1",
        )
        report = analyze(_program(tree))

        self.assertEqual(len(report.exits), 1)
        self.assertIsInstance(report.exits[0], ReturnExit)
        self.assertEqual(report.exits[0].state.denials, ())
        self.assertEqual(report.exits[0].state.events, ())
        self.assertEqual(report.denial_origins, ())

    def test_nested_catches_record_full_consumption_chain(self):
        tree = Catch(
            body=Catch(
                body=Deny("inner-check"),
                handler=Deny("middle-check"),
                location="inner-try",
            ),
            handler=Return("outer-fallback"),
            location="outer-try",
        )
        report = analyze(_program(tree))

        exit_ = report.exits[0]
        self.assertIsInstance(exit_, ReturnExit)
        self.assertEqual(exit_.state.denials, ("inner-check", "middle-check"))
        events = exit_.state.events
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].at, "inner-try")
        self.assertEqual(events[1].at, "outer-try")
        # 外层捕获时两个拒绝入口一起被消费。
        self.assertEqual(events[1].denials, ("inner-check", "middle-check"))

        inner_fate = report.fate_of("inner-check")[0]
        self.assertEqual(inner_fate.kind, FATE_TO_RETURN)
        self.assertEqual(inner_fate.consumed_at, "inner-try")


class BranchTests(unittest.TestCase):
    """分支合流后必须仍能区分曾经拒绝与从未拒绝的出口。"""

    def test_merge_splits_same_origin_by_taint(self):
        # 两个分支到达同一个 Return 位置，但 true 分支先经历过被吞的拒绝。
        shared_return = Return("shared")
        tree = Branch(
            condition="has-permission",
            when_true=Catch(
                body=Deny("check"),
                handler=shared_return,
                location="try1",
            ),
            when_false=shared_return,
            location="branch1",
        )
        report = analyze(_program(tree))

        returns = report.return_exits()
        self.assertEqual(len(returns), 2)
        tainted = [e for e in returns if e.ever_denied]
        clean = [e for e in returns if not e.ever_denied]
        self.assertEqual(len(tainted), 1)
        self.assertEqual(len(clean), 1)
        self.assertEqual(tainted[0].origin, "shared")
        self.assertEqual(clean[0].origin, "shared")
        self.assertEqual(tainted[0].state.denials, ("check",))
        self.assertEqual(clean[0].state.denials, ())

        # 拒绝入口只存在于其中一条路径，fate 恰好一条。
        self.assertEqual(len(report.fate_of("check")), 1)
        self.assertEqual(report.fate_of("check")[0].kind, FATE_TO_RETURN)

    def test_branch_with_distinct_denial_origins(self):
        tree = Branch(
            condition="flag",
            when_true=Deny("deny-a"),
            when_false=Deny("deny-b"),
            location="branch1",
        )
        report = analyze(_program(tree))

        self.assertEqual(len(report.denial_exits()), 2)
        origins = sorted(e.origin for e in report.denial_exits())
        self.assertEqual(origins, ["deny-a", "deny-b"])
        self.assertEqual(report.denial_origins, ("deny-a", "deny-b"))
        for fate in report.fates:
            self.assertEqual(fate.kind, FATE_PROPAGATES_DENIED)
        self.assertFalse(report.is_safe_to_proceed())

    def test_branch_one_clean_one_error_exit(self):
        tree = Branch(
            condition="flag",
            when_true=Return("ok"),
            when_false=Error("boom"),
            location="branch1",
        )
        report = analyze(_program(tree))
        self.assertEqual(len(report.exits), 2)
        self.assertEqual([e.origin for e in report.clean_returns()], ["ok"])
        self.assertEqual([e.origin for e in report.error_exits()], ["boom"])


class FinallyTests(unittest.TestCase):
    """finally：正常结束保留待传播结果，显式完成则替换。"""

    def test_cleanup_normal_completion_preserves_pending_denial(self):
        tree = Finally(
            body=Deny("check"),
            cleanup=Pass("cleanup-pass"),
            location="fin1",
        )
        report = analyze(_program(tree))

        self.assertEqual(len(report.exits), 1)
        exit_ = report.exits[0]
        self.assertIsInstance(exit_, ThrowExit)
        self.assertEqual(exit_.kind, DENIED)
        self.assertEqual(exit_.origin, "check")
        self.assertEqual(exit_.state.denials, ("check",))
        # 正常结束的 finally 不产生覆盖事件，拒绝命运仍是原样传播。
        self.assertEqual(exit_.state.events, ())
        self.assertEqual(report.fate_of("check")[0].kind, FATE_PROPAGATES_DENIED)

    def test_cleanup_normal_completion_preserves_pending_return(self):
        tree = Finally(
            body=Return("body-return"),
            cleanup=Pass("cleanup-pass"),
            location="fin1",
        )
        report = analyze(_program(tree))
        self.assertEqual(len(report.exits), 1)
        self.assertEqual(report.exits[0].origin, "body-return")
        self.assertIsInstance(report.exits[0], ReturnExit)

    def test_cleanup_explicit_return_overrides_pending_denial(self):
        tree = Finally(
            body=Deny("check"),
            cleanup=Return("cleanup-return"),
            location="fin1",
        )
        report = analyze(_program(tree))

        self.assertEqual(len(report.exits), 1)
        exit_ = report.exits[0]
        self.assertIsInstance(exit_, ReturnExit)
        self.assertEqual(exit_.origin, "cleanup-return")
        self.assertEqual(exit_.state.denials, ("check",))
        event = exit_.state.events[0]
        self.assertIsInstance(event, FinallyOverride)
        self.assertEqual(event.at, "fin1")
        self.assertEqual(event.original, "denied")
        self.assertEqual(event.replacement, "return")
        self.assertEqual(event.denials, ("check",))
        self.assertEqual(report.fate_of("check")[0].kind, FATE_FINALLY_RETURN)
        self.assertTrue(report.is_safe_to_proceed())

    def test_cleanup_explicit_error_overrides_pending_denial(self):
        tree = Finally(
            body=Deny("check"),
            cleanup=Error("cleanup-error"),
            location="fin1",
        )
        report = analyze(_program(tree))

        exit_ = report.exits[0]
        self.assertIsInstance(exit_, ThrowExit)
        self.assertEqual(exit_.kind, ERROR)
        self.assertEqual(exit_.origin, "cleanup-error")
        self.assertEqual(exit_.state.denials, ("check",))
        event = exit_.state.events[0]
        self.assertEqual(event.original, "denied")
        self.assertEqual(event.replacement, "error")
        # 拒绝已被 finally 替换，不得再有 DENIED 出口。
        self.assertEqual(report.denial_exits(), ())
        fate = report.fate_of("check")[0]
        self.assertEqual(fate.kind, FATE_FINALLY_ERROR)
        self.assertEqual(fate.consumed_at, "fin1")

    def test_cleanup_new_denial_overrides_pending_denial(self):
        tree = Finally(
            body=Deny("check1"),
            cleanup=Deny("check2"),
            location="fin1",
        )
        report = analyze(_program(tree))

        exit_ = report.exits[0]
        self.assertEqual(exit_.origin, "check2")
        self.assertEqual(exit_.kind, DENIED)
        self.assertEqual(exit_.state.denials, ("check1", "check2"))
        event = exit_.state.events[0]
        self.assertIsInstance(event, FinallyOverride)
        # 覆盖事件只针对主体的 check1；check2 是替换物自身。
        self.assertEqual(event.denials, ("check1",))
        self.assertEqual(event.replacement, "denied")
        kinds = {f.origin: f.kind for f in report.fates}
        self.assertEqual(kinds["check1"], FATE_FINALLY_DENIED)
        self.assertEqual(kinds["check2"], FATE_PROPAGATES_DENIED)

    def test_cleanup_return_overrides_plain_return_too(self):
        tree = Finally(
            body=Return("body-return"),
            cleanup=Return("cleanup-return"),
            location="fin1",
        )
        report = analyze(_program(tree))
        self.assertEqual(len(report.exits), 1)
        self.assertEqual(report.exits[0].origin, "cleanup-return")
        event = report.exits[0].state.events[0]
        self.assertIsInstance(event, FinallyOverride)
        self.assertEqual(event.original, "return")
        self.assertEqual(event.replacement, "return")

    def test_cleanup_internal_catch_history_survives_normal_finish(self):
        # 清理块内部吞掉了一次拒绝，finally 整体正常结束：
        # 主体返回值保留，清理块里的事件历史也必须保留。
        cleanup = Catch(
            body=Deny("cleanup-check"),
            handler=Pass("cleanup-recover"),
            location="cleanup-try",
        )
        tree = Finally(
            body=Return("body-return"),
            cleanup=cleanup,
            location="fin1",
        )
        report = analyze(_program(tree))

        exit_ = report.exits[0]
        self.assertEqual(exit_.origin, "body-return")
        self.assertEqual(exit_.state.denials, ("cleanup-check",))
        self.assertIsInstance(exit_.state.events[0], Caught)
        self.assertEqual(exit_.state.events[0].at, "cleanup-try")


class CallTests(unittest.TestCase):
    """调用内联与无递归约束。"""

    def _callee_program(self, caller_body):
        # 成功路径用 Pass 落到块尾（隐式返回），用于验证调用内联时
        # “落尾”不会被误当成显式返回而短路外层 Sequence。
        callee = Function(
            "helper",
            Branch(
                condition="ok",
                when_true=Pass("helper-pass"),
                when_false=Deny("helper-deny"),
                location="helper-branch",
            ),
        )
        return Program((Function("main", caller_body), callee), entry="main")

    def test_call_inlines_callee_exits_with_origin_locations(self):
        tree = Sequence([Call("helper", "call-site"), Return("after")], "main")
        report = analyze(self._callee_program(tree))

        # helper 落尾路径继续走到 after；helper 拒绝路径短路。
        origins = sorted(e.origin for e in report.exits)
        self.assertEqual(origins, ["after", "helper-deny"])
        after = next(e for e in report.exits if e.origin == "after")
        self.assertIsInstance(after, ReturnExit)
        self.assertFalse(after.ever_denied)

        denied = report.denial_exits()[0]
        self.assertEqual(denied.origin, "helper-deny")
        self.assertEqual(denied.state.denials, ("helper-deny",))

    def test_explicit_callee_return_short_circuits_caller_sequence(self):
        # 对照边界：被调函数显式 return 必须短路调用点后续语句。
        callee = Function(
            "helper2",
            Branch(
                condition="ok",
                when_true=Return("helper-explicit"),
                when_false=Deny("helper-deny2"),
                location="helper-branch",
            ),
        )
        tree = Sequence([Call("helper2", "call-site"), Return("after")], "main")
        report = analyze(Program((Function("main", tree), callee), entry="main"))
        origins = sorted(e.origin for e in report.exits)
        self.assertEqual(origins, ["helper-deny2", "helper-explicit"])

    def test_outer_catch_consumes_denial_inlined_from_callee(self):
        tree = Catch(
            body=Call("helper", "call-site"),
            handler=Return("caller-fallback"),
            location="caller-try",
        )
        report = analyze(self._callee_program(tree))

        # helper 落尾路径使 caller 也走到块尾（隐式返回）；
        # helper 拒绝路径被外层 catch 吞成显式 fallback 返回。
        return_origins = sorted(e.origin for e in report.return_exits())
        self.assertEqual(
            return_origins, ["caller-fallback", "main:<implicit-return>"]
        )
        tainted = report.tainted_returns()
        self.assertEqual(len(tainted), 1)
        self.assertEqual(tainted[0].origin, "caller-fallback")
        self.assertEqual(tainted[0].state.denials, ("helper-deny",))
        self.assertEqual(
            tainted[0].state.events[0].at, "caller-try"
        )
        self.assertEqual(report.denial_exits(), ())

    def test_direct_recursion_is_rejected(self):
        recursive = Function(
            "loopy",
            Sequence([Call("loopy", "self-call"), Return("ok")], "body"),
        )
        with self.assertRaises(RecursiveCallError) as ctx:
            Analyzer(Program((recursive,), entry="loopy"))
        self.assertIn("loopy", ctx.exception.cycle)

    def test_mutual_recursion_is_rejected(self):
        a = Function("a", Call("b", "call-b"))
        b = Function("b", Call("a", "call-a"))
        with self.assertRaises(RecursiveCallError):
            Analyzer(Program((a, b), entry="a"))

    def test_unknown_callee_is_rejected(self):
        tree = Call("missing", "call-site")
        with self.assertRaises(UnknownFunctionError) as ctx:
            Analyzer(Program((Function("main", tree),), entry="main"))
        self.assertEqual(ctx.exception.name, "missing")
        self.assertEqual(ctx.exception.location, "call-site")

    def test_unknown_entry_is_rejected(self):
        with self.assertRaises(UnknownFunctionError):
            Analyzer(Program((Function("main", Return("ok")),), entry="nope"))


class StructuralValidationTests(unittest.TestCase):
    """输入树本身的合法性校验。"""

    def test_empty_location_rejected(self):
        with self.assertRaises(InvalidNodeError):
            Deny("")

    def test_empty_sequence_rejected(self):
        with self.assertRaises(InvalidNodeError):
            Sequence([], "empty-seq")

    def test_non_node_child_rejected(self):
        with self.assertRaises(InvalidNodeError):
            Sequence([Deny("a"), "not-a-node"], "seq")  # type: ignore[list-item]

    def test_bad_catch_filter_rejected(self):
        with self.assertRaises(InvalidNodeError):
            Catch(Deny("a"), Return("b"), "try", catches=("weird",))

    def test_duplicate_function_name_rejected(self):
        f1 = Function("dup", Return("a"))
        f2 = Function("dup", Return("b"))
        with self.assertRaises(ValueError):
            Analyzer(Program((f1, f2), entry="dup"))


class DeterminismTests(unittest.TestCase):
    """报告稳定性：重复分析结果一致、出口可去重。"""

    def test_repeated_analysis_is_identical(self):
        tree = Branch(
            condition="flag",
            when_true=Catch(Deny("d1"), Return("r1"), "t1"),
            when_false=Branch(
                condition="inner",
                when_true=Deny("d2"),
                when_false=Error("e1"),
                location="b2",
            ),
            location="b1",
        )
        program = _program(tree)
        r1 = analyze(program)
        r2 = Analyzer(program).analyze()
        self.assertEqual(r1.exits, r2.exits)
        self.assertEqual(r1.fates, r2.fates)
        self.assertEqual(r1.denial_origins, r2.denial_origins)


if __name__ == "__main__":
    unittest.main(verbosity=2)
