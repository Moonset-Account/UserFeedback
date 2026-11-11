"""采集器的问题（未支持规则 / 数据错误）模型。

所有问题都以结构化方式上报，而不是静默忽略：
- 级别 warning 表示"该规则未被支持，已跳过，其余内容继续处理"；
- 级别 error 表示"该组件无法处理或整体采集失败"。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum


class IssueCode(str, Enum):
    """问题代码，同时作为"支持的协议子集"的负面清单。"""

    # --- 不支持的 RRULE 特性（本项目明确声明的子集之外）---
    RRULE_SECONDLY = "RRULE_SECONDLY"            # FREQ=SECONDLY
    RRULE_MINUTELY = "RRULE_MINUTELY"            # FREQ=MINUTELY
    RRULE_HOURLY = "RRULE_HOURLY"                # FREQ=HOURLY
    RRULE_BY_YEAR_DAY = "RRULE_BY_YEAR_DAY"      # BYYEARDAY（含 BYYEARDAY=0 等特例）
    RRULE_BY_WEEK_NO = "RRULE_BY_WEEK_NO"        # BYWEEKNO
    RRULE_RSCALE = "RRULE_RSCALE"                # RFC 7529 RSCALE 等非公历扩展
    RRULE_SKIP = "RRULE_SKIP"                    # RFC 7529 SKIP
    RRULE_MULTIPLE = "RRULE_MULTIPLE"            # 单事件含多个 RRULE（RFC 允许但本采集器不支持）
    RRULE_BAD_PART = "RRULE_BAD_PART"            # 无法识别/非法的 RRULE 部件
    RRULE_BAD_FREQ = "RRULE_BAD_FREQ"            # 缺少 FREQ 或 FREQ 非法
    RRULE_COUNT_UNTIL_BOTH = "RRULE_COUNT_UNTIL_BOTH"  # COUNT 与 UNTIL 同时出现（RFC 禁止）

    # --- 不支持的其它重复组件 ---
    RDATE_PERIOD = "RDATE_PERIOD"                # RDATE 值为 PERIOD（只支持 DATE/DATE-TIME）
    RDATE_BAD_VALUE = "RDATE_BAD_VALUE"          # RDATE 值无法解析
    RECURRENCE_ID_RANGE = "RECURRENCE_ID_RANGE"  # RECURRENCE-ID 带 RANGE 参数
    NO_MASTER = "NO_MASTER"                      # 找不到对应主事件的 RECURRENCE-ID 例外

    # --- 解析层问题 ---
    PARSE_ERROR = "PARSE_ERROR"                  # ICS 文本无法解析
    INVALID_CALENDAR = "INVALID_CALENDAR"        # 顶层不是 VCALENDAR / 缺少必要属性
    UNSUPPORTED_COMPONENT = "UNSUPPORTED_COMPONENT"  # VTODO/VJOURNAL/VAVAILABILITY 等
    BAD_DATETIME = "BAD_DATETIME"                # 日期时间属性无法解析

    # --- 采集层问题 ---
    FETCH_ERROR = "FETCH_ERROR"                  # 下载/读取失败
    UNSUPPORTED_SCHEME = "UNSUPPORTED_SCHEME"    # 非 http(s)/file 来源
    RANGE_LIMIT = "RANGE_LIMIT"                  # 展开规模触及上限


def severity_of(code: IssueCode) -> str:
    """返回问题的默认级别。"""
    if code in (
        IssueCode.PARSE_ERROR,
        IssueCode.INVALID_CALENDAR,
        IssueCode.FETCH_ERROR,
        IssueCode.UNSUPPORTED_SCHEME,
    ):
        return "error"
    return "warning"


@dataclass
class Issue:
    code: str
    message: str
    severity: str = "warning"
    uid: str | None = None
    line: int | None = None
    property_name: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def make(cls, code: IssueCode, message: str, *, uid: str | None = None,
             line: int | None = None, property_name: str | None = None) -> "Issue":
        return cls(
            code=code.value,
            message=message,
            severity=severity_of(code),
            uid=uid,
            line=line,
            property_name=property_name,
        )
