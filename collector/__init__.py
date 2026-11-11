"""公开天文观测窗口 iCalendar 采集器。

本包只负责"采集 + 解析 + 展开"，不做预约或日程管理：
- fetch:   仅通过公开 HTTP(S) 接口或本地文件取得 ICS 文本；
- expand:  在有界时间区间内展开 RRULE/RDATE/EXDATE/RECURRENCE-ID；
- service: 一次采集任务的编排、证据与错误记录；
- store:   按目标名整体替换的结果存储（修改例外后不残留旧实例）。
"""

from .errors import Issue, IssueCode
from .service import collect, RunRequest

__all__ = ["collect", "RunRequest", "Issue", "IssueCode"]
