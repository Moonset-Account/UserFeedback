"""信息流安全解释器运行时。

安全模型
--------
* 两级标签格 ``PUBLIC <= SECRET``，``join`` 为最小上界。
* 每个求值在一个 *程序计数器标签* ``pc`` 下进行；在秘密条件分支内 ``pc=SECRET``，
  分支内产生的一切结果都被抬升为 SECRET。
* ``Let`` 绑定与 ``Pair`` 字段都是 **惰性 thunk**：
    - thunk 记录创建点的 ``pc``，仅在首次读取时求值一次并记忆化结果；
    - 读取时的有效标签 = ``join(结果固有标签, 读取点 pc)``，
      因此同一个共享结果在不同控制上下文下呈现不同标签，且不会把一次高上下文
      读取“粘”到后续低上下文读取上（无标签蠕变）。
* 未取用的 ``If`` 分支、未读取的配对字段完全不求值，因此不会污染另一字段。
* 异常同样携带标签；``Try`` 只能捕获当前 ``pc`` 可见的异常，不可见异常继续上浮。
* ``Label`` 只允许升级；任何降级尝试立即抛出 ``DowngradeViolation``，
  并记录肇事 AST 节点 ID，``Try`` 无法捕获监控违规。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .ast import (
    BinOp,
    Fst,
    If,
    Input,
    Label,
    Let,
    Lit,
    Not,
    Pair,
    Snd,
    Throw,
    Try,
    Var,
    Expr,
)
from .labels import Level, join

#: 观察到成对值时返回的占位标记：观察者只能逐字段请求，不能整体取走。
PAIR = object()


class LabeledException(Exception):
    """语言级异常，携带安全标签与抛出节点 ID。"""

    def __init__(self, message: str, level: Level, origin: int | None, tag: str = "error"):
        super().__init__(message)
        self.message = message
        self.level = level
        self.origin = origin
        self.tag = tag

    def snapshot(self) -> dict:
        return {
            "kind": "labeled_exception",
            "message": self.message,
            "level": self.level,
            "origin": self.origin,
            "tag": self.tag,
        }


class DowngradeViolation(RuntimeError):
    """非法降级：试图把高标签值重标为低标签。监控级错误，Try 不可捕获。"""

    def __init__(self, node_id: int, from_level: Level, to_level: Level, pc: Level):
        super().__init__(
            f"节点 {node_id} 非法降级: {from_level.name_label} -> {to_level.name_label}"
            f"（pc={pc.name_label}）"
        )
        self.node_id = node_id
        self.from_level = from_level
        self.to_level = to_level
        self.pc = pc

    def snapshot(self) -> dict:
        return {
            "kind": "downgrade_violation",
            "node_id": self.node_id,
            "from_level": self.from_level,
            "to_level": self.to_level,
            "pc": self.pc,
        }


class ObservationPathError(RuntimeError):
    """观察路径非法（路径越界或中途遇到非成对值）。"""


@dataclass(frozen=True)
class Labeled:
    """带标签的求值结果（内部表示）。"""

    value: Any
    level: Level


@dataclass(frozen=True)
class Observation:
    """分字段观察接口的单次应答。

    kind 取值：
      * ``"value"``  —— 许可内可见值（成对值以 ``PAIR`` 占位，必须继续逐字段请求）；
      * ``"error"``  —— 许可内可见的语言级异常；
      * ``"hidden"`` —— 超出许可，值与错误对外不可区分，统一返回隐藏应答。
    """

    kind: str
    value: Any = None
    level: Level | None = None
    origin: int | None = None

    @classmethod
    def visible(cls, value: Any, level: Level) -> "Observation":
        return cls("value", value, level)

    @classmethod
    def hidden(cls) -> "Observation":
        return cls("hidden")

    @classmethod
    def error(cls, message: str, level: Level, origin: int | None) -> "Observation":
        return cls("error", message, level, origin)

    @property
    def is_hidden(self) -> bool:
        return self.kind == "hidden"


class Thunk:
    """惰性记忆化计算。

    异常不记忆化：若首次求值抛出 ``LabeledException``，thunk 保持未完成状态，
    下次读取会重新求值（每次 miss 都计入统计）。
    """

    def __init__(self, runtime: "Runtime", expr: Expr, env: Mapping[str, "Thunk"],
                 creation_pc: Level, immediate: Labeled | None = None):
        self.runtime = runtime
        self.expr = expr
        self.env = dict(env)
        self.creation_pc = creation_pc
        self._done = False
        self._value: Any = None
        self._level: Level | None = None
        self.hits = 0
        self.misses = 0
        if immediate is not None:
            self._done = True
            self._value = immediate.value
            self._level = immediate.level

    @classmethod
    def immediate(cls, runtime: "Runtime", labeled: Labeled) -> "Thunk":
        dummy = Lit(labeled.value)
        return cls(runtime, dummy, {}, labeled.level, immediate=labeled)

    @property
    def memoized(self) -> bool:
        return self._done

    def force(self) -> Labeled:
        """在创建点 pc 下求值，返回结果的 *固有标签*。"""
        if self._done:
            self.hits += 1
            return Labeled(self._value, self._level)  # type: ignore[arg-type]
        self.misses += 1
        labeled = self.runtime.eval(self.expr, self.env, self.creation_pc)
        self._done = True
        self._value = labeled.value
        self._level = labeled.level
        return labeled

    def read(self, pc: Level) -> Labeled:
        """读取并叠加访问者所在控制上下文的 pc 标签。"""
        labeled = self.force()
        return Labeled(labeled.value, join(labeled.level, pc))


@dataclass
class PairValue:
    """成对值的内部表示：两个字段各自是独立的惰性 thunk。"""

    first: Thunk
    second: Thunk
    node_id: int


class Runtime:
    """解释器运行时：保存输入登记、求值统计与违规记录。"""

    def __init__(self, root: Expr, inputs: Mapping[str, tuple[Any, Level]] | None = None):
        self.root = root
        self.inputs: dict[str, tuple[Any, Level]] = {}
        for name, entry in (inputs or {}).items():
            value, level = entry
            if not isinstance(level, Level):
                raise TypeError(f"输入 {name!r} 的级别必须是 Level 成员")
            self.inputs[name] = (value, level)
        self.eval_count: dict[int, int] = {}
        self.violations_log: list[dict] = []
        self.thunks: list[Thunk] = []
        # 根表达式本身也是一个在公开 pc 下创建的惰性 thunk
        self._root_thunk = Thunk(self, root, {}, Level.PUBLIC)
        self.thunks.append(self._root_thunk)

    # ------------------------------------------------------------------ 求值

    def eval(self, expr: Expr, env: Mapping[str, Thunk], pc: Level) -> Labeled:
        self.eval_count[expr.node_id] = self.eval_count.get(expr.node_id, 0) + 1
        handler = _DISPATCH[type(expr)]
        return handler(self, expr, env, pc)

    def _eval_lit(self, expr: Lit, env, pc) -> Labeled:
        return Labeled(expr.value, pc)

    def _eval_input(self, expr: Input, env, pc) -> Labeled:
        entry = self.inputs.get(expr.name)
        if entry is None:
            raise LabeledException(f"缺失输入: {expr.name}", pc, expr.node_id)
        value, level = entry
        return Labeled(value, join(level, pc))

    def _eval_var(self, expr: Var, env, pc) -> Labeled:
        thunk = env.get(expr.name)
        if thunk is None:
            raise LabeledException(f"未绑定的变量: {expr.name}", pc, expr.node_id)
        return thunk.read(pc)

    def _eval_let(self, expr: Let, env, pc) -> Labeled:
        thunk = Thunk(self, expr.bound, env, pc)
        self.thunks.append(thunk)
        new_env = dict(env)
        new_env[expr.name] = thunk
        return self.eval(expr.body, new_env, pc)

    def _eval_label(self, expr: Label, env, pc) -> Labeled:
        inner = self.eval(expr.expr, env, pc)
        if expr.level < inner.level:
            violation = DowngradeViolation(
                node_id=expr.node_id,
                from_level=inner.level,
                to_level=expr.level,
                pc=pc,
            )
            self.violations_log.append(violation.snapshot())
            raise violation
        # 升级（或同级重标）：取两者上界
        return Labeled(inner.value, join(inner.level, expr.level))

    def _eval_if(self, expr: If, env, pc) -> Labeled:
        try:
            cond = self.eval(expr.cond, env, pc)
        except LabeledException as exc:
            raise LabeledException(
                exc.message, join(exc.level, pc), exc.origin, exc.tag
            ) from None
        if not isinstance(cond.value, bool):
            raise LabeledException(
                "if 条件必须是布尔值", join(cond.level, pc), expr.node_id
            )
        branch_pc = join(pc, cond.level)
        chosen = expr.then if cond.value else expr.else_
        # 未取用分支在此完全不可达，不会产生任何求值或标签污染
        return self.eval(chosen, env, branch_pc)

    def _eval_pair(self, expr: Pair, env, pc) -> Labeled:
        first_thunk = Thunk(self, expr.first, env, pc)
        second_thunk = Thunk(self, expr.second, env, pc)
        self.thunks.extend((first_thunk, second_thunk))
        # 成对值本身的存在性标签为 pc；字段标签在各自被读取时计算
        return Labeled(PairValue(first_thunk, second_thunk, expr.node_id), pc)

    def _eval_fst(self, expr: Fst, env, pc) -> Labeled:
        return self._eval_projection(expr, expr.pair, env, pc, select_first=True)

    def _eval_snd(self, expr: Snd, env, pc) -> Labeled:
        return self._eval_projection(expr, expr.pair, env, pc, select_first=False)

    def _eval_projection(self, expr: Expr, pair_expr: Expr, env, pc,
                         select_first: bool) -> Labeled:
        pair = self.eval(pair_expr, env, pc)
        if not isinstance(pair.value, PairValue):
            raise LabeledException(
                "投影作用于非成对值", join(pair.level, pc), expr.node_id
            )
        field_pc = join(pair.level, pc)
        thunk = pair.value.first if select_first else pair.value.second
        return thunk.read(field_pc)

    def _eval_throw(self, expr: Throw, env, pc) -> Labeled:
        payload = self.eval(expr.expr, env, pc)
        raise LabeledException(
            str(payload.value), join(payload.level, pc), expr.node_id, expr.tag
        )

    def _eval_try(self, expr: Try, env, pc) -> Labeled:
        try:
            return self.eval(expr.body, env, pc)
        except LabeledException as exc:
            exc_level = join(exc.level, pc)
            if exc_level > pc:
                # 异常对当前控制上下文不可见：不能捕获，继续带标签上浮
                raise LabeledException(
                    exc.message, exc_level, exc.origin, exc.tag
                ) from None
            # 可见异常：处理表达式可通过保留变量 _exn 读取异常载荷
            handler_env = dict(env)
            handler_env["_exn"] = Thunk.immediate(
                self, Labeled(exc.message, exc_level)
            )
            return self.eval(expr.handler, handler_env, pc)

    def _eval_not(self, expr: Not, env, pc) -> Labeled:
        inner = self.eval(expr.expr, env, pc)
        if not isinstance(inner.value, bool):
            raise LabeledException(
                "Not 作用于非布尔值", join(inner.level, pc), expr.node_id
            )
        return Labeled(not inner.value, join(inner.level, pc))

    def _eval_binop(self, expr: BinOp, env, pc) -> Labeled:
        lhs = self.eval(expr.lhs, env, pc)
        rhs = self.eval(expr.rhs, env, pc)
        level = join(join(lhs.level, rhs.level), pc)
        op = expr.op
        try:
            if op == "+":
                result = lhs.value + rhs.value
            elif op == "-":
                result = lhs.value - rhs.value
            elif op == "*":
                result = lhs.value * rhs.value
            elif op == "//":
                result = lhs.value // rhs.value
            elif op == "%":
                result = lhs.value % rhs.value
            elif op == "==":
                result = lhs.value == rhs.value
            elif op == "!=":
                result = lhs.value != rhs.value
            elif op == "<":
                result = lhs.value < rhs.value
            elif op == "<=":
                result = lhs.value <= rhs.value
            elif op == ">":
                result = lhs.value > rhs.value
            elif op == ">=":
                result = lhs.value >= rhs.value
            elif op == "and":
                self._require_bool(op, lhs, rhs, expr.node_id, level)
                result = lhs.value and rhs.value
            elif op == "or":
                self._require_bool(op, lhs, rhs, expr.node_id, level)
                result = lhs.value or rhs.value
            else:  # pragma: no cover - 构造期已校验
                raise LabeledException(f"未知运算符 {op!r}", level, expr.node_id)
        except LabeledException:
            raise
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            raise LabeledException(
                f"运算错误 ({op}): {exc}", level, expr.node_id
            ) from None
        return Labeled(result, level)

    @staticmethod
    def _require_bool(op, lhs, rhs, node_id, level) -> None:
        if not isinstance(lhs.value, bool) or not isinstance(rhs.value, bool):
            raise LabeledException(
                f"运算符 {op!r} 要求布尔操作数", level, node_id
            )

    # ------------------------------------------------------------- 公开观察

    def observe(self, path: Sequence[int] = (),
                clearance: Level = Level.PUBLIC) -> Observation:
        """分字段公开观察接口。

        ``path`` 是字段下标序列（0=first，1=second），沿配对结构逐层下降。
        观察者只能看到 ``level <= clearance`` 的值或错误；超出许可一律返回
        ``Observation.hidden()``，值隐藏与错误隐藏对外不可区分。
        非法路径（越界 / 中途遇到非成对值）抛出 ``ObservationPathError``；
        监控级违规 ``DowngradeViolation`` 不在本接口吞掉，直接上浮。
        """
        thunk = self._root_thunk
        current: Labeled | None = None
        for index in path:
            if index not in (0, 1):
                raise ObservationPathError(f"非法字段下标: {index!r}")
            try:
                current = thunk.read(clearance)
            except LabeledException as exc:
                level = join(exc.level, clearance)
                if level <= clearance:
                    return Observation.error(exc.message, level, exc.origin)
                return Observation.hidden()
            # 隐藏的配对不能继续下降，否则结构本身会成为侧信道
            if current.level > clearance:
                return Observation.hidden()
            if not isinstance(current.value, PairValue):
                raise ObservationPathError("观察路径中途遇到非成对值")
            thunk = current.value.first if index == 0 else current.value.second

        try:
            leaf = thunk.read(clearance)
        except LabeledException as exc:
            level = join(exc.level, clearance)
            if level <= clearance:
                return Observation.error(exc.message, level, exc.origin)
            return Observation.hidden()
        # 值与错误统一受许可约束：超出许可一律 hidden，二者对外不可区分
        if leaf.level > clearance:
            return Observation.hidden()
        if isinstance(leaf.value, PairValue):
            # 成对值不能整体取走，只暴露占位标记，观察者必须继续逐字段请求
            return Observation.visible(PAIR, leaf.level)
        return Observation.visible(leaf.value, leaf.level)

    # ---------------------------------------------------------------- 统计

    def violation_traces(self) -> list[dict]:
        """返回已发生的非法降级记录（含节点 ID），供审计定位。"""
        return [dict(item) for item in self.violations_log]

    def thunk_stats(self) -> list[dict]:
        """返回全部 thunk 的命中/未命中统计，用于验证惰性与共享。"""
        return [
            {
                "node_id": thunk.expr.node_id,
                "creation_pc": thunk.creation_pc,
                "hits": thunk.hits,
                "misses": thunk.misses,
                "memoized": thunk.memoized,
            }
            for thunk in self.thunks
        ]

    def node_eval_count(self, node_id: int) -> int:
        return self.eval_count.get(node_id, 0)


_DISPATCH = {
    Lit: Runtime._eval_lit,
    Input: Runtime._eval_input,
    Var: Runtime._eval_var,
    Let: Runtime._eval_let,
    Label: Runtime._eval_label,
    If: Runtime._eval_if,
    Pair: Runtime._eval_pair,
    Fst: Runtime._eval_fst,
    Snd: Runtime._eval_snd,
    Throw: Runtime._eval_throw,
    Try: Runtime._eval_try,
    BinOp: Runtime._eval_binop,
    Not: Runtime._eval_not,
}
