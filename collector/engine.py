"""采集引擎：把“页面取址 → 下载 → 失效识别 → 刷新 → 内容校验”编排为有限流程。

核心约束：
  1. 刷新只在“明确识别链接失效”后发生，且刷新必须重新从页面取得地址；
  2. 每次刷新后核对稳定业务版本，版本变化立即停止（采集对象不能被悄悄替换）；
  3. 刷新次数有硬上限（默认 3），达到上限即停止并说明，绝不无限重试；
  4. 内容校验失败（结构/身份）属于终端错误，刷新不能修复内容问题，不重试。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from collector.base import BaseBackend
from collector.models import AcquiredLink, DownloadResponse
from collector.validation import validate

EventSink = Callable[[dict[str, Any]], None]

# 下载结论
DL_OK = "ok"
DL_EXPIRED = "expired"          # 410 + 过期错误页 / 已消费
DL_ERROR_HTML = "error_html"    # HTTP 200 的错误 HTML
DL_OTHER_HTTP = "other_http"    # 其他非成功状态码
DL_TRANSPORT = "transport"      # 传输层异常

# 运行结果状态
OUT_SUCCESS = "success"
OUT_FAILED_EXPIRED = "failed_expired"
OUT_FAILED_VERSION = "failed_version_changed"
OUT_FAILED_IDENTITY = "failed_identity"
OUT_FAILED_STRUCTURE = "failed_structure"
OUT_FAILED_ACQUIRE = "failed_acquire"
OUT_FAILED_TRANSPORT = "failed_transport"

REFRESHABLE = {DL_EXPIRED, DL_ERROR_HTML, DL_OTHER_HTTP, DL_TRANSPORT}


@dataclass
class RunResult:
    run_id: str
    scenario_id: str
    backend_name: str
    status: str
    message: str
    attempts: int = 0
    refreshes: int = 0
    max_refreshes: int = 3
    events: list[dict[str, Any]] = field(default_factory=list)
    saved_path: str | None = None
    validation: dict | None = None
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def duration_ms(self) -> int:
        return int((self.finished_at - self.started_at) * 1000) if self.finished_at else 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "backend_name": self.backend_name,
            "status": self.status,
            "message": self.message,
            "attempts": self.attempts,
            "refreshes": self.refreshes,
            "max_refreshes": self.max_refreshes,
            "events": self.events,
            "saved_path": self.saved_path,
            "validation": self.validation,
            "duration_ms": self.duration_ms,
        }


def classify_download(resp: DownloadResponse) -> tuple[str, str]:
    """把一次原始下载响应归类为明确的下载结论。"""
    if resp.transport_error:
        return DL_TRANSPORT, resp.transport_error

    ctype = (resp.headers.get("Content-Type") or resp.headers.get("content-type") or "").lower()
    head = resp.content[:512].lstrip()[:200].lower()
    is_html = "html" in ctype or b"<html" in head or b"<!doctype html" in head

    if is_html:
        kind = "error_html" if resp.status_code == 200 else f"http_{resp.status_code}_html"
        if resp.status_code == 200:
            return DL_ERROR_HTML, "HTTP 200 但响应体是 HTML 错误页（依据内容类型识别）"
        if resp.status_code == 410:
            return DL_EXPIRED, "HTTP 410：链接已过期或已被使用（错误 HTML 页面）"
        return DL_OTHER_HTTP, f"HTTP {resp.status_code} 的 HTML 错误页（{kind}）"

    if resp.status_code == 200:
        return DL_OK, "HTTP 200 且内容为非 HTML 二进制，进入内容校验"
    if resp.status_code == 410:
        return DL_EXPIRED, "HTTP 410：链接已过期或一次性链接已被消费"
    if resp.status_code == 404:
        return DL_EXPIRED, "HTTP 404：链接不存在或已被回收，按失效处理"
    return DL_OTHER_HTTP, f"HTTP {resp.status_code}，非预期下载状态"


class CollectionEngine:
    def __init__(self, backend: BaseBackend, downloads_dir: Path,
                 max_refreshes: int = 3, sink: EventSink | None = None) -> None:
        self._backend = backend
        self._downloads_dir = Path(downloads_dir)
        self._downloads_dir.mkdir(parents=True, exist_ok=True)
        self._max_refreshes = max_refreshes
        self._sink: EventSink = sink or (lambda event: None)

    def run(self, scenario_id: str, page_url: str, run_id: str | None = None) -> RunResult:
        result = RunResult(
            run_id=run_id or uuid.uuid4().hex[:12],
            scenario_id=scenario_id,
            backend_name=self._backend.name,
            status=OUT_FAILED_ACQUIRE,
            message="",
            max_refreshes=self._max_refreshes,
            started_at=time.time(),
        )

        def emit(kind: str, message: str, **extra: Any) -> None:
            event = {"seq": len(result.events) + 1, "kind": kind, "message": message,
                     "ts": round(time.time(), 3), **extra}
            result.events.append(event)
            self._sink(event)

        emit("run_started", f"开始采集（后端={self._backend.name}，刷新上限={self._max_refreshes}）",
             page_url=page_url)

        baseline: AcquiredLink | None = None
        try:
            baseline = self._backend.acquire_link(page_url)
        except Exception as exc:
            evidence = getattr(exc, "args", None)
            emit("acquire_failed", f"首次取得地址失败：{exc}",
                 evidence=evidence[1] if evidence and len(evidence) > 1 else None)
            result.status = OUT_FAILED_ACQUIRE
            result.message = str(exc)
            result.finished_at = time.time()
            return result

        emit("link_acquired", "从页面取得短时链接与稳定业务版本", evidence=baseline.evidence,
             version=baseline.version, experiment_id=baseline.experiment_id,
             download_url=baseline.download_url)

        current = baseline
        while True:
            result.attempts += 1
            emit("download_attempt", f"第 {result.attempts} 次下载尝试",
                 attempt=result.attempts, download_url=current.download_url)
            resp = self._backend.download(current.download_url)
            verdict, reason = classify_download(resp)
            emit("download_result", f"下载结论：{verdict} —— {reason}",
                 verdict=verdict, status_code=resp.status_code,
                 content_type=resp.headers.get("Content-Type") or resp.headers.get("content-type"),
                 bytes=len(resp.content), final_url=resp.final_url,
                 transport_error=resp.transport_error)

            if verdict == DL_OK:
                report = validate(
                    resp.content, resp.headers,
                    expected_experiment_id=baseline.experiment_id,
                    expected_version=baseline.version,
                )
                result.validation = report.as_dict()
                emit("validation", "三层校验（类型/结构/业务身份）完成",
                     validation=result.validation)
                if report.passed:
                    saved = self._downloads_dir / f"{result.run_id}_{scenario_id}.zip"
                    saved.write_bytes(resp.content)
                    result.saved_path = str(saved)
                    result.status = OUT_SUCCESS
                    result.message = "下载成功并通过类型、结构与业务身份全部校验"
                    emit("success", f"采集成功，下载物保存至 {saved}", saved_path=str(saved))
                    break

                failed_check = next(c for c in report.checks if not c.passed)
                if failed_check.name == "identity":
                    result.status = OUT_FAILED_IDENTITY
                    result.message = f"业务身份校验失败，终止采集：{failed_check.detail}"
                else:
                    result.status = OUT_FAILED_STRUCTURE
                    result.message = f"内容{failed_check.name}校验失败（终端错误，不刷新）：{failed_check.detail}"
                emit("validation_failed", result.message, failed_check=failed_check.name)
                break

            # 非 OK：只有“可刷新的失效”才允许重新取址
            if verdict not in REFRESHABLE:
                result.status = OUT_FAILED_TRANSPORT
                result.message = f"不可恢复的下载失败：{reason}"
                emit("terminal", result.message)
                break

            if result.refreshes >= self._max_refreshes:
                result.status = OUT_FAILED_EXPIRED
                result.message = (
                    f"链接持续失效（最近结论：{verdict}），已达刷新上限 {self._max_refreshes} 次，"
                    "停止采集，不进行无限重试"
                )
                emit("refresh_limit", result.message,
                     refreshes=result.refreshes, max_refreshes=self._max_refreshes)
                break

            result.refreshes += 1
            emit("refreshing", f"识别链接失效（{verdict}），重新从页面取得地址（第 {result.refreshes} 次刷新）",
                 refresh=result.refreshes)
            try:
                refreshed = self._backend.acquire_link(page_url)
            except Exception as exc:
                result.status = OUT_FAILED_ACQUIRE
                result.message = f"刷新时取得地址失败：{exc}"
                emit("acquire_failed", result.message)
                break

            emit("link_refreshed", "刷新取得新链接，开始核对业务版本与采集对象",
                 evidence=refreshed.evidence, version=refreshed.version,
                 experiment_id=refreshed.experiment_id, download_url=refreshed.download_url)

            # 版本守卫：业务版本变化意味着采集对象已变，立即停止
            if refreshed.version != baseline.version:
                result.status = OUT_FAILED_VERSION
                result.message = (
                    f"刷新后业务版本变化：{baseline.version} → {refreshed.version}；"
                    "当前采集对象已改变，按要求停止采集（不下载新对象）"
                )
                emit("version_changed", result.message,
                     baseline_version=baseline.version, refreshed_version=refreshed.version)
                break
            if refreshed.experiment_id != baseline.experiment_id:
                result.status = OUT_FAILED_VERSION
                result.message = (
                    f"刷新后实验编号变化：{baseline.experiment_id} → {refreshed.experiment_id}，停止采集"
                )
                emit("version_changed", result.message)
                break

            emit("version_confirmed",
                 f"业务版本未变（{refreshed.version}），采集对象一致，使用新链接重试",
                 version=refreshed.version)
            current = refreshed

        result.finished_at = time.time()
        emit("run_finished", f"采集结束：{result.status}（耗时 {result.duration_ms} ms）",
             status=result.status, attempts=result.attempts, refreshes=result.refreshes)
        return result
