"""目录扫描器：只经公开 HTTP GET 接口翻页采集，严格执行请求预算。

预算口径：**每发起一次 HTTP 请求尝试（含重试）计 1 次请求**。
预算耗尽时立即停止，原因记为 budget_exhausted，并由上层 runner 声明
"样本仅代表已观察集合，不构成全站无偏样本"。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

import requests

from .parsers import ParsedPage, ParsedRecord, UnsupportedProtocolError, parse_page

# 可重试的瞬时失败：连接错误、超时、429、5xx
_RETRY_STATUS = {429, 500, 502, 503, 504}


class ScanError(RuntimeError):
    """采集无法继续（非预算原因），携带已发生的页面上下文。"""


@dataclass(frozen=True)
class PageEvent:
    page_index: int                  # 从 1 开始
    url: str
    http_status: int
    raw_count: int                   # 本页原始条目数（含重复）
    unique_on_page: int              # 本页新出现的唯一 ID 数
    duplicate_on_page: int           # 本页与此前页面/本页重复的条目数
    requests_used: int               # 截至本页累计请求尝试次数
    records: list[ParsedRecord] = field(repr=False)


@dataclass(frozen=True)
class StopEvent:
    reason: str            # completed | budget_exhausted | request_error | protocol_error
    detail: str
    last_page_index: int
    requests_used: int
    budget: int
    next_url_not_fetched: str | None = None  # 预算耗尽时未抓取的下一页 URL（扫描边界证据）


@dataclass
class ScanReport:
    start_url: str
    pages: list[PageEvent]
    stop: StopEvent
    ordered_unique_records: list[ParsedRecord]  # 首次出现顺序的唯一记录（唯一 ID 流）
    total_raw_entries: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_url": self.start_url,
            "pages": [asdict(p) | {"records": [asdict(r) for r in p.records]} for p in self.pages],
            "stop": asdict(self.stop),
            "ordered_unique_records": [asdict(r) for r in self.ordered_unique_records],
            "total_raw_entries": self.total_raw_entries,
        }


EventCallback = Callable[[PageEvent], None]


class CatalogScanner:
    def __init__(
        self,
        start_url: str,
        request_budget: int,
        *,
        timeout: float = 8.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.2,
        session: requests.Session | None = None,
        event_callback: EventCallback | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(request_budget, int) or request_budget <= 0:
            raise ValueError("request_budget 必须为正整数")
        self.start_url = start_url
        self.budget = request_budget
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._session = session or requests.Session()
        self._event_callback = event_callback
        self._sleep = sleep

    def _get_once(self, url: str) -> requests.Response:
        return self._session.get(
            url,
            timeout=self.timeout,
            headers={"Accept": "application/json, text/html", "User-Agent": "bounded-sampling-collector/1.0"},
        )

    def _get_with_budget(self, url: str) -> requests.Response:
        """发起请求并记账；预算不足直接抛 ScanError(budget_exhausted)。"""
        resp: requests.Response | None = None
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if self._requests_used >= self.budget:
                raise _BudgetExhausted(url)
            self._requests_used += 1
            try:
                resp = self._get_once(url)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    self._sleep(self.backoff_seconds * (attempt + 1))
                    continue
                raise ScanError(f"请求失败（已重试 {self.max_retries} 次）: {url} :: {exc}") from exc

            if resp.status_code in _RETRY_STATUS and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                delay = self.backoff_seconds * (attempt + 1)
                if retry_after and retry_after.isdigit():
                    delay = max(delay, min(float(retry_after), 2.0))
                self._sleep(delay)
                continue
            if resp.status_code != 200:
                raise ScanError(f"HTTP {resp.status_code}（不可重试）: {url}")
            return resp

        if resp is not None:
            raise ScanError(f"HTTP {resp.status_code}（重试 {self.max_retries} 次后仍失败）: {url}")
        raise ScanError(f"请求失败: {url} :: {last_exc}")

    def scan(self) -> ScanReport:
        self._requests_used = 0
        pages: list[PageEvent] = []
        seen: set[str] = set()
        ordered_unique: list[ParsedRecord] = []
        total_raw = 0
        url: str | None = self.start_url
        page_index = 0

        try:
            while url is not None:
                resp = self._get_with_budget(url)
                try:
                    parsed: ParsedPage = parse_page(resp.content, resp.headers.get("Content-Type", ""), url)
                except UnsupportedProtocolError as exc:
                    return self._finish(
                        pages, ordered_unique, total_raw,
                        StopEvent(
                            reason="protocol_error",
                            detail=str(exc),
                            last_page_index=page_index,
                            requests_used=self._requests_used,
                            budget=self.budget,
                        ),
                        self.start_url,
                    )

                page_index += 1
                unique_here = 0
                dup_here = 0
                page_seen: set[str] = set()
                for rec in parsed.records:
                    total_raw += 1
                    if rec.specimen_id in page_seen:
                        dup_here += 1
                        continue
                    page_seen.add(rec.specimen_id)
                    if rec.specimen_id in seen:
                        dup_here += 1
                    else:
                        seen.add(rec.specimen_id)
                        ordered_unique.append(rec)
                        unique_here += 1

                evt = PageEvent(
                    page_index=page_index,
                    url=url,
                    http_status=resp.status_code,
                    raw_count=len(parsed.records),
                    unique_on_page=unique_here,
                    duplicate_on_page=dup_here,
                    requests_used=self._requests_used,
                    records=list(parsed.records),
                )
                pages.append(evt)
                if self._event_callback:
                    self._event_callback(evt)

                url = parsed.next_url

            stop = StopEvent(
                reason="completed",
                detail="已到达末页（服务端未返回下一页游标/链接），目录扫描完整",
                last_page_index=page_index,
                requests_used=self._requests_used,
                budget=self.budget,
            )
            return self._finish(pages, ordered_unique, total_raw, stop, self.start_url)

        except _BudgetExhausted as exc:
            stop = StopEvent(
                reason="budget_exhausted",
                detail=(
                    f"请求预算 {self.budget} 次已用尽，扫描在第 {page_index + 1} 页前中止；"
                    "抽样仅针对已观察集合，不构成全站无偏样本"
                ),
                last_page_index=page_index,
                requests_used=self._requests_used,
                budget=self.budget,
                next_url_not_fetched=exc.url,
            )
            return self._finish(pages, ordered_unique, total_raw, stop, self.start_url)

    @staticmethod
    def _finish(
        pages: list[PageEvent],
        ordered_unique: list[ParsedRecord],
        total_raw: int,
        stop: StopEvent,
        start_url: str,
    ) -> ScanReport:
        return ScanReport(
            start_url=start_url,
            pages=pages,
            stop=stop,
            ordered_unique_records=ordered_unique,
            total_raw_entries=total_raw,
        )


class _BudgetExhausted(Exception):
    """内部信号：下一次请求将超出预算。"""

    def __init__(self, url: str) -> None:
        super().__init__(f"budget exhausted before fetching {url}")
        self.url = url
