"""受限表达式语言的 AST 定义。

语法（文档化用途）::

    e ::= Lit(v)                      常量（int / bool / str）
        | Input(name)                 运行时秘密/公开输入，级别在 Runtime 中登记
        | Var(name)                   变量读取
        | Let(name, e1, e2)           惰性绑定：e1 仅在 name 首次被读取时求值
        | Label(e, level)             显式重标；只允许升级，降级触发 DowngradeViolation
        | If(cond, then, else_)       条件；未取用分支完全不求值
        | Pair(e1, e2)                成对构造；两个字段分别惰性记忆化
        | Fst(e) / Snd(e)             取字段（触发该字段的 thunk）
        | Throw(e, node_tag?)         抛出带标签异常
        | Try(body, handler)          捕获 *同标签可见* 的语言级异常
        | BinOp(op, lhs, rhs)         受限二元运算
        | Not(e)                      布尔取反

每个节点构造时自动获得进程内唯一整数 id，供非法降级定位使用。
所有节点使用 keyword-only 字段，保证继承链上默认/非默认字段可以自由组合。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count

_id_counter = count(1)


def reset_node_ids() -> None:
    """重置节点 ID 计数器（主要供测试使用）。"""
    global _id_counter
    _id_counter = count(1)


@dataclass(frozen=True)
class Node:
    """所有 AST 节点的基类，携带唯一节点 ID。"""

    node_id: int = field(
        default_factory=lambda: next(_id_counter), compare=False, kw_only=True
    )


@dataclass(frozen=True)
class Expr(Node):
    """表达式基类。"""


@dataclass(frozen=True)
class Lit(Expr):
    value: object


@dataclass(frozen=True)
class Input(Expr):
    name: str


@dataclass(frozen=True)
class Var(Expr):
    name: str


@dataclass(frozen=True)
class Let(Expr):
    name: str
    bound: Expr
    body: Expr


@dataclass(frozen=True)
class Label(Expr):
    expr: Expr
    level: object  # labels.Level，避免循环导入用 object 标注

    def __post_init__(self) -> None:
        # 提前校验枚举类型，错误在构造期暴露
        from .labels import Level as _Level

        if not isinstance(self.level, _Level):
            raise TypeError("Label.level 必须是 secure_expr.Level 成员")


@dataclass(frozen=True)
class If(Expr):
    cond: Expr
    then: Expr
    else_: Expr


@dataclass(frozen=True)
class Pair(Expr):
    first: Expr
    second: Expr


@dataclass(frozen=True)
class Fst(Expr):
    pair: Expr


@dataclass(frozen=True)
class Snd(Expr):
    pair: Expr


@dataclass(frozen=True)
class Throw(Expr):
    expr: Expr
    tag: str = "error"


@dataclass(frozen=True)
class Try(Expr):
    body: Expr
    handler: Expr


# 支持的二元运算符：纯函数，不产生副作用
BIN_OPS = frozenset(
    {"+", "-", "*", "//", "%", "==", "!=", "<", "<=", ">", ">=", "and", "or"}
)


@dataclass(frozen=True)
class BinOp(Expr):
    lhs: Expr
    rhs: Expr
    op: str

    def __post_init__(self) -> None:
        if self.op not in BIN_OPS:
            raise ValueError(f"不支持的二元运算符: {self.op!r}")


@dataclass(frozen=True)
class Not(Expr):
    expr: Expr
