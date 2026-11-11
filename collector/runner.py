"""任务编排：扫描公开目录 → 以首次出现顺序形成唯一 ID 流 → 蓄水池抽样。

关键边界语义（验收点）：
* 去重发生在抽样之前：同一 ID 的重复出现不会再次进入抽样器，
  因此重复条目不增加任何 ID 的入样机会（tests 中有独立断言）。
* 预算耗尽 / 采集中断时，结果声明严格限定为"已观察集合"，
  绝不冒称全站无偏样本；正常扫完时也只声明对该目录端点观察到的总体无偏。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from typing import Any, Callable

from .reservoir import ReservoirSampler, SamplingResult
from .scanner import CatalogScanner, PageEvent, ScanError, ScanReport, StopEvent

DECLARATIONS: dict[str, str] = {
    "completed": (
        "已完整扫描该目录端点的全部页面。本样本对实际观察到的 {n} 个唯一 ID 总体等概率无偏；"
        "若站点还存在该端点之外的条目，本结论不自动覆盖它们。"
    ),
    "budget_exhausted": (
        "请求预算在扫描中途耗尽，目录未扫完。本样本仅对预算内已观察到的 {n} 个唯一 ID 等概率，"
        "不代表、也不能冒称为全站无偏样本；未抓取的下一页为：{next_url}"
    ),
    "request_error": (
        "采集因请求错误中断。本样本仅覆盖中断前已观察到的 {n} 个唯一 ID，"
        "不构成全站无偏样本；错误：{detail}"
    ),
    "protocol_error": (
        "采集因协议不受支持/响应无法解析而中断。本样本仅覆盖中断前已观察到的 {n} 个唯一 ID，"
        "不构成全站无偏样本；错误：{detail}"
    ),
}


@dataclass
class TaskResult:
    task_id: str
    params: dict[str, Any]
    status: str                      # ok | stopped | failed
    stop_reason: str
    declaration: str
    scan: dict[str, Any]
    sampling: dict[str, Any] | None
    # 最终样本的来源页证据：样本 ID -> 首次出现的页号/URL/详情链接
    sample_sources: list[dict[str, Any]]
    observed_unique_count: int
    duplicate_count: int
    raw_entry_count: int
    requests_used: int
    budget: int
    error: str | None
    started_at: float
    finished_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _build_sample_sources(sampling: SamplingResult, scan: ScanReport) -> list[dict[str, Any]]:
    """为每个入样 ID 回溯其首次出现的页号与 URL（来源证据）。"""
    id_to_record = {r.specimen_id: r for r in scan.ordered_unique_records}
    id_to_page: dict[str, PageEvent] = {}
    for page in scan.pages:
        for rec in page.records:
            if rec.specimen_id not in id_to_page:
                id_to_page[rec.specimen_id] = page

    sources: list[dict[str, Any]] = []
    for sid in sampling.sample:
        rec = id_to_record.get(sid)
        page = id_to_page.get(sid)
        sources.append(
            {
                "specimen_id": sid,
                "title": rec.title if rec else None,
                "source_page": page.page_index if page else None,
                "source_page_url": page.url if page else None,
                "detail_url": rec.detail_url if rec else None,
            }
        )
    return sources


def run_sampling_task(
    task_id: str,
    start_url: str,
    seed: int,
    sample_size: int,
    request_budget: int,
    *,
    scanner: CatalogScanner | None = None,
    timeout: float = 8.0,
    max_retries: int = 2,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> TaskResult:
    """执行一次完整任务。scanner 可注入（测试用假扫描器/浏览器扫描器）。"""
    started = time.monotonic()
    params = {
        "start_url": start_url,
        "seed": seed,
        "sample_size": sample_size,
        "request_budget": request_budget,
        "transport": "browser" if scanner is not None and type(scanner).__name__ == "BrowserCatalogScanner" else "http",
    }

    def emit(stage: str, payload: dict[str, Any]) -> None:
        if progress_callback:
            progress_callback(stage, payload)

    scanner = scanner or CatalogScanner(
        start_url, request_budget, timeout=timeout, max_retries=max_retries,
        event_callback=lambda evt: emit("page", {"page_index": evt.page_index, "requests_used": evt.requests_used}),
    )

    try:
        scan = scanner.scan()
    except ScanError as exc:
        finished = time.monotonic()
        return TaskResult(
            task_id=task_id, params=params, status="failed", stop_reason="request_error",
            declaration=DECLARATIONS["request_error"].format(n=0, detail=str(exc)),
            scan={"pages": [], "stop": asdict(StopEvent(
                reason="request_error", detail=str(exc), last_page_index=0,
                requests_used=getattr(scanner, "_requests_used", 0), budget=request_budget)),
                "ordered_unique_records": [], "total_raw_entries": 0},
            sampling=None, sample_sources=[], observed_unique_count=0, duplicate_count=0,
            raw_entry_count=0, requests_used=getattr(scanner, "_requests_used", 0),
            budget=request_budget, error=str(exc), started_at=started, finished_at=finished,
        )

    emit("scan_done", {"unique": len(scan.ordered_unique_records), "reason": scan.stop.reason})

    sampler = ReservoirSampler(seed=seed, sample_size=sample_size)
    for rec in scan.ordered_unique_records:  # 唯一 ID 流：重复条目此前已被剔除
        sampler.offer(rec.specimen_id)
    sampling = sampler.finish()

    stop: StopEvent = scan.stop
    if stop.reason == "completed":
        declaration = DECLARATIONS["completed"].format(n=sampling.observed_unique)
        status = "ok"
    else:
        declaration = DECLARATIONS.get(stop.reason, DECLARATIONS["request_error"]).format(
            n=sampling.observed_unique,
            next_url=getattr(stop, "next_url_not_fetched", None) or "（无）",
            detail=stop.detail,
        )
        status = "stopped" if stop.reason == "budget_exhausted" else "failed"

    finished = time.monotonic()
    result = TaskResult(
        task_id=task_id,
        params=params,
        status=status,
        stop_reason=stop.reason,
        declaration=declaration,
        scan=scan.to_dict(),
        sampling=sampling.to_dict(),
        sample_sources=_build_sample_sources(sampling, scan),
        observed_unique_count=sampling.observed_unique,
        duplicate_count=scan.total_raw_entries - sampling.observed_unique,
        raw_entry_count=scan.total_raw_entries,
        requests_used=stop.requests_used,
        budget=request_budget,
        error=None if status != "failed" else stop.detail,
        started_at=started,
        finished_at=finished,
    )
    emit("done", {"task_id": task_id, "status": status})
    return result
