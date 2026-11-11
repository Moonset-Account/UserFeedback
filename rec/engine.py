"""分段采集引擎（采集器核心，只经 HTTP 公开接口取数）。

职责边界：
- 输入只有对象 URL、任务工作目录与分块大小；不导入生成器、不读取对象真值；
- 用强校验器（ETag）+ ``If-Range`` 维护区间覆盖图，合并重叠区间；
- 任何版本混杂、Content-Range/Content-Length 不一致、截断、5xx、416 响应
  都只记录证据并丢弃字节，绝不写入覆盖图；
- 覆盖完整后才解析 EXPB，解析失败任务失败，不会产出结果；
- 每次状态变化原子落盘，进程重启后可从断点继续。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from rec import intervals
from rec.expb import ExpbError, parse_expb
from rec.http_client import (
    HttpProtocolError,
    TransportError,
    get_range,
    head,
)

CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
MAX_ATTEMPTS_PER_GAP = 20
MAX_DISCOVERY_ATTEMPTS = 10
BACKOFF_BASE = 0.12
STATE_FILE = "state.json"
OBJECT_FILE = "object.bin"
EVENTS_FILE = "events.jsonl"

TERMINAL_STATUSES = ("complete", "failed", "stopped")


@dataclass
class Event:
    seq: int
    ts: float
    level: str
    type: str
    message: str
    evidence: Dict[str, Any] = field(default_factory=dict)


class Engine:
    def __init__(
        self,
        task_dir: str,
        url: str,
        scenario_label: str,
        chunk_size: int = 2048,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        pause_after_accepts: Optional[int] = None,
    ) -> None:
        self.task_dir = task_dir
        os.makedirs(task_dir, exist_ok=True)
        self.state_path = os.path.join(task_dir, STATE_FILE)
        self.object_path = os.path.join(task_dir, OBJECT_FILE)
        self.events_path = os.path.join(task_dir, EVENTS_FILE)
        self.chunk_size = max(64, int(chunk_size))
        self.event_callback = event_callback
        # 测试钩子：接受该数量个响应后自动暂停，模拟在未完成状态下退出
        self.pause_after_accepts = pause_after_accepts
        self._accepted_responses = 0
        self._lock = threading.Lock()
        self._running = False
        self._wake = threading.Event()
        self._seq = 0
        self._gap_attempts = 0
        self._discover_attempts = 0

        if os.path.exists(self.state_path):
            with open(self.state_path, "r", encoding="utf-8") as fh:
                self.state = json.load(fh)
            # 进程死亡时遗留的 running 一律视为可恢复的暂停态
            if self.state["status"] == "running":
                self.state["status"] = "paused"
            existing = len(self.state.get("version_chain", []))
            self._version_seq = existing
        else:
            self.state = {
                "url": url,
                "scenario": scenario_label,
                "chunk_size": self.chunk_size,
                "status": "paused",
                "expected_length": None,
                "etag": None,
                "last_modified": None,
                "spans": [],
                "version_chain": [],
                "accepted_bytes": 0,
                "rejected_count": 0,
                "request_count": 0,
                "last_error": None,
                "result": None,
                "error": None,
                "started_at": None,
                "updated_at": None,
                "finished_at": None,
            }
            self._version_seq = 0
            self._persist_locked()

    # ------------------------------------------------------------------ 快照
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            snap = json.loads(json.dumps(self.state))
        length = snap["expected_length"]
        spans = [tuple(s) for s in snap["spans"]]
        snap["gaps"] = (
            intervals.gaps(spans, length) if length is not None else []
        )
        snap["covered"] = (
            intervals.covered_count(spans, length) if length is not None else 0
        )
        snap["object_path"] = self.object_path
        return snap

    def read_events(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        if not os.path.exists(self.events_path):
            return []
        with open(self.events_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        if limit:
            lines = lines[-limit:]
        return [json.loads(line) for line in lines if line.strip()]

    # ------------------------------------------------------------- 状态控制
    def pause(self) -> None:
        with self._lock:
            if self.state["status"] not in TERMINAL_STATUSES:
                self.state["status"] = "paused"
                self._persist_locked()
        self._wake.set()

    def stop(self) -> None:
        with self._lock:
            if self.state["status"] not in TERMINAL_STATUSES:
                self.state["status"] = "stopped"
                self.state["finished_at"] = time.time()
                self._persist_locked()
        self._wake.set()

    def run(self) -> None:
        """阻塞执行采集，直到完成、失败、停止或暂停。"""
        with self._lock:
            if self._running:
                return  # 同一引擎不允许并发运行
            if self.state["status"] in TERMINAL_STATUSES:
                return
            self._running = True
            self.state["status"] = "running"
            if self.state["started_at"] is None:
                self.state["started_at"] = time.time()
            self._persist_locked()
        try:
            self._loop()
        except Exception as exc:  # 防御性兜底：异常也要落盘为失败
            with self._lock:
                self.state["status"] = "failed"
                self.state["error"] = f"引擎异常: {exc}"
                self.state["finished_at"] = time.time()
                self._persist_locked()
            self._emit("error", "engine-exception", str(exc))
        finally:
            with self._lock:
                self._running = False

    # ----------------------------------------------------------------- 主循环
    def _loop(self) -> None:
        while True:
            if self._interrupted():
                return
            if self.state["expected_length"] is None:
                if self._discover_attempts >= MAX_DISCOVERY_ATTEMPTS:
                    self._fail(
                        f"HEAD 探测连续 {MAX_DISCOVERY_ATTEMPTS} 次失败，任务终止"
                    )
                    return
                if not self._discover():
                    self._discover_attempts += 1
                    self._sleep(backoff=True)
                    continue
                self._discover_attempts = 0
            gap = intervals.first_gap(
                [tuple(s) for s in self.state["spans"]],
                self.state["expected_length"],
            )
            if gap is None:
                self._finalize()
                return
            if self._gap_attempts >= MAX_ATTEMPTS_PER_GAP:
                self._fail(
                    f"同一缺口连续 {MAX_ATTEMPTS_PER_GAP} 次未能取得合法字节，任务终止",
                )
                return
            response = self._request_gap(gap)
            if response is None:
                self._sleep(backoff=True)
                continue
            self._gap_attempts = 0

    def _interrupted(self) -> bool:
        status = self.state["status"]
        if status in ("paused", "stopped"):
            return True
        return status == "failed"

    def _discover(self) -> bool:
        try:
            resp = head(self.state["url"])
        except (TransportError, HttpProtocolError) as exc:
            self._emit(
                "warn", "head-failed", f"HEAD 探测失败，稍后重试: {exc}",
                {"url": self.state["url"]},
            )
            self._note_error(str(exc))
            return False
        if resp.status != 200:
            self._note_error(f"HEAD 返回非 200 状态: {resp.status}")
            self._emit("warn", "head-bad-status",
                       f"HEAD 返回 {resp.status}，稍后重试",
                       {"status": resp.status})
            return False
        length_text = resp.headers.get("content-length")
        etag = resp.headers.get("etag")
        if not length_text:
            self._fail("源站未返回 Content-Length，无法建立区间覆盖图")
            return False
        if not etag or not etag.startswith('"'):
            self._fail("源站未返回强校验器 ETag，采集器拒绝在无版本保证下工作")
            return False
        length = int(length_text)
        with self._lock:
            # 恢复场景：若已锁定版本与当前对象不一致，按新对象重置覆盖图
            if self.state["etag"] and etag != self.state["etag"]:
                self._reset_for_version_locked(
                    etag, length, "HEAD 发现对象版本已变化，丢弃旧版本区间",
                )
            self.state["expected_length"] = length
            self.state["last_modified"] = resp.headers.get("last-modified")
            if self.state["etag"] is None:
                self.state["etag"] = etag
                self._append_chain_locked(etag, length, "HEAD 锁定初始对象版本")
            self._persist_locked()
        self._emit(
            "info", "discovered",
            f"探测到对象长度 {length} 字节，ETag {etag}",
            {"length": length, "etag": etag,
             "accept_ranges": resp.headers.get("accept-ranges")},
        )
        return True

    def _request_gap(self, gap: tuple[int, int]):
        start, _ = gap
        length = self.state["expected_length"]
        end = min(start + self.chunk_size - 1, length - 1)
        headers = {"Range": f"bytes={start}-{end}"}
        pinned = self.state["etag"]
        if pinned:
            headers["If-Range"] = pinned
        try:
            with self._lock:
                self.state["request_count"] += 1
                self._persist_locked()
            resp = get_range(self.state["url"], start, end, if_range=pinned)
        except (TransportError, HttpProtocolError) as exc:
            self._reject("transport-error", f"请求失败: {exc}",
                         {"request": headers})
            return None
        self._emit(
            "info", "http-response",
            f"收到 HTTP {resp.status} 响应",
            {
                "request": headers,
                "status": resp.status,
                "response_headers": dict(resp.headers),
                "truncated": resp.truncated,
                "bytes_received": len(resp.body),
                "note": resp.note,
            },
        )
        status = resp.status
        if status >= 500:
            self._reject("server-error", f"服务器错误区间 HTTP {status}",
                         {"request": headers, "status": status})
            return None
        if status == 416:
            self._reject("range-unsatisfiable",
                         "服务器返回 416，区间当前不可满足，退避后重试",
                         {"request": headers,
                          "content_range": resp.headers.get("content-range")})
            return None
        if status == 200:
            return self._handle_full(resp, headers)
        if status == 206:
            return self._handle_partial(resp, start, end, headers)
        self._reject("unexpected-status", f"未预期的状态码 {status}",
                     {"request": headers, "status": status})
        return None

    # ------------------------------------------------------------- 响应处理
    def _handle_full(self, resp, headers: Dict[str, str]):
        if resp.truncated:
            self._reject("truncated-full",
                         "200 全量响应在传输中截断，丢弃整段响应",
                         {"request": headers, "bytes_received": len(resp.body),
                          "note": resp.note})
            return None
        new_etag = resp.headers.get("etag")
        if not new_etag:
            self._reject("missing-etag", "200 响应缺少 ETag，无法确认版本",
                         {"request": headers})
            return None
        declared = resp.headers.get("content-length")
        if declared is not None and int(declared) != len(resp.body):
            self._reject("length-mismatch",
                         f"Content-Length={declared} 与实际字节 {len(resp.body)} 不符",
                         {"request": headers, "declared": int(declared),
                          "actual": len(resp.body)})
            return None
        length = len(resp.body)
        pinned = self.state["etag"]
        with self._lock:
            if pinned and new_etag != pinned:
                reason = (
                    "If-Range 未命中：源站以 200 返回新版本对象，"
                    "丢弃旧版本全部区间并按新版本重建覆盖图"
                )
                self._reset_for_version_locked(new_etag, length, reason)
                self._emit("warn", "version-reset", reason,
                           {"old_etag": pinned, "new_etag": new_etag,
                            "new_length": length})
            elif self.state["expected_length"] is None:
                self.state["expected_length"] = length
            elif self.state["expected_length"] != length:
                self._reject_locked(
                    "length-mismatch",
                    f"200 对象长度 {length} 与已探测长度 "
                    f"{self.state['expected_length']} 不一致",
                    {"request": headers},
                )
                return None
            self._write_span_locked(0, resp.body)
            self.state["spans"] = intervals.add(
                [tuple(s) for s in self.state["spans"]], 0, length
            )
            self._count_accepted_locked(len(resp.body))
            self.state["etag"] = new_etag
            self._maybe_pause_after_accept_locked()
            self._persist_locked()
        self._emit("success", "full-accepted",
                   f"接受 200 全量响应 {length} 字节，覆盖图已更新",
                   {"request": headers, "etag": new_etag, "length": length})
        return True

    def _handle_partial(self, resp, req_start: int, req_end: int,
                        headers: Dict[str, str]):
        cr_text = resp.headers.get("content-range")
        match = CONTENT_RANGE_RE.match(cr_text or "")
        if not match:
            self._reject("bad-content-range",
                         f"206 缺少合法 Content-Range: {cr_text!r}",
                         {"request": headers,
                          "content_range": cr_text})
            return None
        cr_start, cr_end, cr_total = (int(x) for x in match.groups())
        pinned = self.state["etag"]
        expected_length = self.state["expected_length"]
        new_etag = resp.headers.get("etag")

        if expected_length is not None and cr_total != expected_length:
            self._reject(
                "inconsistent-total-length",
                f"Content-Range 声明总长度 {cr_total} 与对象真实长度 "
                f"{expected_length} 不一致，拒绝混合字节",
                {"request": headers, "content_range": cr_text,
                 "expected_length": expected_length},
            )
            return None
        if cr_start != req_start:
            self._reject(
                "misaligned-range",
                f"Content-Range 起点 {cr_start} 与请求起点 {req_start} 不一致",
                {"request": headers, "content_range": cr_text},
            )
            return None
        if cr_end < cr_start:
            self._reject("bad-content-range", "Content-Range 终点早于起点",
                         {"request": headers, "content_range": cr_text})
            return None
        if pinned and new_etag and new_etag != pinned:
            self._reject(
                "mixed-version",
                f"206 的 ETag {new_etag} 与锁定版本 {pinned} 不同，"
                "拒绝把新版本字节混入旧版本覆盖图",
                {"request": headers, "content_range": cr_text,
                 "pinned_etag": pinned, "response_etag": new_etag},
            )
            return None
        declared_len = resp.headers.get("content-length")
        want = cr_end - cr_start + 1
        if declared_len is not None and int(declared_len) != want:
            self._reject(
                "bad-content-length",
                f"Content-Length={declared_len} 与 Content-Range 推算长度 {want} 不符",
                {"request": headers, "declared": int(declared_len), "want": want},
            )
            return None
        if resp.truncated or len(resp.body) != want:
            self._reject(
                "truncated-partial",
                f"分段响应不完整：期望 {want} 字节，实际 {len(resp.body)} 字节",
                {"request": headers, "content_range": cr_text,
                 "bytes_received": len(resp.body), "truncated": resp.truncated,
                 "note": resp.note},
            )
            return None
        if not new_etag:
            self._reject("missing-etag", "206 响应缺少 ETag，无法确认版本",
                         {"request": headers})
            return None

        with self._lock:
            if self.state["expected_length"] is None:
                self.state["expected_length"] = cr_total
            if self.state["etag"] is None:
                self.state["etag"] = new_etag
                self._append_chain_locked(new_etag, cr_total,
                                          "首个 206 响应锁定对象版本")
            self._write_span_locked(cr_start, resp.body)
            self.state["spans"] = intervals.add(
                [tuple(s) for s in self.state["spans"]],
                cr_start, cr_start + len(resp.body),
            )
            self._count_accepted_locked(len(resp.body))
            self._maybe_pause_after_accept_locked()
            self._persist_locked()
        self._emit(
            "success", "partial-accepted",
            f"接受区间 [{cr_start}, {cr_end}] 共 {want} 字节",
            {"request": headers, "content_range": cr_text, "etag": new_etag,
             "spans": self.state["spans"]},
        )
        return True

    # ------------------------------------------------------------- 收尾解析
    def _finalize(self) -> None:
        length = self.state["expected_length"]
        if os.path.getsize(self.object_path) != length:
            self._fail("覆盖图声称完整但文件长度与对象长度不一致")
            return
        with open(self.object_path, "rb") as fh:
            data = fh.read()
        try:
            obj = parse_expb(data)
        except ExpbError as exc:
            self._fail(f"完整对象解析失败: {exc}")
            return
        with self._lock:
            self.state["status"] = "complete"
            self.state["result"] = obj.summary
            self.state["finished_at"] = time.time()
            self._persist_locked()
        self._emit(
            "success", "parsed",
            f"对象解析成功：{obj.object_name}，{len(obj.records)} 条实验记录",
            {"summary": obj.summary},
        )

    def _fail(self, message: str) -> None:
        with self._lock:
            self.state["status"] = "failed"
            self.state["error"] = message
            self.state["last_error"] = message
            self.state["finished_at"] = time.time()
            self._persist_locked()
        self._emit("error", "failed", message)

    # ------------------------------------------------------------- 持久化
    def _persist_locked(self) -> None:
        self.state["updated_at"] = time.time()
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    def _write_span_locked(self, offset: int, payload: bytes) -> None:
        mode = "r+b" if os.path.exists(self.object_path) else "wb"
        with open(self.object_path, mode) as fh:
            fh.seek(offset)
            fh.write(payload)

    def _reset_for_version_locked(self, etag: str, length: int,
                                  reason: str) -> None:
        self.state["etag"] = etag
        self.state["expected_length"] = length
        self.state["spans"] = []
        self.state["accepted_bytes"] = 0
        self._gap_attempts = 0
        self._discover_attempts = 0
        # 截断旧版本字节，杜绝新旧版本在同一文件中混杂
        with open(self.object_path, "wb"):
            pass
        self._append_chain_locked(etag, length, reason)

    def _append_chain_locked(self, etag: str, length: int,
                             reason: str) -> None:
        self._version_seq += 1
        self.state["version_chain"].append({
            "seq": self._version_seq,
            "etag": etag,
            "length": length,
            "reason": reason,
            "seen_at": time.time(),
        })

    def _count_accepted_locked(self, n: int) -> None:
        self.state["accepted_bytes"] += n

    def _maybe_pause_after_accept_locked(self) -> None:
        self._accepted_responses += 1
        if (
            self.pause_after_accepts is not None
            and self._accepted_responses >= self.pause_after_accepts
        ):
            self.state["status"] = "paused"
            self._emit(
                "info", "auto-paused",
                f"测试钩子：已接受 {self._accepted_responses} 个响应，自动暂停",
            )

    def _note_error(self, message: str) -> None:
        with self._lock:
            self.state["last_error"] = message
            self._persist_locked()

    def _reject(self, etype: str, message: str,
                evidence: Dict[str, Any]) -> None:
        with self._lock:
            self._reject_locked(etype, message, evidence)

    def _reject_locked(self, etype: str, message: str,
                       evidence: Dict[str, Any]) -> None:
        self.state["rejected_count"] += 1
        self.state["last_error"] = message
        self._gap_attempts += 1
        self._persist_locked()
        self._emit("warn", etype, f"已拒绝响应: {message}", evidence)

    def _emit(self, level: str, etype: str, message: str,
              evidence: Optional[Dict[str, Any]] = None) -> None:
        self._seq += 1
        event = {
            "seq": self._seq,
            "ts": time.time(),
            "level": level,
            "type": etype,
            "message": message,
            "evidence": evidence or {},
        }
        with open(self.events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        if self.event_callback:
            try:
                self.event_callback(event)
            except Exception:
                pass

    def _sleep(self, backoff: bool = False) -> None:
        delay = BACKOFF_BASE * min(self._gap_attempts + 1, 6)
        self._wake.wait(timeout=delay)
        self._wake.clear()
