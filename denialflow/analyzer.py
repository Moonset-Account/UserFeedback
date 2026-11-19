"""抽象执行器与分析报告。

执行语义
========

* 每个节点在给定 :class:`~denialflow.model.State` 下返回一组
  *完成形式*（正常落尾 / 显式返回 / 抛出异常）。分支天然产生多路径，
  集合自动去重。
* ``Call`` 把被调函数在初始污点下的全部出口内联到调用点，并并入
  调用路径已有的污点历史；叶节点 ``origin`` 保持为被调函数内的原位置。
* ``Catch`` 只捕获类别匹配的抛出；捕获时为该路径写入
  :class:`~denialflow.model.Caught` 事件，再在合并后的污点上执行处理块。
* ``Finally`` 的清理块正常落尾时原样保留主体结果；清理块显式
  返回/抛出时替换主体结果并写入
  :class:`~denialflow.model.FinallyOverride` 事件。
* 函数体落到块尾等价于一个位于函数合成位置的正常返回。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RecursiveCallError, UnknownFunctionError
from .model import (
    DENIED,
    ERROR,
    AnyExit,
    AnyNode,
    Branch,
    Call,
    Catch,
    Caught,
    Completion,
    Deny,
    Error,
    Finally,
    FinallyOverride,
    Function,
    Pass,
    Program,
    Return,
    ReturnExit,
    Sequence,
    State,
    ThrowExit,
    _Normal,
)

# 拒绝入口最终命运的分类标签。
FATE_PROPAGATES_DENIED = "propagates_denied"        # 拒绝原样传播到函数外
FATE_TO_RETURN = "converted_to_return"              # 被吞掉/改写为正常返回
FATE_TO_ERROR = "replaced_with_error"               # 被替换为普通错误
FATE_TO_DENIED = "replaced_with_denial"             # 被替换为另一次拒绝
FATE_FINALLY_RETURN = "finally_overrides_return"    # finally 显式返回替换
FATE_FINALLY_ERROR = "finally_overrides_error"      # finally 显式抛普通错误
FATE_FINALLY_DENIED = "finally_overrides_denial"    # finally 显式抛新拒绝


@dataclass(frozen=True)
class Fate:
    """单个权限拒绝入口在单条路径上的最终命运。"""

    origin: str
    """拒绝入口的叶节点位置。"""

    kind: str
    """命运分类标签（模块级 ``FATE_*`` 常量）。"""

    exit: AnyExit
    """该路径实际到达的函数出口。"""

    consumed_at: str | None
    """第一个消费/改写它的节点位置；未被消费时为 ``None``。"""

    events: tuple
    """该路径上与该入口相关的完整事件链。"""

    @property
    def was_consumed(self) -> bool:
        return self.consumed_at is not None


@dataclass(frozen=True)
class Report:
    """入口函数的完整分析结果。"""

    entry: str
    exits: tuple[AnyExit, ...]
    fates: tuple[Fate, ...]
    denial_origins: tuple[str, ...]

    # -- 出口视角 --------------------------------------------------------

    def return_exits(self) -> tuple[ReturnExit, ...]:
        """所有正常返回出口（含“曾经拒绝但已被改成返回”的出口）。"""
        return tuple(e for e in self.exits if isinstance(e, ReturnExit))

    def throwing_exits(self) -> tuple[ThrowExit, ...]:
        """所有向外抛异常的出口。"""
        return tuple(e for e in self.exits if isinstance(e, ThrowExit))

    def denial_exits(self) -> tuple[ThrowExit, ...]:
        """仍以权限拒绝形式向外传播的出口。"""
        return tuple(
            e for e in self.exits if isinstance(e, ThrowExit) and e.kind == DENIED
        )

    def error_exits(self) -> tuple[ThrowExit, ...]:
        """以普通错误形式向外传播的出口。"""
        return tuple(
            e for e in self.exits if isinstance(e, ThrowExit) and e.kind == ERROR
        )

    def tainted_returns(self) -> tuple[ReturnExit, ...]:
        """*曾经拒绝*但最终正常返回的出口（catch 吞改的典型信号）。"""
        return tuple(
            e for e in self.exits if isinstance(e, ReturnExit) and e.ever_denied
        )

    def clean_returns(self) -> tuple[ReturnExit, ...]:
        """路径上从未出现过权限拒绝的正常返回出口。"""
        return tuple(
            e for e in self.exits if isinstance(e, ReturnExit) and not e.ever_denied
        )

    # -- 拒绝入口视角 ----------------------------------------------------

    def fate_of(self, origin: str) -> tuple[Fate, ...]:
        """某个拒绝入口在所有可达路径上的命运。"""
        return tuple(f for f in self.fates if f.origin == origin)

    def consumed_origins(self) -> tuple[str, ...]:
        """至少在一条路径上被消费/替换过的拒绝入口。"""
        return tuple(
            origin
            for origin in self.denial_origins
            if any(f.was_consumed for f in self.fate_of(origin))
        )

    def unconsumed_origins(self) -> tuple[str, ...]:
        """在所有可达路径上都原样传播、从未被消费的拒绝入口。"""
        return tuple(
            origin
            for origin in self.denial_origins
            if all(not f.was_consumed for f in self.fate_of(origin))
        )

    def is_safe_to_proceed(self) -> bool:
        """外层调用者是否可以安全执行受保护操作。

        仅当不存在任何仍向外传播的权限拒绝出口时为 ``True``。
        “曾经拒绝但改成返回”的出口语义上已经是正常返回，是否继续
        业务动作应由调用策略决定，这里只保证没有拒绝会漏出。
        """
        return not self.denial_exits()


class Analyzer:
    """对无递归函数森林做抽象执行。"""

    def __init__(self, program: Program) -> None:
        if not isinstance(program, Program):
            raise TypeError("program 必须是 Program 实例")
        self._program = program
        self._functions: dict[str, Function] = {}
        for func in program.functions:
            if func.name in self._functions:
                raise ValueError(f"函数名重复定义: {func.name!r}")
            self._functions[func.name] = func
        if program.entry not in self._functions:
            raise UnknownFunctionError(program.entry)
        self._validate_call_graph()
        self._func_cache: dict[str, tuple[AnyExit, ...]] = {}

    # ------------------------------------------------------------------
    # 调用图校验：未知名调用 + 任何形式的递归
    # ------------------------------------------------------------------

    def _validate_call_graph(self) -> None:
        visiting: list[str] = []
        visited: set[str] = set()

        def walk(name: str) -> None:
            if name in visiting:
                cycle = tuple(visiting[visiting.index(name):]) + (name,)
                raise RecursiveCallError(cycle)
            if name in visited:
                return
            visiting.append(name)
            try:
                for node in _iter_nodes(self._functions[name].body):
                    if isinstance(node, Call):
                        if node.name not in self._functions:
                            raise UnknownFunctionError(node.name, node.location)
                        walk(node.name)
            finally:
                visiting.pop()
            visited.add(name)

        walk(self._program.entry)

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------

    def analyze(self) -> Report:
        exits = self._function_exits(self._program.entry)
        denial_origins = _sorted_unique(
            denial
            for exit_ in exits
            for denial in exit_.state.denials
        )
        fates: list[Fate] = []
        for exit_ in exits:
            for origin in exit_.state.denials:
                fates.append(self._classify(origin, exit_))
        fates.sort(key=lambda f: (f.origin, f.kind, f.exit.origin))
        return Report(
            entry=self._program.entry,
            exits=tuple(_sort_exits(exits)),
            fates=tuple(fates),
            denial_origins=tuple(denial_origins),
        )

    # ------------------------------------------------------------------
    # 函数级出口（带缓存；无递归时缓存安全）
    # ------------------------------------------------------------------

    def _function_exits(self, name: str) -> tuple[AnyExit, ...]:
        if name in self._func_cache:
            return self._func_cache[name]
        func = self._functions[name]
        completions = self._exec(func.body, State.initial())
        exits: list[AnyExit] = []
        fallback_origin = f"{name}:<implicit-return>"
        for comp in completions:
            if isinstance(comp, _Normal):
                exits.append(ReturnExit(origin=fallback_origin, state=comp.state))
            else:
                exits.append(comp)
        result = tuple(_dedupe_exits(exits))
        self._func_cache[name] = result
        return result

    def _callee_completions(self, name: str) -> tuple[Completion, ...]:
        """被调函数体的*原始完成形式*（不落隐式返回）。

        调用内联时，被调函数“落到块尾”应表现为调用点的正常落尾，
        使外层 Sequence 能继续执行后续语句；只有被调函数显式
        ``return`` / 抛出才在调用点短路。
        """
        func = self._functions[name]
        return tuple(self._exec(func.body, State.initial()))

    # ------------------------------------------------------------------
    # 节点抽象执行
    # ------------------------------------------------------------------

    def _exec(self, node: AnyNode, state: State) -> set[Completion]:
        if isinstance(node, Return):
            return {ReturnExit(origin=node.location, state=state)}
        if isinstance(node, Pass):
            return {_Normal(state=state)}
        if isinstance(node, Deny):
            return {
                ThrowExit(
                    origin=node.location,
                    state=state.with_denial(node.location),
                    kind=DENIED,
                )
            }
        if isinstance(node, Error):
            return {ThrowExit(origin=node.location, state=state, kind=ERROR)}
        if isinstance(node, Sequence):
            return self._exec_sequence(node, state)
        if isinstance(node, Branch):
            return self._exec(node.when_true, state) | self._exec(
                node.when_false, state
            )
        if isinstance(node, Call):
            return self._exec_call(node, state)
        if isinstance(node, Catch):
            return self._exec_catch(node, state)
        if isinstance(node, Finally):
            return self._exec_finally(node, state)
        raise TypeError(f"不支持的节点类型: {type(node).__name__}")

    def _exec_sequence(self, node: Sequence, state: State) -> set[Completion]:
        current: set[Completion] = {_Normal(state=state)}
        for stmt in node.statements:
            nxt: set[Completion] = set()
            for comp in current:
                # 已完成（返回/抛出）的路径直接短路，不再执行后续语句。
                if not isinstance(comp, _Normal):
                    nxt.add(comp)
                    continue
                nxt |= self._exec(stmt, comp.state)
            current = nxt
        return current

    def _exec_call(self, node: Call, state: State) -> set[Completion]:
        results: set[Completion] = set()
        for callee_comp in self._callee_completions(node.name):
            # 内联被调函数的完成形式：保留其叶节点 origin 与事件，
            # 再叠加调用路径自身的污点历史。
            merged = state.merge_denials(callee_comp.state.denials)
            for event in callee_comp.state.events:
                merged = merged.with_event(event)
            results.add(_with_state(callee_comp, merged))
        return results

    def _exec_catch(self, node: Catch, state: State) -> set[Completion]:
        results: set[Completion] = set()
        for comp in self._exec(node.body, state):
            if not isinstance(comp, ThrowExit) or comp.kind not in node.catches:
                # 正常完成，或异常类别不匹配：原样向外传播，不消费。
                results.add(comp)
                continue

            # 被抛出路径上携带的全部拒绝入口，是本次捕获涉及的对象。
            consumed = tuple(comp.state.denials)
            handler_results = self._exec(node.handler, comp.state)
            for handler_comp in handler_results:
                handler_state = self._attach_catch_event(
                    handler_comp, node.location, consumed
                )
                results.add(_with_state(handler_comp, handler_state))
        return results

    def _attach_catch_event(
        self,
        comp: Completion,
        catch_location: str,
        consumed: tuple[str, ...],
    ) -> State:
        base = comp.state
        if not consumed:
            # 捕获的是纯普通错误、路径上没有任何拒绝历史：
            # 不产生与权限拒绝相关的事件。
            return base
        replacement = _completion_replacement(comp)
        return base.with_event(
            Caught(
                at=catch_location,
                denials=consumed,
                replacement=replacement,
            )
        )

    def _exec_finally(self, node: Finally, state: State) -> set[Completion]:
        results: set[Completion] = set()
        for pending in self._exec(node.body, state):
            for cleanup in self._exec(node.cleanup, _state_of(pending)):
                results.add(self._combine_finally(node, pending, cleanup))
        return results

    def _combine_finally(
        self, node: Finally, pending: Completion, cleanup: Completion
    ) -> Completion:
        if isinstance(cleanup, _Normal):
            # finally 正常结束：保留待传播结果，并入清理块新增的
            # 拒绝入口与事件历史（清理块内部可能也吞过拒绝）。
            merged = _state_of(pending).merge_denials(cleanup.state.denials)
            for event in cleanup.state.events:
                merged = merged.with_event(event)
            return _with_state(pending, merged)

        # 清理块显式返回/抛出：替换待传播结果。事件只记录主体路径上
        # 被覆盖的拒绝入口；清理块自己新抛的拒绝属于替换物，不算被消费。
        original = _completion_replacement(pending)
        replacement = _completion_replacement(cleanup)
        overridden = tuple(_state_of(pending).denials)
        new_state = cleanup.state.with_event(
            FinallyOverride(
                at=node.location,
                denials=overridden,
                replacement=replacement,
                original=original,
            )
        )
        return _with_state(cleanup, new_state)

    # ------------------------------------------------------------------
    # 拒绝命运分类
    # ------------------------------------------------------------------

    def _classify(self, origin: str, exit_: AnyExit) -> Fate:
        related_events = tuple(
            event for event in exit_.state.events if origin in event.denials
        )
        consumed_at = related_events[0].at if related_events else None
        last_event = related_events[-1] if related_events else None

        if isinstance(exit_, ReturnExit):
            if isinstance(last_event, FinallyOverride):
                kind = FATE_FINALLY_RETURN
            else:
                kind = FATE_TO_RETURN
        elif isinstance(exit_, ThrowExit) and exit_.kind == DENIED:
            if isinstance(last_event, FinallyOverride):
                kind = FATE_FINALLY_DENIED
            elif isinstance(last_event, Caught):
                kind = FATE_TO_DENIED
            else:
                kind = FATE_PROPAGATES_DENIED
        else:  # ThrowExit, kind == ERROR
            if isinstance(last_event, FinallyOverride):
                kind = FATE_FINALLY_ERROR
            else:
                kind = FATE_TO_ERROR

        return Fate(
            origin=origin,
            kind=kind,
            exit=exit_,
            consumed_at=consumed_at,
            events=related_events,
        )


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _iter_nodes(node: AnyNode):
    """深度优先遍历结构树（含节点自身）。"""
    yield node
    for child in node.children():
        yield from _iter_nodes(child)


def _state_of(comp: Completion) -> State:
    return comp.state


def _with_state(comp: Completion, state: State) -> Completion:
    if isinstance(comp, _Normal):
        return _Normal(state=state)
    if isinstance(comp, ReturnExit):
        return ReturnExit(origin=comp.origin, state=state)
    return ThrowExit(origin=comp.origin, state=state, kind=comp.kind)


def _completion_replacement(comp: Completion) -> str:
    if isinstance(comp, (_Normal, ReturnExit)):
        return "return"
    return DENIED if comp.kind == DENIED else ERROR


def _sorted_unique(items) -> list[str]:
    return sorted(set(items))


def _dedupe_exits(exits: list[AnyExit]) -> list[AnyExit]:
    seen: set[AnyExit] = set()
    unique: list[AnyExit] = []
    for exit_ in exits:
        if exit_ not in seen:
            seen.add(exit_)
            unique.append(exit_)
    return unique


def _sort_exits(exits) -> list[AnyExit]:
    def key(exit_: AnyExit):
        kind = 0 if isinstance(exit_, ReturnExit) else 1
        throw_kind = getattr(exit_, "kind", "")
        return (kind, throw_kind, exit_.origin, exit_.state.denials)

    return sorted(exits, key=key)


def analyze(program: Program) -> Report:
    """便捷入口：构建分析器并立即返回报告。"""
    return Analyzer(program).analyze()
