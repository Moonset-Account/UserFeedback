"""IPv6 前缀委派迁移规划器（仅使用标准库）。

公开 API 见 README.md。核心模块：

* :mod:`planner.prefix`     128 位整数区间上的前缀与区间运算
* :mod:`planner.model`      输入/输出数据模型
* :mod:`planner.aggregate`  精确汇总（最小通告集）
* :mod:`planner.scheduler`  最早安全完成排程搜索
* :mod:`planner.verifier`   符号验证与小地址投影逐地址验证
* :mod:`planner.render`     文本/JSON 渲染
"""

from .aggregate import minimal_advertisements
from .model import (
    Advertisement,
    Event,
    Prefix,
    Problem,
    Schedule,
    Tenant,
    TempPrefix,
    Upstream,
)
from .scheduler import NoFeasibleSchedule, plan
from .verifier import verify_schedule, verify_small_projection

__all__ = [
    "Advertisement",
    "Event",
    "NoFeasibleSchedule",
    "Prefix",
    "Problem",
    "Schedule",
    "Tenant",
    "TempPrefix",
    "Upstream",
    "minimal_advertisements",
    "plan",
    "verify_schedule",
    "verify_small_projection",
]
