"""sigreview —— 异步验签制品发布静态审查库。

读取构建器导出的有限控制流图（CFG），在每个发布点判断：所有可达
分支是否都持有针对**实际被发布快照**的成功验签证明。

公开入口：
    analyze_graph(graph, ...)  / analyze_dict(data, ...) / analyze_file(path, ...)
"""

from .analysis import (
    PublishReport,
    ReviewResult,
    analyze_graph,
)
from .loader import CFGFormatError, load_dict, load_file
from .model import (
    BRANCH,
    PUBLISH,
    SNAPSHOT,
    VERIFY,
    WAIT_ALL,
    WAIT_ANY,
    CFG,
    Node,
)

__all__ = [
    "analyze_graph",
    "load_dict",
    "load_file",
    "CFGFormatError",
    "Node",
    "CFG",
    "PublishReport",
    "ReviewResult",
    "SNAPSHOT",
    "VERIFY",
    "WAIT_ALL",
    "WAIT_ANY",
    "BRANCH",
    "PUBLISH",
]
