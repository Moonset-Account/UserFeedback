"""采集编排：抓取 → 解析 → 可用性探测 → 身份归并 → 报告。

采集器只接收来源 URL/路径与参数，不依赖样例生成器的任何内部信息。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .errors import FeedParseError, FetchError
from .fetcher import DEFAULT_TIMEOUT, fetch_feed, probe_url
from .identity import build_catalog
from .models import to_jsonable
from .parser import parse_feed


@dataclass
class SourceSpec:
    url: str                       # 订阅源地址（http(s)/file）
    version_label: str = ""        # 同一订阅源不同版本快照的标签，如 v1/v2


def _apply_probe(record, result) -> None:
    record.available = result.available
    record.http_status = result.http_status
    record.content_type = result.content_type
    record.content_length = result.content_length
    record.redirect_chain = result.redirect_chain
    record.elapsed_ms = result.elapsed_ms
    record.probe_method = result.method
    record.error = result.error


def run_collection(sources: list[SourceSpec | dict],
                   *,
                   scenario: str = "custom",
                   timeout: float = DEFAULT_TIMEOUT,
                   probe_media: bool = True) -> dict:
    """执行一次采集任务，返回可直接序列化的报告字典。"""
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    specs = [s if isinstance(s, SourceSpec) else SourceSpec(**s)
             for s in sources]

    feeds_report: list[dict] = []
    parsed_feeds = []
    errors: list[dict] = []

    for index, spec in enumerate(specs):
        label = spec.version_label or f"v{index + 1}"
        feed_brief = {
            "version_index": index,
            "version_label": label,
            "requested_url": spec.url,
            "fetch_ok": False,
            "parse_ok": False,
        }
        try:
            raw = fetch_feed(spec.url, timeout=timeout)
            feed_brief["fetch_ok"] = True
            feed_brief["byte_length"] = len(raw)
        except FetchError as exc:
            errors.append({
                "stage": "fetch", "version_index": index,
                "version_label": label, "source": spec.url,
                "error_type": type(exc).__name__, "message": str(exc),
            })
            feed_brief["error"] = str(exc)
            feeds_report.append(feed_brief)
            continue

        try:
            parsed = parse_feed(raw, spec.url)
            feed_brief["parse_ok"] = True
            feed_brief["format"] = parsed.format
        except FeedParseError as exc:
            errors.append({
                "stage": "parse", "version_index": index,
                "version_label": label, "source": spec.url,
                "error_type": type(exc).__name__, "message": str(exc),
                "raw_excerpt": raw[:500].decode("utf-8", "replace"),
            })
            feed_brief["error"] = str(exc)
            feeds_report.append(feed_brief)
            continue

        # 附件可用性探测（只探测，不下载完整文件）
        if probe_media:
            seen: set[str] = set()
            for entry in parsed.entries:
                for enc in entry.enclosures:
                    if enc.resolved_url in seen:
                        continue
                    seen.add(enc.resolved_url)
                    _apply_probe(enc, probe_url(enc.resolved_url, timeout))
                for cover in entry.covers:
                    if cover.resolved_url in seen:
                        continue
                    seen.add(cover.resolved_url)
                    _apply_probe(cover, probe_url(cover.resolved_url, timeout))
            for cover in parsed.covers:
                if cover.resolved_url not in seen:
                    seen.add(cover.resolved_url)
                    _apply_probe(cover, probe_url(cover.resolved_url, timeout))
            # 不可用附件同步进错误清单，页面“具体错误”区展示
            for entry in parsed.entries:
                for enc in entry.enclosures:
                    if enc.available is False:
                        errors.append({
                            "stage": "enclosure_probe",
                            "version_index": index, "version_label": label,
                            "source": enc.resolved_url,
                            "entry_title": entry.title,
                            "error_type": "EnclosureUnavailable",
                            "message": (enc.error or "媒体不可用")
                                       + f"（探测方式 {enc.probe_method}）",
                        })

        parsed_feeds.append(parsed)
        feeds_report.append({
            **feed_brief,
            "feed": to_jsonable(parsed),
        })

    labels = [s.version_label or f"v{i + 1}" for i, s in enumerate(specs)]
    catalog = build_catalog(parsed_feeds, labels) if parsed_feeds else None
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    status = "success"
    if errors and not parsed_feeds:
        status = "failed"
    elif errors:
        status = "completed_with_errors"

    return {
        "run_id": started_at[:19].replace("-", "").replace(":", "")
                  + f"Z-{int(started * 1000) % 100000:05d}",
        "scenario": scenario,
        "started_at": started_at,
        "elapsed_ms": elapsed_ms,
        "status": status,
        "timeout_seconds": timeout,
        "probe_media": probe_media,
        "feeds": feeds_report,
        "catalog": to_jsonable(catalog) if catalog else {
            "feed_groups": {}, "entries": [], "conflicts": [], "snapshots": []},
        "errors": errors,
    }
