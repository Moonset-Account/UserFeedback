"""authflow — 授权事实与持久化写入是否绑定在同一对象版本上的静态分析。

公开 API（详见 :mod:`authflow.analysis` 与 :mod:`authflow.model`）：

- IR 模型：:class:`~authflow.model.Program`、:class:`~authflow.model.ProgramBuilder`
  以及各类节点（Entry / Branch / AuthCheck / Alias / Call / TxBegin / TxCommit /
  TxRollback / Merge）。
- 分析入口：:func:`~authflow.analysis.analyze`，返回
  :class:`~authflow.analysis.AnalysisResult`，其中包含受约束写入、
  无授权约束写入 :class:`~authflow.analysis.Finding`、被回滚/覆盖而失效的授权事实
  以及可达与不可达调用的区分。

设计概览
--------
分析在控制流图（CFG）上做单调数据流分析：

1. 成功的授权检查产生*授权事实*，事实是 ``(对象, 版本, 操作)`` 三元组，
   并记录产生它的检查节点作为来源。
2. 事实只能沿着可执行路径前向传播；分支交汇时取交集（必须在所有路径上成立）。
3. 调用摘要若声明替换某对象，则该对象旧版本上的所有事实立即失效；
   若推进某变量到新版本，则该变量上的旧事实同样失效。
4. 别名只传播*引用*，副本则是新对象，事实不随副本传播。
5. 事务回滚使事务内产生/传播的事实失效并恢复事务前快照；提交则保留事实。
6. 不可达调用与可达但无法证明授权的写入分别报告。
"""

from authflow.analysis import (
    AnalysisResult,
    ChainLink,
    Env,
    FactKey,
    Finding,
    analyze,
)
from authflow.errors import ProgramStructureError
from authflow.model import (
    AliasNode,
    AuthCheckNode,
    BranchNode,
    CallNode,
    EntryNode,
    MergeNode,
    Node,
    Program,
    ProgramBuilder,
    TxBeginNode,
    TxCommitNode,
    TxRollbackNode,
)

__all__ = [
    "AnalysisResult",
    "AliasNode",
    "AuthCheckNode",
    "BranchNode",
    "CallNode",
    "ChainLink",
    "EntryNode",
    "Env",
    "FactKey",
    "Finding",
    "MergeNode",
    "Node",
    "Program",
    "ProgramBuilder",
    "ProgramStructureError",
    "TxBeginNode",
    "TxCommitNode",
    "TxRollbackNode",
    "analyze",
]
