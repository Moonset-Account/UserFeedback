# -*- coding: utf-8 -*-
"""采集任务编排：从规定接口取原文 → 按显式地区规则解析 → 保存结构化结果。

同一份原文可在不同地区规则下重新解释（reinterpret），
解析器无状态、不缓存猜测结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .extractor import parse_measurement, Measurement
from .fetchers import (
    RawRecord, CollectorError,
    collect_file, collect_http, collect_browser,
)
from .locales import get_rule, LocaleRule


@dataclass
class TaskResult:
    source_type: str
    source_descriptor: str
    locale_code: str
    locale_origin: str
    records: list[dict] = field(default_factory=list)
    collection_error: dict | None = None
    total: int = 0
    ok_count: int = 0
    error_count: int = 0
    missing_count: int = 0

    def to_dict(self) -> dict:
        return {
            "source_type": self.source_type,
            "source_descriptor": self.source_descriptor,
            "locale_code": self.locale_code,
            "locale_origin": self.locale_origin,
            "records": self.records,
            "collection_error": self.collection_error,
            "total": self.total,
            "ok_count": self.ok_count,
            "error_count": self.error_count,
            "missing_count": self.missing_count,
        }


def _run_records(records: list[RawRecord], rule: LocaleRule,
                 source_type: str, descriptor: str,
                 locale_origin: str) -> TaskResult:
    result = TaskResult(
        source_type=source_type,
        source_descriptor=descriptor,
        locale_code=rule.code,
        locale_origin=locale_origin,
    )
    for rec in records:
        # 每条记录自带显式地区（源声明或用户选择经 fetchers 解析）；
        # 任务级 rule 为兜底，二者均来自显式来源。
        effective = get_rule(rec.declared_locale)
        m: Measurement = parse_measurement(rec.raw_value, effective)
        m.source = rec.to_dict()
        d = m.to_dict()
        result.records.append(d)
        result.total += 1
        if m.status == "error":
            result.error_count += 1
        elif m.status == "missing":
            result.missing_count += 1
        else:
            result.ok_count += 1
    return result


def run_text_input(lines: list[str], locale_code: str) -> TaskResult:
    """直接粘贴文本的采集入口（来源类型 manual_text）。"""
    rule = get_rule(locale_code)
    records = [
        RawRecord(raw_value=line.strip(),
                  source_type="manual_text",
                  location={"line": i + 1,
                            "locale_origin": "用户显式选择" if locale_code != "und"
                            else "用户未选择，按未定地区处理"},
                  declared_locale=locale_code, row_id=str(i + 1))
        for i, line in enumerate(lines) if line.strip()
    ]
    return _run_records(records, rule, "manual_text",
                        f"手动输入 {len(records)} 行",
                        "用户显式选择" if locale_code != "und" else "未定地区")


def run_file(path: str, locale_code: str | None = None,
             value_field: str = "value", id_field: str = "id") -> TaskResult:
    try:
        records = collect_file(path, explicit_locale=locale_code,
                               value_field=value_field, id_field=id_field)
    except CollectorError as exc:
        return TaskResult("file", path, locale_code or "und",
                          "用户显式选择" if locale_code else "未定地区",
                          collection_error={"code": "COLLECT_FAILED", "message": str(exc)})
    origin = records[0].location.get("locale_origin", "") if records else ""
    rule = get_rule(locale_code or (records[0].declared_locale if records else "und"))
    return _run_records(records, rule, "file", path, origin)


def run_http(url: str, locale_code: str | None = None,
             value_field: str = "value", id_field: str = "id") -> TaskResult:
    try:
        records = collect_http(url, explicit_locale=locale_code,
                               value_field=value_field, id_field=id_field)
    except CollectorError as exc:
        return TaskResult("http", url, locale_code or "und",
                          "用户显式选择" if locale_code else "未定地区",
                          collection_error={"code": "COLLECT_FAILED", "message": str(exc)})
    origin = records[0].location.get("locale_origin", "") if records else ""
    rule = get_rule(locale_code or (records[0].declared_locale if records else "und"))
    return _run_records(records, rule, "http", url, origin)


def run_browser(url: str, locale_code: str | None = None,
                headless: bool = True) -> TaskResult:
    try:
        records = collect_browser(url, explicit_locale=locale_code, headless=headless)
    except CollectorError as exc:
        return TaskResult("browser_dom", url, locale_code or "und",
                          "用户显式选择" if locale_code else "未定地区",
                          collection_error={"code": "COLLECT_FAILED", "message": str(exc)})
    origin = records[0].location.get("locale_origin", "") if records else ""
    rule = get_rule(locale_code or (records[0].declared_locale if records else "und"))
    return _run_records(records, rule, "browser_dom", url, origin)


def reinterpret(raw_texts: list[str], locale_code: str) -> TaskResult:
    """用新的地区规则重新解释同一批原文（典型场景：1,234 在 en_US/de_DE 下）。"""
    return run_text_input(raw_texts, locale_code)
