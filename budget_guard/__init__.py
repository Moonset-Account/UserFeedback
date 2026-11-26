"""budget_guard: 受限操作图的静态资源预算分析。

公开 API:
    analyze(graph, evidence=None, *, reject=()) -> AnalysisReport
    evaluate(report, *, max_bytes=None, max_records=None, strict_warnings=False)
        -> GateDecision
    load_operation_graph(source) -> dict
    load_evidence(source) -> dict

模块只使用 Python 标准库，不访问网络，也不执行操作图描述的任何操作。
"""

from .errors import GraphValidationError
from .model import (
    Cost,
    Issue,
    NodeReport,
    BranchVerdict,
    AnalysisReport,
    GateDecision,
    B,
)
from .analyzer import analyze
from .gate import evaluate
from .loader import load_operation_graph, load_evidence

# 语义化别名，方便不同调用习惯
analyze_graph = analyze
check_budget = evaluate

__all__ = [
    "analyze",
    "analyze_graph",
    "evaluate",
    "check_budget",
    "load_operation_graph",
    "load_evidence",
    "GraphValidationError",
    "Cost",
    "Issue",
    "NodeReport",
    "BranchVerdict",
    "AnalysisReport",
    "GateDecision",
    "B",
]

__version__ = "1.0.0"
