"""文档批量改写检测的事件分析模块。

核心入口：

* :class:`~doc_rewrite.events.Event` / :func:`~doc_rewrite.events.event_from_dict`：
  可信采集端事件的数据模型与反序列化。
* :class:`~doc_rewrite.policy.Policy`：策略参数（窗口长度、文档/目录阈值等）。
* :class:`~doc_rewrite.analyzer.RewriteAnalyzer`：乱序事件分析器，
  支持显式水位线、暂定/确认发现、最小支持事件集合与未知清单。

设计原则见 ``README.md``。本包仅依赖 Python 标准库。
"""

from doc_rewrite.analyzer import RewriteAnalyzer
from doc_rewrite.events import Event, EventType, event_from_dict, load_scenario
from doc_rewrite.policy import Policy

__all__ = [
    "Event",
    "EventType",
    "Policy",
    "RewriteAnalyzer",
    "event_from_dict",
    "load_scenario",
]
