"""denialflow —— 权限拒绝传播的结构化函数树静态分析库。

典型用法
========

>>> from denialflow import (
...     Program, Function, Sequence, Branch, Catch, Finally,
...     Return, Pass, Deny, Error, Call, analyze,
... )
>>> tree = Sequence([Deny("check"), Return("ok")], "main")
>>> report = analyze(Program((Function("main", tree),), entry="main"))
>>> report.denial_origins
('check',)
"""

from __future__ import annotations

from .analyzer import (
    FATE_FINALLY_DENIED,
    FATE_FINALLY_ERROR,
    FATE_FINALLY_RETURN,
    FATE_PROPAGATES_DENIED,
    FATE_TO_DENIED,
    FATE_TO_ERROR,
    FATE_TO_RETURN,
    Analyzer,
    Fate,
    Report,
    analyze,
)
from .errors import (
    DenialFlowError,
    InvalidNodeError,
    RecursiveCallError,
    UnknownFunctionError,
)
from .model import (
    DENIED,
    ERROR,
    Branch,
    Call,
    Catch,
    Caught,
    Deny,
    Error,
    Finally,
    FinallyOverride,
    Function,
    Node,
    Pass,
    Program,
    Return,
    ReturnExit,
    Sequence,
    State,
    ThrowExit,
)

__all__ = [
    # 节点与程序
    "Node",
    "Sequence",
    "Branch",
    "Call",
    "Catch",
    "Finally",
    "Return",
    "Pass",
    "Deny",
    "Error",
    "Function",
    "Program",
    # 状态 / 事件 / 出口
    "State",
    "Caught",
    "FinallyOverride",
    "ReturnExit",
    "ThrowExit",
    "DENIED",
    "ERROR",
    # 分析
    "Analyzer",
    "Report",
    "Fate",
    "analyze",
    "FATE_PROPAGATES_DENIED",
    "FATE_TO_RETURN",
    "FATE_TO_ERROR",
    "FATE_TO_DENIED",
    "FATE_FINALLY_RETURN",
    "FATE_FINALLY_ERROR",
    "FATE_FINALLY_DENIED",
    # 异常
    "DenialFlowError",
    "InvalidNodeError",
    "UnknownFunctionError",
    "RecursiveCallError",
]
