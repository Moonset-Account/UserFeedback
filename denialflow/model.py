"""结构化函数树与抽象执行的核心数据模型。

节点类别覆盖题目要求的五种结构 + 三种叶节点：

结构节点
--------
* :class:`Sequence`  顺序
* :class:`Branch`    分支
* :class:`Call`      调用（内联被调函数的所有可能出口）
* :class:`Catch`     捕获（try/catch，可按异常类别过滤）
* :class:`Finally`   捕获 finally（try/finally）

叶节点
------
* :class:`Return`  正常返回
* :class:`Deny`    权限检查拒绝（专用异常）
* :class:`Error`   普通错误
* :class:`Pass`    正常落到块尾（finally 正常结束等场景需要）

污点模型
--------
:class:`State` 沿每条抽象执行路径携带：

* ``denials``：该路径上*曾经出现过*的所有权限拒绝入口（叶节点位置），
  即使后来被 catch 吞掉或替换，记录仍然保留，用于合流后区分
  “曾经拒绝”与“从未拒绝”；
* ``events``：拒绝在每一层被消费 / 替换 / 改写的完整历史，
  每条事件记录发生位置和涉及的拒绝入口。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from .errors import InvalidNodeError

# ---------------------------------------------------------------------------
# 异常类别标签：DENIED 是题目中的“专用权限异常”，ERROR 是普通错误。
# ---------------------------------------------------------------------------

DENIED = "denied"
ERROR = "error"
EXCEPTION_KINDS = frozenset({DENIED, ERROR})


def _check_location(node_name: str, location: object) -> str:
    if not isinstance(location, str) or not location:
        raise InvalidNodeError(f"{node_name} 节点的 location 必须是非空字符串")
    return location


# ---------------------------------------------------------------------------
# 结构树节点
# ---------------------------------------------------------------------------


class Node:
    """节点公共基类，每个节点带一个用于结果溯源的位置标签。"""

    location: str

    def children(self) -> tuple["Node", ...]:
        """直接子节点（用于递归校验与遍历）。"""
        return ()


@dataclass(frozen=True)
class Return(Node):
    """正常返回叶节点。"""

    location: str

    def __post_init__(self) -> None:
        _check_location("Return", self.location)


@dataclass(frozen=True)
class Pass(Node):
    """正常落到块尾的叶节点（不产生返回值，仅表示正常完成）。"""

    location: str

    def __post_init__(self) -> None:
        _check_location("Pass", self.location)


@dataclass(frozen=True)
class Deny(Node):
    """权限检查拒绝叶节点，抛出专用权限异常。"""

    location: str

    def __post_init__(self) -> None:
        _check_location("Deny", self.location)


@dataclass(frozen=True)
class Error(Node):
    """普通错误叶节点，抛出非权限类异常。"""

    location: str

    def __post_init__(self) -> None:
        _check_location("Error", self.location)


@dataclass(frozen=True)
class Sequence(Node):
    """顺序节点：语句依次执行，前一句不完成则短路。"""

    statements: tuple[Node, ...]
    location: str

    def __init__(self, statements: tuple[Node, ...] | list[Node], location: str) -> None:
        object.__setattr__(self, "statements", tuple(statements))
        object.__setattr__(self, "location", location)
        self.__post_init__()

    def __post_init__(self) -> None:
        _check_location("Sequence", self.location)
        if not self.statements:
            raise InvalidNodeError("Sequence 至少包含一条语句", self.location)
        for stmt in self.statements:
            if not isinstance(stmt, Node):
                raise InvalidNodeError("Sequence 的元素必须是 Node", self.location)

    def children(self) -> tuple[Node, ...]:
        return self.statements


@dataclass(frozen=True)
class Branch(Node):
    """分支节点。

    ``when_true`` / ``when_false`` 均为必填结构，抽象执行把两条路径的
    出口做并集；合流后的出口仍各自携带自己的污点历史。
    """

    condition: str
    when_true: Node
    when_false: Node
    location: str

    def __post_init__(self) -> None:
        _check_location("Branch", self.location)
        if not isinstance(self.condition, str) or not self.condition:
            raise InvalidNodeError("Branch 的 condition 必须是非空字符串", self.location)
        if not isinstance(self.when_true, Node) or not isinstance(self.when_false, Node):
            raise InvalidNodeError("Branch 的两个分支必须是 Node", self.location)

    def children(self) -> tuple[Node, ...]:
        return (self.when_true, self.when_false)


@dataclass(frozen=True)
class Call(Node):
    """调用节点：内联名为 ``name`` 的被调函数的全部可能出口。"""

    name: str
    location: str

    def __post_init__(self) -> None:
        _check_location("Call", self.location)
        if not isinstance(self.name, str) or not self.name:
            raise InvalidNodeError("Call 的 name 必须是非空字符串", self.location)


@dataclass(frozen=True)
class Catch(Node):
    """捕获节点（try/catch）。

    * ``body``：受保护主体；
    * ``handler``：捕获后执行的处理块；
    * ``catches``：可捕获的异常类别集合，``{"denied"}``、``{"error"}``
      或 ``{"denied", "error"}``；权限拒绝只有被匹配的 catch 才会被消费；
    * 主体正常完成时不进入处理块，处理块内部的事件与主体相互独立。
    """

    body: Node
    handler: Node
    location: str
    catches: frozenset[str] = frozenset({DENIED, ERROR})

    def __init__(
        self,
        body: Node,
        handler: Node,
        location: str,
        catches: frozenset[str] | set[str] | tuple[str, ...] | None = None,
    ) -> None:
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "handler", handler)
        object.__setattr__(self, "location", location)
        object.__setattr__(
            self,
            "catches",
            frozenset({DENIED, ERROR}) if catches is None else frozenset(catches),
        )
        self.__post_init__()

    def __post_init__(self) -> None:
        _check_location("Catch", self.location)
        if not isinstance(self.body, Node) or not isinstance(self.handler, Node):
            raise InvalidNodeError("Catch 的 body 与 handler 必须是 Node", self.location)
        if not self.catches or not self.catches.issubset(EXCEPTION_KINDS):
            raise InvalidNodeError(
                f"catches 必须是 {sorted(EXCEPTION_KINDS)} 的非空子集", self.location
            )

    def children(self) -> tuple[Node, ...]:
        return (self.body, self.handler)


@dataclass(frozen=True)
class Finally(Node):
    """finally 节点（try/finally，无 catch 语义）。

    * ``body``：主体；
    * ``cleanup``：无论主体如何完成都会执行的清理块。

    清理块正常结束（落到块尾）时保留主体待传播的结果；清理块显式
    返回或抛出时替换主体的结果，并记录 :class:`FinallyOverride` 事件。
    """

    body: Node
    cleanup: Node
    location: str

    def __post_init__(self) -> None:
        _check_location("Finally", self.location)
        if not isinstance(self.body, Node) or not isinstance(self.cleanup, Node):
            raise InvalidNodeError("Finally 的 body 与 cleanup 必须是 Node", self.location)

    def children(self) -> tuple[Node, ...]:
        return (self.body, self.cleanup)


StructuralNode = Union[Sequence, Branch, Call, Catch, Finally]
LeafNode = Union[Return, Pass, Deny, Error]
AnyNode = Union[StructuralNode, LeafNode]


# ---------------------------------------------------------------------------
# 污点事件：记录每一次“拒绝被消费 / 替换 / 改写”
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Caught:
    """权限拒绝被某个 catch 捕获。

    * ``at``：Catch 节点位置；
    * ``denials``：本次被消费的拒绝入口位置（可能有多个：处理块再次抛出
      的拒绝也会在外层被一起捕获）；
    * ``replacement``：处理块的出口形式——
      ``"return"`` 改成正常返回；
      ``"denied"`` 替换为另一次权限拒绝；
      ``"error"`` 替换为普通错误。
    """

    at: str
    denials: tuple[str, ...]
    replacement: str


@dataclass(frozen=True)
class FinallyOverride:
    """finally 清理块的显式返回/抛出替换了待传播的结果。"""

    at: str
    denials: tuple[str, ...]
    replacement: str  # "return" | "denied" | "error"
    original: str  # 被替换掉的主体完成形式："return" | "denied" | "error"


Event = Union[Caught, FinallyOverride]


# ---------------------------------------------------------------------------
# 执行状态
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class State:
    """沿单条抽象路径传播的污点状态。"""

    denials: tuple[str, ...] = ()
    events: tuple[Event, ...] = ()

    @classmethod
    def initial(cls) -> "State":
        return cls()

    def with_denial(self, location: str) -> "State":
        return State(self.denials + (location,), self.events)

    def with_event(self, event: Event) -> "State":
        return State(self.denials, self.events + (event,))

    def merge_denials(self, denials: tuple[str, ...]) -> "State":
        """并入处理块/清理块内部新增的拒绝入口（保持出现顺序、去重）。"""
        merged = list(self.denials)
        for item in denials:
            if item not in merged:
                merged.append(item)
        return State(tuple(merged), self.events)

    @property
    def ever_denied(self) -> bool:
        """该路径上是否曾经出现过权限拒绝（无论后来是否被消费）。"""
        return bool(self.denials)


# ---------------------------------------------------------------------------
# 出口（分析结果的最小单位）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Exit:
    """公共出口字段。

    * ``origin``：决定该出口形式的叶节点位置（溯源点）；
    * ``state``：到达该出口时的污点状态；
    * ``ever_denied``：便捷标志，路径上是否曾经拒绝。
    """

    origin: str
    state: State

    @property
    def ever_denied(self) -> bool:
        return self.state.ever_denied


@dataclass(frozen=True)
class ReturnExit(Exit):
    """正常返回出口（包括 catch 吞掉拒绝后改成的返回）。"""


@dataclass(frozen=True)
class ThrowExit(Exit):
    """向外抛出异常的出口。"""

    kind: str  # DENIED | ERROR

    @property
    def is_denial(self) -> bool:
        return self.kind == DENIED


@dataclass(frozen=True)
class _Normal:
    """块内部的“正常落到块尾”完成形式，不会出现在最终报告里。"""

    state: State


# 块语句执行后的完成形式：正常落尾 / 显式返回 / 抛出。
Completion = Union[_Normal, ReturnExit, ThrowExit]
AnyExit = Union[ReturnExit, ThrowExit]


# ---------------------------------------------------------------------------
# 函数与程序（函数森林）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Function:
    """一个无递归结构化函数：名字 + 函数体节点。"""

    name: str
    body: Node

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise InvalidNodeError("Function 的 name 必须是非空字符串")
        if not isinstance(self.body, Node):
            raise InvalidNodeError(f"函数 {self.name!r} 的 body 必须是 Node")


@dataclass(frozen=True)
class Program:
    """函数森林：函数集合 + 指定的分析入口名。"""

    functions: tuple[Function, ...]
    entry: str

    def lookup(self, name: str) -> Function:
        for func in self.functions:
            if func.name == name:
                return func
        raise KeyError(name)
