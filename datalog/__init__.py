"""分层 Datalog 引擎（有限关系上的声明式规则求值）。

公开 API 见模块级 ``__all__``，说明见 README。
"""

from .engine import (
    Atom,
    DatalogError,
    Literal,
    NOT,
    ProofNode,
    NegationCheck,
    QueryEngine,
    Rule,
    SafetyError,
    StratificationError,
    Var,
)

__all__ = [
    "Atom",
    "DatalogError",
    "Literal",
    "NOT",
    "ProofNode",
    "NegationCheck",
    "QueryEngine",
    "Rule",
    "SafetyError",
    "StratificationError",
    "Var",
]
