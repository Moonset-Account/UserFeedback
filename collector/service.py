"""一次采集任务的编排：获取 → 解析 → 有界展开 → 结构化结果。"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from icalendar import Calendar

from .errors import Issue, IssueCode
from .expand import DEFAULT_MAX_INSTANCES, expand_calendar
from .fetch import fetch


@dataclass
class RunRequest:
    source: str
    range_start: str                       # ISO 日期或日期时间
    range_end: str
    max_instances: int = DEFAULT_MAX_INSTANCES
    target_name: str | None = None         # 存储用目标名；为空则不落盘

    @staticmethod
    def from_dict(data: dict) -> "RunRequest":
        return RunRequest(
            source=str(data["source"]),
            range_start=str(data["range_start"]),
            range_end=str(data["range_end"]),
            max_instances=int(data.get("max_instances", DEFAULT_MAX_INSTANCES)),
            target_name=data.get("target_name"),
        )


def _parse_boundary(text: str) -> dt.datetime | dt.date:
    """查询范围边界：YYYY-MM-DD 视为当日 00:00 的日期；含 T 则为日期时间。"""
    if "T" in text:
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed  # 范围边界用 naive，与浮动时间同尺度比较
        return parsed
    return dt.date.fromisoformat(text)


def _bound_to_type(bound: dt.datetime | dt.date, kind: str):
    """把查询边界对齐到事件的时间尺度。"""
    if kind == "all_day":
        return bound.date() if isinstance(bound, dt.datetime) else bound
    if isinstance(bound, dt.date) and not isinstance(bound, dt.datetime):
        return dt.datetime.combine(bound, dt.time.min)
    return bound


def collect(request: RunRequest, storage=None) -> dict:
    """执行采集，返回可直接 JSON 序列化的运行结果。"""
    run_id = uuid.uuid4().hex[:12]
    started_at = dt.datetime.now().isoformat(timespec="seconds")
    issues: list[dict] = []

    try:
        range_lo = _parse_boundary(request.range_start)
        range_hi = _parse_boundary(request.range_end)
    except ValueError as exc:
        return _failed_run(run_id, request, started_at, issues,
                           f"查询范围格式错误：{exc}")
    if not range_hi > range_lo:
        return _failed_run(run_id, request, started_at, issues,
                           "查询范围结束时间必须晚于开始时间")

    fetch_result = fetch(request.source)
    evidence = fetch_result.evidence.to_dict()
    if fetch_result.issue is not None:
        issues.append(fetch_result.issue.to_dict())
    if not fetch_result.ok:
        return _failed_run(run_id, request, started_at, issues,
                           "源数据获取失败", evidence=evidence)

    try:
        calendar = Calendar.from_ical(fetch_result.text)
    except Exception as exc:  # icalendar 内部解析异常类型较多，统一结构化上报
        issues.append(Issue.make(
            IssueCode.PARSE_ERROR,
            f"iCalendar 解析失败：{type(exc).__name__}: {exc}",
        ).to_dict())
        return _failed_run(run_id, request, started_at, issues,
                           "iCalendar 解析失败", evidence=evidence)

    if not getattr(calendar, "name", None) == "VCALENDAR":
        issues.append(Issue.make(
            IssueCode.INVALID_CALENDAR,
            f"顶层组件为 {getattr(calendar, 'name', 'UNKNOWN')}，应为 VCALENDAR",
        ).to_dict())
        return _failed_run(run_id, request, started_at, issues,
                           "数据不是合法 VCALENDAR", evidence=evidence)

    expanded = expand_calendar(
        calendar, fetch_result.text, range_lo, range_hi,
        max_instances=request.max_instances,
    )
    issues.extend(expanded["issues"])

    result = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
        "status": "success" if not any(i["severity"] == "error" for i in issues) else "completed_with_errors",
        "request": {
            "source": request.source,
            "range_start": request.range_start,
            "range_end": request.range_end,
            "max_instances": request.max_instances,
        },
        "source_evidence": evidence,
        "instances": expanded["instances"],
        "excluded": expanded["excluded"],
        "issues": issues,
        "stats": expanded["stats"],
        "supported_subset": _supported_subset(),
    }

    if storage is not None and request.target_name:
        storage.save(request.target_name, result)
    return result


def _failed_run(run_id, request: RunRequest, started_at, issues, message,
                evidence=None) -> dict:
    return {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
        "status": "failed",
        "error_message": message,
        "request": {
            "source": request.source,
            "range_start": request.range_start,
            "range_end": request.range_end,
            "max_instances": request.max_instances,
        },
        "source_evidence": evidence,
        "instances": [],
        "excluded": [],
        "issues": issues,
        "stats": {"candidates_produced": 0, "in_range": 0, "cancelled": 0,
                  "master_events": 0, "override_events": 0, "excluded_count": 0},
        "supported_subset": _supported_subset(),
    }


def _supported_subset() -> dict:
    return {
        "components": ["VCALENDAR", "VEVENT", "VTIMEZONE"],
        "rrule_freq": ["DAILY", "WEEKLY", "MONTHLY", "YEARLY"],
        "rrule_parts": ["INTERVAL", "COUNT", "UNTIL", "WKST", "BYDAY",
                        "BYMONTHDAY", "BYMONTH", "BYSETPOS"],
        "supported": ["RDATE(DATE/DATE-TIME)", "EXDATE(DATE/DATE-TIME)",
                      "RECURRENCE-ID 移动与取消(STATUS=CANCELLED)",
                      "全日事件", "跨日时长", "浮动时间", "VTIMEZONE 显式时区/夏令时"],
        "unsupported": ["FREQ=SECONDLY/MINUTELY/HOURLY", "BYYEARDAY", "BYWEEKNO",
                        "BYHOUR/BYMINUTE/BYSECOND", "RSCALE/SKIP(RFC 7529)",
                        "RDATE PERIOD", "RECURRENCE-ID RANGE", "单事件多 RRULE",
                        "COUNT 与 UNTIL 同时出现"],
    }
