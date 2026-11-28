"""分段字节流帧归并状态机。

设计要点
========

1. **推迟确认**：版本未确认时，归并器不在任何候选帧边界上提交输出，而是
   在每个数据定界符处按帧目录评估全部版本，把所有通过形式约束的边界作为
   “可证实假设”保留（:meth:`FrameMerger.pending_hypotheses` 可观测）。
   未确认期只移动扫描游标、不删除候选帧字节；控制包到达后对整个缓冲按
   唯一版本重新解析并一次性释放，期间产生的帧与版本事件带有
   ``deferred=True`` 标记。
2. **局部恢复**：校验/对齐失败只丢弃从坏标记到下一个定界符之间的字节并
   发出断续事件，已发布事件（含序号）永不回退、永不改写。
3. **有限缓冲**：未确认积压超过 ``max_buffer`` 时，从最早字节丢弃到最后
   一个定界符起点，发出 ``buffer_overflow`` 断续；缓冲中只存放未发布字节，
   因此已发布帧不可能被回退。
4. **流结束**：:meth:`FrameMerger.flush` 处理尾部，未确认且多版本证据并存
   时拒绝猜测，发出 ``truncated_ambiguous``；仅存在单版本证据时按该版本
   推断释放并显式标注 ``flush_inferred``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import (
    CONTROL_MARKER,
    FRAME_MARKER,
    INCOMPLETE,
    REJECTED,
    FrameCatalog,
    ParseOutcome,
)


class DiscontinuityReason:
    """断续原因常量（字符串枚举，便于日志与断言直接比较）。"""

    #: 重同步跳过的无主字节（定界符前噪声、坏控制包等）。
    RESYNC_SKIPPED = "resync_skipped"
    #: 帧校验和失败后局部恢复。
    CHECKSUM_MISMATCH = "checksum_mismatch"
    #: 帧本身有效但测点载荷存在凑不满 3 字节的残段。
    MALFORMED_PAYLOAD = "malformed_payload"
    #: 未确认积压超过缓冲上限，丢弃早期未发布字节。
    BUFFER_OVERFLOW = "buffer_overflow"
    #: 流结束时尾部字节不足以成帧。
    TRUNCATED = "truncated"
    #: 流结束时多版本边界仍并存，无法唯一解帧。
    TRUNCATED_AMBIGUOUS = "truncated_ambiguous"
    #: 流结束时仅单版本证据，按推断版本释放（非控制包确认）。
    FLUSH_INFERRED = "flush_inferred"


@dataclass(frozen=True)
class FrameEvent:
    """一个已发布的数据帧（发布即终态，不会被回退或改写）。"""

    sequence: int
    version: int
    measurements: tuple[tuple[int, int], ...]
    timestamp: int | None
    payload_remainder: int
    #: 该帧是否在版本确认前到达、确认后才释放（推迟确认）。
    deferred: bool


@dataclass(frozen=True)
class VersionEvent:
    """版本（重新）确认事件。"""

    version: int
    previous: int | None
    #: 确认发生时缓冲中是否含有更早到达、被推迟释放的帧。
    deferred: bool


@dataclass(frozen=True)
class GapEvent:
    """断续事件：一段字节未能作为帧发布。"""

    reason: str
    bytes_skipped: int
    detail: str = ""


@dataclass
class FrameMerger:
    """帧归并器。

    Parameters
    ----------
    catalog:
        帧目录，默认 :meth:`FrameCatalog.default`（V1 sum8 / V2 crc8+对齐）。
    max_buffer:
        未发布字节的硬上限（字节）。未确认积压超出时从最早字节丢弃到
        最后一个定界符起点，并发出 ``buffer_overflow`` 断续。
    """

    catalog: FrameCatalog = field(default_factory=FrameCatalog.default)
    max_buffer: int = 4096

    def __post_init__(self) -> None:
        if self.max_buffer < max(len(FRAME_MARKER), len(CONTROL_MARKER)):
            raise ValueError("max_buffer too small to hold any delimiter")
        self._buffer = bytearray()
        self._confirmed: int | None = None
        self._deferred = False
        self._frame_seq = 0
        # offset(当前缓冲) -> {version: end_offset}，仅未确认期累积。
        self._hypotheses: dict[int, dict[int, int]] = {}
        # 标记偏移 -> 上次完整评估时“标记到缓冲末尾”的字节指纹。
        # 未确认期不删除候选字节、每次从缓冲起点重扫；指纹不变说明没有
        # 新字节，跳过重复评估防止死循环；新 push 延长缓冲后指纹改变，
        # 帧更长的版本即可在字节补齐时补登其可证实边界。
        self._marker_fingerprints: dict[int, bytes] = {}

    # ------------------------------------------------------------------ #
    # 可观测状态
    # ------------------------------------------------------------------ #
    @property
    def confirmed_version(self) -> int | None:
        """当前已确认版本；未确认时为 ``None``。"""
        return self._confirmed

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    @property
    def pending_hypotheses(self) -> dict[int, dict[int, int]]:
        """未确认期保留的可证实边界（深拷贝，键为当前缓冲偏移）。"""
        return {
            offset: dict(ends) for offset, ends in self._hypotheses.items()
        }

    def reset(self) -> None:
        """清空全部状态（已发布序号也归零），用于测试或链路复位。"""
        self._buffer.clear()
        self._confirmed = None
        self._deferred = False
        self._frame_seq = 0
        self._hypotheses.clear()
        self._marker_fingerprints.clear()

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #
    def push(
        self, data: bytes | bytearray
    ) -> list[FrameEvent | VersionEvent | GapEvent]:
        """喂入一段可能落在任意帧边界上的字节，返回本次新产生的事件。"""
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("push expects bytes-like data")
        if not data:
            return []
        self._buffer.extend(data)
        return self._drain()

    def flush(
        self,
    ) -> list[FrameEvent | VersionEvent | GapEvent]:
        """声明流结束，释放可推断的尾部并报告残余断续。"""
        events: list[FrameEvent | VersionEvent | GapEvent] = []

        if self._confirmed is None and self._buffer:
            evidenced_versions = {
                version
                for ends in self._hypotheses.values()
                for version in ends
            }
            if len(evidenced_versions) == 1:
                # 唯一可证实版本：推断释放，但显式标注区别于控制包确认。
                inferred = next(iter(evidenced_versions))
                had_backlog = len(self._buffer) > 0
                self._confirmed = inferred
                self._deferred = had_backlog
                self._marker_fingerprints.clear()
                events.append(
                    VersionEvent(
                        inferred, previous=None, deferred=had_backlog
                    )
                )
                events.append(
                    GapEvent(
                        DiscontinuityReason.FLUSH_INFERRED,
                        0,
                        "single_surviving_hypothesis_at_flush",
                    )
                )
                events.extend(self._drain())
            else:
                detail = (
                    "competing_versions="
                    + ",".join(str(v) for v in sorted(evidenced_versions))
                    if evidenced_versions
                    else "no_parseable_frame"
                )
                events.append(
                    GapEvent(
                        DiscontinuityReason.TRUNCATED_AMBIGUOUS,
                        len(self._buffer),
                        detail,
                    )
                )
                self._discard_prefix(len(self._buffer))
                return events
        else:
            events.extend(self._drain())

        if self._buffer:
            events.append(
                GapEvent(
                    DiscontinuityReason.TRUNCATED,
                    len(self._buffer),
                    "trailing_bytes_never_completed_a_frame",
                )
            )
            self._discard_prefix(len(self._buffer))
        return events

    # ------------------------------------------------------------------ #
    # 内部：主驱动
    # ------------------------------------------------------------------ #
    def _drain(self) -> list[FrameEvent | VersionEvent | GapEvent]:
        events: list[FrameEvent | VersionEvent | GapEvent] = []
        while True:
            if self._confirmed is None:
                advanced = self._step_unconfirmed(events)
            else:
                advanced = self._step_confirmed(events)
            if not advanced:
                break
        # 推迟标记只服务于“确认触发的这一轮排空”，下一批数据恢复常态。
        self._deferred = False
        return events

    # ------------------------------------------------------------------ #
    # 内部：已确认模式 —— 唯一版本，逐帧提交，坏帧局部恢复
    # ------------------------------------------------------------------ #
    def _step_confirmed(self, events: list) -> bool:
        spec = self.catalog.spec(self._confirmed)  # type: ignore[arg-type]
        frame_idx = self._buffer.find(FRAME_MARKER)
        control_idx = self._buffer.find(CONTROL_MARKER)
        earliest = _earliest_marker(frame_idx, control_idx)

        if earliest is None:
            # 没有任何完整定界符：尾部可能是某个定界符的真前缀，必须
            # 保留，否则会吃掉下一帧的开头（分块喂入时尤其关键）。
            safe_prefix = _longest_delimiter_suffix(
                self._buffer, (FRAME_MARKER, CONTROL_MARKER)
            )
            skip = len(self._buffer) - safe_prefix
            if skip:
                events.append(
                    GapEvent(
                        DiscontinuityReason.RESYNC_SKIPPED,
                        skip,
                        "no_delimiter_resync",
                    )
                )
                self._discard_prefix(skip)
                return True
            return False

        idx, kind = earliest
        if idx > 0:
            events.append(
                GapEvent(
                    DiscontinuityReason.RESYNC_SKIPPED,
                    idx,
                    "bytes_before_delimiter",
                )
            )
            self._discard_prefix(idx)
            return True

        if kind == "control":
            status, version, end = self.catalog.parse_control_packet(
                self._buffer, 0
            )
            if status == INCOMPLETE:
                return False
            if status == REJECTED:
                events.append(
                    GapEvent(
                        DiscontinuityReason.RESYNC_SKIPPED,
                        4,
                        "rejected_control_packet",
                    )
                )
                self._discard_prefix(4)
                return True
            previous = self._confirmed
            self._discard_prefix(end)  # type: ignore[arg-type]
            if version != previous:
                events.append(
                    VersionEvent(
                        version, previous=previous, deferred=self._deferred
                    )
                )
                self._confirmed = version
            # 控制包已消费：推迟语义只作用于它之前的积压，同批及之后
            # 到达的帧按常规已确认路径发布，立即复位标记。
            self._deferred = False
            return True

        outcome = spec.try_parse(self._buffer, 0, absolute=True)
        if outcome.status == INCOMPLETE:
            return False
        if outcome.status == REJECTED:
            self._reject_frame(events, outcome)
            return True

        self._publish_frame(events, spec.version, outcome)
        self._discard_prefix(outcome.end_offset)  # type: ignore[arg-type]
        return True

    def _publish_frame(
        self, events: list, version: int, outcome: ParseOutcome
    ) -> None:
        self._frame_seq += 1
        events.append(
            FrameEvent(
                sequence=self._frame_seq,
                version=version,
                measurements=tuple(outcome.measurements),
                timestamp=outcome.timestamp,
                payload_remainder=outcome.payload_remainder,
                deferred=self._deferred,
            )
        )
        if outcome.payload_remainder:
            events.append(
                GapEvent(
                    DiscontinuityReason.MALFORMED_PAYLOAD,
                    outcome.payload_remainder,
                    "payload_length_not_a_multiple_of_3",
                )
            )

    def _reject_frame(self, events: list, outcome: ParseOutcome) -> None:
        spec = self.catalog.spec(self._confirmed)  # type: ignore[arg-type]
        declared_end = spec.total_length(self._buffer[2])
        next_frame = self._buffer.find(FRAME_MARKER, len(FRAME_MARKER))
        next_control = self._buffer.find(CONTROL_MARKER, len(CONTROL_MARKER))
        next_marker = _earliest_offset(
            next_frame, next_control, default=len(self._buffer)
        )

        # 声明帧尾仍在缓冲内：按目录长度恢复，保证分块喂入与整段喂入的
        # 恢复点一致（不依赖后续帧标记是否已经到达）。
        if declared_end <= len(self._buffer):
            nxt = declared_end
            detail = (
                f"{outcome.reject_reason or 'rejected'};recover_at_declared_end"
            )
        elif next_marker < len(self._buffer):
            # 声明帧尾超出缓冲（长度字段本身可能损坏），退化为标记扫描。
            nxt = next_marker
            detail = (
                f"{outcome.reject_reason or 'rejected'};recover_at_next_marker"
            )
        else:
            # 既无法证实声明帧尾、也看不到后续定界符：等待更多字节，
            # 不提前丢弃，避免吞掉下一帧的开头。
            return

        reason = (
            DiscontinuityReason.CHECKSUM_MISMATCH
            if outcome.reject_reason
            and outcome.reject_reason.startswith("checksum_mismatch")
            else DiscontinuityReason.RESYNC_SKIPPED
        )
        events.append(GapEvent(reason, nxt, detail))
        self._discard_prefix(nxt)

    # ------------------------------------------------------------------ #
    # 内部：未确认模式 —— 保留全部可证实边界，等待控制包
    # （只记录假设与指纹，不删除候选帧字节）
    # ------------------------------------------------------------------ #
    def _step_unconfirmed(self, events: list) -> bool:
        # 未确认期缓冲整体保留（规模受 max_buffer 约束），每次都从缓冲
        # 起点扫描；已完整评估且视图未变化的标记由指纹守卫跳过，新字节
        # 到达后视图变化、自动放行重新评估。
        frame_idx = self._buffer.find(FRAME_MARKER, 0)
        control_idx = self._buffer.find(CONTROL_MARKER, 0)

        def _fingerprint(offset: int) -> bytes:
            return bytes(self._buffer[offset:])

        # 已完整评估过且“标记到缓冲末尾”字节视图未变化的定界符直接跳过；
        # 新 push 延长缓冲后视图改变，标记自动放行重新评估（长版本晚成帧
        # 场景），同一轮 drain 内视图不变则防止死循环。
        while (
            frame_idx != -1
            and self._marker_fingerprints.get(frame_idx)
            == _fingerprint(frame_idx)
        ):
            frame_idx = self._buffer.find(
                FRAME_MARKER, frame_idx + len(FRAME_MARKER)
            )
        while (
            control_idx != -1
            and self._marker_fingerprints.get(control_idx)
            == _fingerprint(control_idx)
        ):
            control_idx = self._buffer.find(
                CONTROL_MARKER, control_idx + len(CONTROL_MARKER)
            )

        # 控制包优先：只要缓冲中出现完整控制包就先尝试确认，容量兜底不得
        # 阻塞控制包，否则超限积压的链路将永远无法恢复。
        if control_idx != -1 and (
            frame_idx == -1 or control_idx < frame_idx
        ):
            status, version, _end = self.catalog.parse_control_packet(
                self._buffer, control_idx
            )
            if status == "ok":
                # 控制包之前的积压若已超上限，先按容量策略丢弃并记账，
                # 再确认版本；控制包本身及其后字节保留。
                if control_idx > self.max_buffer:
                    self._emit_overflow_before(control_idx, events)
                    control_idx = self._buffer.find(CONTROL_MARKER, 0)
                self._deferred = control_idx > 0
                self._confirmed = version
                self._hypotheses.clear()
                self._marker_fingerprints.clear()
                # 版本在此刻正式确认：立即发布 VersionEvent。下一轮进入
                # 已确认分支时 version == previous，不会重复发事件；缓冲
                # 整体保留，由已确认分支从零重解析，保证推迟帧重新物化。
                events.append(
                    VersionEvent(
                        version, previous=None, deferred=self._deferred
                    )
                )
                return True
            if status == REJECTED:
                skip = control_idx + 4
                events.append(
                    GapEvent(
                        DiscontinuityReason.RESYNC_SKIPPED,
                        skip,
                        "rejected_control_packet",
                    )
                )
                self._discard_prefix(skip)
                return True
            # INCOMPLETE：落到容量兜底后等待更多字节。

        # 没有可立即确认的控制包时，才对数据积压执行容量兜底。
        if self._enforce_capacity(events):
            return True

        if frame_idx == -1:
            # 暂无可评估定界符：字节整体保留等待后续数据，积压风险由
            # _enforce_capacity 兜底。
            return False

        # frame_idx 即当前缓冲中的标记绝对偏移；在该标记处评估全部版本。
        outcomes = self.catalog.evaluate_all(self._buffer, frame_idx)
        if all(o.status == INCOMPLETE for o in outcomes.values()):
            # 所有版本都还缺字节：不登记指纹，后续字节到达后同一标记会被
            # 重新评估。
            return False

        # 完整评估（接受或拒绝）：登记当前“标记到缓冲末尾”视图指纹，由
        # 上面的 skip-while 在本轮跳过它；下一批字节到达后视图变化，同一
        # 标记自动放行重新评估。
        self._marker_fingerprints[frame_idx] = _fingerprint(frame_idx)

        accepted = {
            version: o.end_offset
            for version, o in outcomes.items()
            if o.status == "ok"
        }
        if not accepted:
            # 所有版本都拒绝该标记：字节不丢弃，确认版本后重解析时统一
            # 报告恢复跳过。
            return True

        # 记录可证实边界（按标记绝对偏移累积合并：同一标记在后续字节到达
        # 时可能再次评估，先成立的短版本边界不能被后成立的长版本覆盖）。
        existing = self._hypotheses.setdefault(frame_idx, {})
        existing.update(accepted)
        return True

    # ------------------------------------------------------------------ #
    # 内部：缓冲与假设表维护
    # ------------------------------------------------------------------ #
    def _enforce_capacity(self, events: list) -> bool:
        if len(self._buffer) <= self.max_buffer:
            return False
        last_frame = self._buffer.rfind(FRAME_MARKER)
        last_control = self._buffer.rfind(CONTROL_MARKER)
        last = max(last_frame, last_control)
        if last <= 0:
            keep = _longest_delimiter_suffix(
                self._buffer, (FRAME_MARKER, CONTROL_MARKER)
            )
            skip = len(self._buffer) - keep
        else:
            skip = last
        events.append(
            GapEvent(
                DiscontinuityReason.BUFFER_OVERFLOW,
                skip,
                f"unconfirmed_buffer_exceeded_limit={self.max_buffer}",
            )
        )
        self._discard_prefix(skip)
        return True

    def _emit_overflow_before(
        self, limit_offset: int, events: list
    ) -> None:
        """控制包到达但前缀积压超限时，只丢弃 ``limit_offset`` 之前的字节。

        丢弃点取前缀范围内最后一个定界符起点（没有则保留定界符真前缀），
        控制包及其后的完整字节不受影响，链路可立即恢复。
        """
        last_frame = self._buffer.rfind(FRAME_MARKER, 0, limit_offset)
        last_control = self._buffer.rfind(CONTROL_MARKER, 0, limit_offset)
        last = max(last_frame, last_control)
        if last <= 0:
            prefix = bytes(self._buffer[:limit_offset])
            keep = _longest_delimiter_suffix(
                prefix, (FRAME_MARKER, CONTROL_MARKER)
            )
            skip = limit_offset - keep
        else:
            skip = last
        events.append(
            GapEvent(
                DiscontinuityReason.BUFFER_OVERFLOW,
                skip,
                f"backlog_before_control_exceeded_limit={self.max_buffer}",
            )
        )
        self._discard_prefix(skip)

    def _discard_prefix(self, n: int) -> None:
        if n <= 0:
            return
        del self._buffer[:n]
        shifted: dict[int, dict[int, int]] = {}
        for offset, ends in self._hypotheses.items():
            if offset >= n:
                shifted[offset - n] = {
                    version: end - n for version, end in ends.items()
                }
        self._hypotheses = shifted
        # 前缀丢弃后“标记到缓冲末尾”的视图整体改变，旧指纹必然失效，
        # 直接清空（未确认期的前缀丢弃只发生在坏控制包/溢出恢复路径）。
        self._marker_fingerprints = {}


def _earliest_marker(
    frame_idx: int, control_idx: int
) -> tuple[int, str] | None:
    candidates: list[tuple[int, str]] = []
    if frame_idx != -1:
        candidates.append((frame_idx, "frame"))
    if control_idx != -1:
        candidates.append((control_idx, "control"))
    if not candidates:
        return None
    return min(candidates)


def _earliest_offset(a: int, b: int, *, default: int) -> int:
    candidates = [x for x in (a, b) if x != -1]
    return min(candidates) if candidates else default


def _longest_delimiter_suffix(
    buf: bytes, delimiters: tuple[bytes, ...]
) -> int:
    """返回缓冲尾部中“恰好是某个定界符真前缀”的最长长度。

    例如定界符 ``AA 55``，缓冲以 ``AA`` 结尾时返回 1；没有任何定界符的
    前缀匹配时返回 0。该长度即重同步时必须保留的尾部字节数。
    """
    longest = 0
    for marker in delimiters:
        for length in range(1, len(marker)):
            if len(buf) >= length and buf[-length:] == marker[:length]:
                longest = max(longest, length)
    return longest
