"""离线漏洞适用性裁决器（仅依赖 Python 3 标准库）。

模块划分：
- versioning: 三段式数字版本与预发布版本的解析、排序
- interval:   带端点开闭标记的版本区间与无界区间
- models:     组件、清单、公告、适用性记录等数据模型
- loader:     JSON 装载与结构校验
- engine:     逐条裁决与公告更新前后的差异报告
- report:     文本 / JSON 报告渲染
- cli:        命令行入口
"""

from .versioning import Version, IncomparableVersionError, parse_version, compare
from .interval import Interval, IntervalState
from .models import Component, Bom, Advisory, Statement, ApplicabilityRecord, Finding
from .engine import Adjudicator, DISPOSITIONS

__all__ = [
    "Version",
    "IncomparableVersionError",
    "parse_version",
    "compare",
    "Interval",
    "IntervalState",
    "Component",
    "Bom",
    "Advisory",
    "Statement",
    "ApplicabilityRecord",
    "Finding",
    "Adjudicator",
    "DISPOSITIONS",
]
