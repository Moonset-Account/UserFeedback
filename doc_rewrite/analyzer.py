"""乱序事件分析器：识别进程链批量改写用户文档的行为。

判定语义（与 README 一致）：

1. **文件身份** 以可信采集端给出的 ``file_id`` 为准，重命名/硬链接只更新
   路径别名，绝不按路径猜测归并身份；同一 ``file_id`` 始终是同一份文档。
2. **进程世代** 以 spawn 事件维护父->子关系，沿派生链回溯到链根；
   同一派生链对同一文件的多次写入只产生一份受影响文档记录。
3. **风险成立条件**：关闭事件确认内容从“已登记文档类别”变为“高熵未知
   内容”——必须存在携带登记类写前证据的写入，且关闭时熵区间下界达到
   策略阈值。仅关闭后确认才计数，写入时不提前计数。
4. **新建归档排除**：新建文件写入归档类别且保留源文档（归档事件引用了
   仍存在的源 file_id），不计原文档改写，进入 ``excluded`` 清单。
5. **缺少写前证据**：已存在文件被改成高熵未知但没有登记类写前证据，
   不进入风险计数，单列 ``unknown_files``，与身份冲突分表呈现。
6. **窗口发现**：在 ``window_seconds`` 窗口内，不同文档数与独立目录数
   同时达到策略阈值才产生发现，并附最小支持事件集合；水位线未越过
   “窗口内最晚关闭时间 + 整个窗口跨度”前为暂定（tentative），
   越过之后为确认（confirmed）——只有整个跨度都不可能再有事件到达，
   暂定才能升格。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from doc_rewrite.events import EntropyRange, Event, EventType
from doc_rewrite.policy import Policy

# 发现状态
TENTATIVE = "tentative"
CONFIRMED = "confirmed"

# 未知/排除原因
REASON_MISSING_PRE_EVIDENCE = "missing_pre_evidence"
REASON_NEW_FILE = "new_file_no_registered_pre"
REASON_NEW_ARCHIVE = "new_archive_source_retained"
REASON_ENTROPY_INCONCLUSIVE = "entropy_inconclusive"


@dataclass
class _FileCtx:
    """单个 file_id 的累计状态。"""

    file_id: str
    current_path: Optional[str] = None
    writes: list[Event] = field(default_factory=list)
    unknown_high_closes: list[Event] = field(default_factory=list)
    # 类别为 unknown 但熵区间无法证实高熵（缺下界）的关闭：不可静默丢弃。
    unknown_inconclusive_closes: list[Event] = field(default_factory=list)
    identity_events: list[Event] = field(default_factory=list)
    seen_new_file: bool = False
    archived_source: Optional[str] = None
    first_seen_ts: Optional[float] = None


@dataclass
class _RiskRecord:
    """一条“某派生链改写了某文档”的受影响记录。"""

    file_id: str
    chain_root: str
    pids: list[str]
    gens: list[int]
    directory: str
    path: str
    first_write_ts: float
    close_ts: float
    close_seq: int
    modification_count: int
    support_seqs: list[int]
    identity_seqs: list[int]

    @property
    def key(self) -> str:
        return f"{self.chain_root}|{self.file_id}"


class RewriteAnalyzer:
    """事件分析器。

    用法::

        analyzer = RewriteAnalyzer(Policy.from_dict(policy_dict))
        for event in out_of_order_events:
            analyzer.ingest(event)
        snapshot = analyzer.snapshot()

    事件允许乱序到达；内部按 (时间, 序号) 重排后再做状态推进。
    水位线既可由流内 ``watermark`` 事件驱动，也可由
    :meth:`advance_watermark` 显式推进。
    """

    def __init__(self, policy: Optional[Policy] = None) -> None:
        self.policy = policy or Policy()
        # (事件, 送达瞬间的水位线)；水位线 None 表示当时尚未建立。
        # 每次快照都从这份送达日志按 (ts, seq) 重放重建全部状态，
        # 因此乱序到达、迟到补到达都能得到确定且可复核的结果。
        self._ingested: list[tuple[Event, Optional[float]]] = []
        self.watermark: Optional[float] = None
        self.max_event_ts: Optional[float] = None
        self.late_events = 0
        self.watermark_regressions = 0
        self._dirty = True
        self._cached: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------------
    # 输入侧
    # ------------------------------------------------------------------
    def ingest(self, event: Event) -> None:
        """接收一条事件（允许乱序）。"""
        if not isinstance(event, Event):
            raise TypeError("ingest 只接受 Event 实例")
        if event.etype is EventType.WATERMARK:
            self._apply_watermark(event.ts)
            self._note_ts(event.ts)
            return

        # 迟到：送达瞬间水位线已越过该事件时间。事件仍照常参与分析，
        # 审计需要完整事实，但单独计数以便区分“补到达”与正常流。
        if self.watermark is not None and event.ts < self.watermark:
            self.late_events += 1
        self._ingested.append((event, self.watermark))
        self._note_ts(event.ts)
        self._dirty = True

    def advance_watermark(self, ts: float) -> None:
        """显式推进水位线（不会回退）。"""
        self._apply_watermark(float(ts))

    def _apply_watermark(self, ts: float) -> None:
        if self.watermark is None or ts >= self.watermark:
            self.watermark = float(ts)
        else:
            self.watermark_regressions += 1
        self._dirty = True

    def _note_ts(self, ts: float) -> None:
        if self.max_event_ts is None or ts > self.max_event_ts:
            self.max_event_ts = float(ts)

    # ------------------------------------------------------------------
    # 输出侧
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """返回当前可 JSON 序列化的完整分析快照。"""
        if self._dirty or self._cached is None:
            self._cached = self._build_snapshot()
            self._dirty = False
        return self._cached

    # ------------------------------------------------------------------
    # 内部状态推进
    # ------------------------------------------------------------------
    def _build_snapshot(self) -> dict[str, Any]:
        # 全部状态从送达日志重放构建：乱序事件按 (时间, 序号) 规范化，
        # 快照是纯函数式结果，重复调用或在补到达后再调用都互不污染。
        files: dict[str, _FileCtx] = {}
        path_claims: dict[str, list[tuple[str, int, float, str, Optional[str]]]] = {}
        conflicts: set[str] = set()
        parent: dict[str, str] = {}
        gen: dict[str, int] = {}

        ordered = sorted(
            (event for event, _wm in self._ingested),
            key=lambda e: (e.ts, e.seq),
        )
        for event in ordered:
            self._dispatch(event, files, path_claims, conflicts, parent, gen)

        records, excluded, unknowns = self._build_records(files, parent, gen)
        findings = self._build_findings(records)
        conflict_list = self._build_conflicts(path_claims, conflicts)

        qualified_fids = {r.file_id for r in records}
        stats = {
            "events_ingested": len(self._ingested),
            "distinct_files": len(files),
            "affected_documents": len(qualified_fids),
            "affected_records": len(records),
            "findings_total": len(findings),
            "findings_confirmed": sum(1 for f in findings if f["status"] == CONFIRMED),
            "findings_tentative": sum(1 for f in findings if f["status"] == TENTATIVE),
            "unknown_files": len(unknowns),
            "identity_conflicts": len(conflict_list),
            "excluded_entries": len(excluded),
            "late_events": self.late_events,
            "watermark_regressions": self.watermark_regressions,
        }
        return {
            "watermark": self.watermark,
            "max_event_ts": self.max_event_ts,
            "policy": {
                "window_seconds": self.policy.window_seconds,
                "min_documents": self.policy.min_documents,
                "min_distinct_dirs": self.policy.min_distinct_dirs,
                "high_entropy_threshold": self.policy.high_entropy_threshold,
                "registered_categories": sorted(self.policy.registered_categories),
                "unknown_category": self.policy.unknown_category,
                "archive_categories": sorted(self.policy.archive_categories),
            },
            "findings": findings,
            "affected_records": [self._record_dict(r) for r in records],
            "unknown_files": unknowns,
            "identity_conflicts": conflict_list,
            "excluded": excluded,
            "stats": stats,
        }

    def _dispatch(
        self,
        event: Event,
        files: dict[str, _FileCtx],
        path_claims: dict[str, list[tuple[str, int, float, str, Optional[str]]]],
        conflicts: set[str],
        parent: dict[str, str],
        gen: dict[str, int],
    ) -> None:
        etype = event.etype
        if etype is EventType.SPAWN:
            if event.pid is None or event.ppid is None:
                raise ValueError(f"spawn 事件缺少 pid/ppid: seq={event.seq}")
            # 同一进程重复 spawn 声明不一致时记录为世代冲突，不覆盖。
            if event.pid in parent and parent[event.pid] != event.ppid:
                conflicts.add(f"spawn:{event.pid}")
            parent.setdefault(event.pid, event.ppid)
            if event.gen is not None:
                gen[event.pid] = event.gen
            return

        if event.file_id is None:
            raise ValueError(f"非 spawn/watermark 事件缺少 file_id: seq={event.seq}")
        ctx = files.get(event.file_id)
        if ctx is None:
            ctx = _FileCtx(file_id=event.file_id)
            files[event.file_id] = ctx
        if ctx.first_seen_ts is None:
            ctx.first_seen_ts = event.ts

        if etype is EventType.OPEN:
            ctx.current_path = event.path or ctx.current_path
            self._claim_path(event, path_claims, conflicts)
        elif etype is EventType.WRITE:
            if event.new_file:
                ctx.seen_new_file = True
            if event.archived_source is not None:
                ctx.archived_source = event.archived_source
            ctx.writes.append(event)
            if event.path:
                ctx.current_path = event.path
                self._claim_path(event, path_claims, conflicts)
        elif etype is EventType.CLOSE:
            if event.post_category == self.policy.unknown_category:
                if self._entropy_is_high(event.entropy):
                    ctx.unknown_high_closes.append(event)
                else:
                    ctx.unknown_inconclusive_closes.append(event)
            if event.path:
                ctx.current_path = event.path
                self._claim_path(event, path_claims, conflicts)
        elif etype is EventType.RENAME:
            ctx.identity_events.append(event)
            # 旧路径仍归该 file_id，新路径登记别名；两边都做冲突检测。
            self._claim_path(event, path_claims, conflicts)
            if event.new_path:
                ctx.current_path = event.new_path
                alias = Event(
                    seq=event.seq,
                    ts=event.ts,
                    etype=EventType.RENAME,
                    file_id=event.file_id,
                    path=event.new_path,
                    pid=event.pid,
                    gen=event.gen,
                )
                self._claim_path(alias, path_claims, conflicts)
        elif etype is EventType.HARDLINK:
            ctx.identity_events.append(event)
            if event.new_path:
                alias = Event(
                    seq=event.seq,
                    ts=event.ts,
                    etype=EventType.HARDLINK,
                    file_id=event.file_id,
                    path=event.new_path,
                    pid=event.pid,
                    gen=event.gen,
                )
                self._claim_path(alias, path_claims, conflicts)
        elif etype is EventType.UNLINK:
            self._claim_path(event, path_claims, conflicts)
        else:  # pragma: no cover - 枚举已穷尽
            raise ValueError(f"未支持的事件类型: {etype}")

    def _claim_path(
        self,
        event: Event,
        path_claims: dict[str, list[tuple[str, int, float, str, Optional[str]]]],
        conflicts: set[str],
    ) -> None:
        if not event.path or event.file_id is None:
            return
        claims = path_claims.setdefault(event.path, [])
        for fid, _seq, _ts, _etype, _pid in claims:
            if fid != event.file_id:
                # 身份冲突：两个不同 file_id 占用同一路径。
                conflicts.add(event.path)
        claims.append(
            (event.file_id, event.seq, event.ts, event.etype.value, event.pid)
        )

    # ------------------------------------------------------------------
    # 风险判定
    # ------------------------------------------------------------------
    def _entropy_is_high(self, entropy: Optional[EntropyRange]) -> bool:
        if entropy is None or entropy.low is None:
            return False
        return entropy.low >= self.policy.high_entropy_threshold

    def _all_closes(self, ctx: _FileCtx) -> list[Event]:
        # unknown_high_closes 只收高熵未知关闭；归档类别关闭需要另查全部事件。
        return [
            event
            for event, _wm in self._ingested
            if event.etype is EventType.CLOSE and event.file_id == ctx.file_id
        ]

    def _path_is_archive(self, path: Optional[str]) -> bool:
        if not path:
            return False
        lowered = path.lower()
        return any(lowered.endswith(suffix) for suffix in self.policy.archive_path_suffixes)

    def _is_archive(self, ctx: _FileCtx, close_event: Event) -> bool:
        category = close_event.post_category
        if category in self.policy.archive_categories:
            return True
        return self._path_is_archive(close_event.path or ctx.current_path)

    def _handle_new_archive(
        self,
        ctx: _FileCtx,
        close_event: Event,
        excluded: list[dict[str, Any]],
        files: dict[str, _FileCtx],
    ) -> bool:
        """新建归档且源文档保留时登记排除，返回是否已处理该文件。"""
        source_retained = (
            ctx.archived_source is not None and ctx.archived_source in files
        )
        if ctx.seen_new_file and self._is_archive(ctx, close_event) and source_retained:
            excluded.append(
                {
                    "file_id": ctx.file_id,
                    "reason": REASON_NEW_ARCHIVE,
                    "path": close_event.path or ctx.current_path,
                    "close_seq": close_event.seq,
                    "close_ts": close_event.ts,
                    "archived_source": ctx.archived_source,
                }
            )
            return True
        return False

    def _build_records(
        self,
        files: dict[str, _FileCtx],
        parent: dict[str, str],
        gen: dict[str, int],
    ) -> tuple[list[_RiskRecord], list[dict[str, Any]], list[dict[str, Any]]]:
        records: list[_RiskRecord] = []
        excluded: list[dict[str, Any]] = []
        unknowns: list[dict[str, Any]] = []
        seen_keys: set[str] = set()

        for fid in sorted(files):
            ctx = files[fid]
            # 归档类关闭单独评估“新建归档且保留源文档”排除；
            # 只有高熵未知关闭才进入风险/未知判定管线。
            archive_closes = [
                e
                for e in self._all_closes(ctx)
                if e.post_category in self.policy.archive_categories
                or self._path_is_archive(e.path or ctx.current_path)
            ]
            if not ctx.unknown_high_closes:
                if archive_closes and self._handle_new_archive(
                    ctx,
                    min(archive_closes, key=lambda e: (e.ts, e.seq)),
                    excluded,
                    files,
                ):
                    continue
                if ctx.unknown_inconclusive_closes:
                    # unknown 类别但熵区间无法证实高熵：审计上不可静默忽略。
                    ev = min(ctx.unknown_inconclusive_closes,
                             key=lambda e: (e.ts, e.seq))
                    unknowns.append(
                        {
                            "file_id": fid,
                            "reason": REASON_ENTROPY_INCONCLUSIVE,
                            "path": ev.path or ctx.current_path,
                            "directory": self._directory_of(
                                ev.path or ctx.current_path
                            ),
                            "first_unknown_close_seq": ev.seq,
                            "first_unknown_close_ts": ev.ts,
                            "observed_pids": sorted(
                                {e.pid for e in (*ctx.writes, ev) if e.pid}
                            ),
                            "write_events": len(ctx.writes),
                            "pre_categories_seen": sorted(
                                {w.pre_category for w in ctx.writes
                                 if w.pre_category}
                            ),
                            "entropy_range": (
                                ev.entropy.to_dict() if ev.entropy else None
                            ),
                        }
                    )
                continue
            close_event = min(ctx.unknown_high_closes, key=lambda e: (e.ts, e.seq))
            registered_writes = [
                w for w in ctx.writes if w.pre_category in self.policy.registered_categories
            ]
            registered_writes.sort(key=lambda e: (e.ts, e.seq))

            if self._handle_new_archive(ctx, close_event, excluded, files):
                continue

            if not registered_writes:
                # 没有登记类写前证据：新建文件与“缺证据的既有文件”分开列。
                if ctx.seen_new_file:
                    excluded.append(
                        {
                            "file_id": fid,
                            "reason": REASON_NEW_FILE,
                            "path": close_event.path or ctx.current_path,
                            "close_seq": close_event.seq,
                            "close_ts": close_event.ts,
                        }
                    )
                else:
                    unknowns.append(
                        {
                            "file_id": fid,
                            "reason": REASON_MISSING_PRE_EVIDENCE,
                            "path": close_event.path or ctx.current_path,
                            "directory": self._directory_of(
                                close_event.path or ctx.current_path
                            ),
                            "first_unknown_close_seq": close_event.seq,
                            "first_unknown_close_ts": close_event.ts,
                            "observed_pids": sorted(
                                {
                                    e.pid
                                    for e in (*ctx.writes, close_event)
                                    if e.pid
                                }
                            ),
                            "write_events": len(ctx.writes),
                            "pre_categories_seen": sorted(
                                {
                                    w.pre_category
                                    for w in ctx.writes
                                    if w.pre_category
                                }
                            ),
                            "entropy_range": (
                                close_event.entropy.to_dict()
                                if close_event.entropy
                                else None
                            ),
                        }
                    )
                continue

            # 按派生链根分组：同一链对同一文件只计一份受影响文档。
            root_to_writes: dict[str, list[Event]] = {}
            for write in registered_writes:
                pid = write.pid or close_event.pid or "unknown"
                root = self._chain_root(pid, parent)
                root_to_writes.setdefault(root, []).append(write)

            close_pid = close_event.pid or "unknown"
            close_root = self._chain_root(close_pid, parent)
            # 关闭进程所在链若没有登记类写入，也单独成一条（关闭确认是它做的）。
            root_to_writes.setdefault(close_root, [])

            for root, writes in sorted(root_to_writes.items()):
                key = f"{root}|{fid}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                involved_pids = sorted(
                    {w.pid for w in writes if w.pid} | {close_pid}
                )
                support = self._support_seqs(ctx, writes, close_event)
                first_ts = min((w.ts for w in writes), default=close_event.ts)
                records.append(
                    _RiskRecord(
                        file_id=fid,
                        chain_root=root,
                        pids=involved_pids,
                        gens=sorted(
                            {gen[pid] for pid in involved_pids if pid in gen}
                        ),
                        directory=self._directory_of(
                            close_event.path or ctx.current_path
                        ),
                        path=close_event.path or ctx.current_path or "",
                        first_write_ts=first_ts,
                        close_ts=close_event.ts,
                        close_seq=close_event.seq,
                        modification_count=len(writes),
                        support_seqs=support,
                        identity_seqs=self._identity_seqs(ctx),
                    )
                )

        records.sort(key=lambda r: (r.close_ts, r.close_seq, r.key))
        return records, excluded, unknowns

    def _support_seqs(
        self, ctx: _FileCtx, writes: list[Event], close_event: Event
    ) -> list[int]:
        """最小支持事件集合：最早一条登记类写前证据 + 关闭确认事件。

        同一 (链, 文件) 的后续写入（含水位线后的补到达）只增加
        ``modification_count``，不进入最小集合——去掉最早写入或关闭确认，
        “改写成立”这一事实就无法证实；去掉其余写入则不影响结论。
        重命名/硬链接属于身份轨迹，单独经 identity_event_seqs 呈现。
        """
        seqs = {close_event.seq}
        if writes:
            earliest = min(writes, key=lambda e: (e.ts, e.seq))
            seqs.add(earliest.seq)
        return sorted(seqs)

    def _identity_seqs(self, ctx: _FileCtx) -> list[int]:
        return sorted(e.seq for e in ctx.identity_events)

    def _chain_root(self, pid: str, parent: dict[str, str]) -> str:
        """沿父链回溯到根；环保护，未登记父信息的进程自身即根。"""
        seen: set[str] = set()
        cur = pid
        while cur in parent and cur not in seen:
            seen.add(cur)
            cur = parent[cur]
        return cur

    def _directory_of(self, path: Optional[str]) -> str:
        if not path:
            return ""
        normalized = path.rstrip("/")
        if "/" not in normalized:
            return "."
        parts = [p for p in normalized.split("/") if p]
        if not parts:
            return "/"
        # 最后一段是文件名，其余为父目录；dir_depth=0 保留完整父目录路径，
        # k>0 时只保留最后 k 段（粗粒度聚合用），绝不跨 file_id 猜测身份。
        parent_parts = parts[:-1]
        if not parent_parts:
            return "/"
        if self.policy.dir_depth > 0:
            parent_parts = parent_parts[-self.policy.dir_depth :]
        return "/" + "/".join(parent_parts)

    # ------------------------------------------------------------------
    # 窗口发现
    # ------------------------------------------------------------------
    def _build_findings(self, records: list[_RiskRecord]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        if not records:
            return findings
        window = self.policy.window_seconds
        used: set[str] = set()
        idx = 0
        finding_no = 0
        n = len(records)

        while idx < n:
            anchor = records[idx]
            if anchor.key in used:
                idx += 1
                continue
            end = anchor.close_ts + window
            group = [
                r
                for r in records
                if r.key not in used
                and anchor.close_ts <= r.close_ts <= end
            ]
            distinct_files = {r.file_id for r in group}
            distinct_dirs = {r.directory for r in group}
            if (
                len(distinct_files) >= self.policy.min_documents
                and len(distinct_dirs) >= self.policy.min_distinct_dirs
            ):
                finding_no += 1
                for r in group:
                    used.add(r.key)
                max_close_ts = max(r.close_ts for r in group)
                # 只有水位线越过“窗口内最晚关闭 + 整个窗口跨度”，才能确认
                # 不会再有同窗口事件到达；此前一律暂定。
                ready_ts = max_close_ts + window
                status = (
                    CONFIRMED
                    if self.watermark is not None and self.watermark >= ready_ts
                    else TENTATIVE
                )
                support_seqs = sorted(
                    {seq for r in group for seq in r.support_seqs}
                )
                findings.append(
                    {
                        "finding_id": f"F{finding_no:03d}",
                        "status": status,
                        "window_start": anchor.close_ts,
                        "window_end": max_close_ts,
                        "window_seconds": window,
                        "confirmation_ready_ts": ready_ts,
                        "anchor_ts": anchor.close_ts,
                        "document_count": len(distinct_files),
                        "distinct_directories": len(distinct_dirs),
                        "directories": sorted(distinct_dirs),
                        "file_ids": sorted(distinct_files),
                        "chain_roots": sorted({r.chain_root for r in group}),
                        "record_keys": sorted(r.key for r in group),
                        "support_event_seqs": support_seqs,
                        "support_size": len(support_seqs),
                        "threshold_documents": self.policy.min_documents,
                        "threshold_directories": self.policy.min_distinct_dirs,
                    }
                )
                # 非重叠：跳到窗口之后的第一条未用记录。
                idx = next(
                    (
                        i
                        for i in range(idx, n)
                        if records[i].close_ts > end and records[i].key not in used
                    ),
                    n,
                )
            else:
                idx += 1
        return findings

    # ------------------------------------------------------------------
    # 身份冲突
    # ------------------------------------------------------------------
    def _build_conflicts(
        self,
        path_claims: dict[str, list[tuple[str, int, float, str, Optional[str]]]],
        conflicts: set[str],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for path in sorted(conflicts):
            claims = path_claims.get(path, [])
            distinct = sorted({fid for fid, *_ in claims})
            if len(distinct) < 2 and not path.startswith("spawn:"):
                continue
            result.append(
                {
                    "path": path,
                    "distinct_file_ids": distinct,
                    "claims": [
                        {
                            "file_id": fid,
                            "seq": seq,
                            "ts": ts,
                            "etype": etype,
                            "pid": pid,
                        }
                        for fid, seq, ts, etype, pid in claims
                    ],
                    "reason": (
                        "spawn_parent_conflict"
                        if path.startswith("spawn:")
                        else "distinct_file_ids_on_same_path"
                    ),
                }
            )
        return result

    def _record_dict(self, record: _RiskRecord) -> dict[str, Any]:
        return {
            "record_key": record.key,
            "file_id": record.file_id,
            "chain_root": record.chain_root,
            "pids": record.pids,
            "generations": record.gens,
            "directory": record.directory,
            "path": record.path,
            "first_write_ts": record.first_write_ts,
            "close_ts": record.close_ts,
            "close_seq": record.close_seq,
            "modification_count": record.modification_count,
            "support_event_seqs": record.support_seqs,
            "identity_event_seqs": record.identity_seqs,
        }
