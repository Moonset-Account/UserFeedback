"""版本化观测存储：修订替换、快照查询与迟到更正支持。

存储语义
========
* 每条观测属于一个观测系列，键为 ``(source, segment_start, series_id)``；
* 同一系列只保留最大 ``revision`` 的观测，较新修订替换旧修订；
  旧修订不会被物理丢弃，可通过历史快照版本重查；
* 每次摄入产生一个单调递增的全局版本号，任何见证都记录其生成版本，
  之后可在该版本上独立复算，不受后续迟到更正影响；
* 线程安全：内部用锁保护，支持并发摄入与查询交错。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .model import (
    MusReport,
    SegmentModel,
    WindowResult,
)
from .mus import mus_report
from .records import Observation, ValidationError
from .window import build_window_witness, compute_window_bounds


class HistoryMissingError(Exception):
    """请求的历史版本早于仍保留的最早快照。"""


@dataclass(frozen=True)
class Change:
    """一次摄入造成的变更，便于调用方定位受影响时间窗并触发重查。"""

    store_version: int
    source: str
    segment_start: int
    segment_length: int
    series_id: str
    revision: int
    replaced_revision: Optional[int]
    kind: str  # "insert" | "replace"

    @property
    def segment_end(self) -> int:
        return self.segment_start + self.segment_length


@dataclass
class _Snapshot:
    version: int
    # key -> 当前有效观测
    effective: Dict[tuple, Observation]
    # 按 (source, segment_start) 分组的有效观测列表
    segments: Dict[tuple, List[Observation]]


class VersionedStore:
    """线程安全的修订感知观测存储与窗口查询入口。"""

    def __init__(self, retain_history: bool = True, max_cached_models: int = 256) -> None:
        self._lock = threading.RLock()
        self._retain_history = retain_history
        self._current: Dict[tuple, Observation] = {}
        self._segments: Dict[tuple, List[Observation]] = {}
        self._snapshots: List[_Snapshot] = [_Snapshot(0, {}, {})]
        self._model_cache: Dict[Tuple[int, tuple], SegmentModel] = {}
        self._max_cached_models = max_cached_models

    # ------------------------------------------------------------------
    # 摄入
    # ------------------------------------------------------------------
    def ingest(self, observation: Observation) -> Change:
        """摄入一条观测；同系列高修订替换低修订，返回变更描述。"""
        with self._lock:
            key = observation.key()
            old = self._current.get(key)
            if old is not None and old.revision > observation.revision:
                raise ValidationError(
                    f"系列 {key} 已有修订 {old.revision}，"
                    f"拒绝迟到的旧修订 {observation.revision}"
                )
            kind = "replace" if old is not None else "insert"
            replaced = old.revision if old is not None else None
            if old is not None and old.revision == observation.revision:
                if old == observation:
                    # 幂等重放：不推进版本
                    return Change(
                        store_version=self._snapshots[-1].version,
                        source=observation.source,
                        segment_start=observation.segment_start,
                        segment_length=observation.segment_length,
                        series_id=observation.series_id,
                        revision=observation.revision,
                        replaced_revision=None,
                        kind="insert",
                    )
                raise ValidationError(
                    f"系列 {key} 修订号 {observation.revision} 已存在且内容不同"
                )

            new_effective = dict(self._current)
            new_effective[key] = observation
            new_segments: Dict[tuple, List[Observation]] = {
                seg_key: list(obs) for seg_key, obs in self._segments.items()
            }
            seg_key = (observation.source, observation.segment_start)
            bucket = new_segments.setdefault(seg_key, [])
            if old is not None:
                bucket[:] = [o for o in bucket if o.key() != key]
            bucket.append(observation)

            self._current = new_effective
            self._segments = new_segments
            version = self._snapshots[-1].version + 1
            snapshot = _Snapshot(version, new_effective, new_segments)
            if self._retain_history:
                self._snapshots.append(snapshot)
            else:
                self._snapshots = [snapshot]
            return Change(
                store_version=version,
                source=observation.source,
                segment_start=observation.segment_start,
                segment_length=observation.segment_length,
                series_id=observation.series_id,
                revision=observation.revision,
                replaced_revision=replaced,
                kind=kind,
            )

    def ingest_many(self, observations: Sequence[Observation]) -> List[Change]:
        return [self.ingest(o) for o in observations]

    @property
    def version(self) -> int:
        with self._lock:
            return self._snapshots[-1].version

    # ------------------------------------------------------------------
    # 快照解析
    # ------------------------------------------------------------------
    def _resolve_snapshot(self, at_version: Optional[int]) -> _Snapshot:
        with self._lock:
            latest = self._snapshots[-1].version
            if at_version is None:
                at_version = latest
            if at_version < 0 or at_version > latest:
                raise HistoryMissingError(
                    f"版本 {at_version} 不存在，当前最新版本为 {latest}"
                )
            if self._snapshots[0].version > at_version:
                raise HistoryMissingError(
                    f"版本 {at_version} 的快照已被裁剪，"
                    f"最早可用版本为 {self._snapshots[0].version}"
                )
            for snap in reversed(self._snapshots):
                if snap.version <= at_version:
                    return snap
            raise HistoryMissingError(f"版本 {at_version} 快照不可用")

    def effective_observations(
        self,
        source: str,
        segment_start: int,
        at_version: Optional[int] = None,
    ) -> List[Observation]:
        """某段在指定版本（默认最新）上的全部有效观测。"""
        snap = self._resolve_snapshot(at_version)
        return list(snap.segments.get((source, segment_start), []))

    def _get_model(
        self, snap: _Snapshot, source: str, segment_start: int
    ) -> Optional[SegmentModel]:
        cache_key = (snap.version, (source, segment_start))
        cached = self._model_cache.get(cache_key)
        if cached is not None:
            return cached
        obs = snap.segments.get((source, segment_start))
        if not obs:
            return None
        model = SegmentModel(obs)
        # 简单的有界缓存：超限时丢弃最旧条目（键按插入序保留）
        if len(self._model_cache) >= self._max_cached_models:
            oldest = next(iter(self._model_cache))
            del self._model_cache[oldest]
        self._model_cache[cache_key] = model
        return model

    # ------------------------------------------------------------------
    # 窗口查询
    # ------------------------------------------------------------------
    def query_window(
        self,
        source: str,
        window_start: int,
        window_end: int,
        k: int,
        segment_starts: Optional[Sequence[int]] = None,
        at_version: Optional[int] = None,
    ) -> WindowResult:
        """查询连续时间窗 [window_start, window_end) 内第 k 小值的极值。

        窗边界只落在段界：``segment_starts`` 给出窗覆盖的各段起点，
        其长度与每个段的 segment_length 由观测记录携带。
        """
        snap = self._resolve_snapshot(at_version)
        if segment_starts is None:
            segment_starts = sorted(
                start
                for (src, start) in snap.segments
                if src == source
                and start >= window_start
                and start < window_end
            )
        segment_starts = list(segment_starts)
        if not segment_starts:
            raise ValidationError("窗口内没有任何时间段观测")

        models: List[SegmentModel] = []
        mus_reports: List[MusReport] = []
        covered: List[Observation] = []
        for start in segment_starts:
            model = self._get_model(snap, source, start)
            if model is None:
                raise ValidationError(
                    f"段 ({source}, {start}) 在版本 {snap.version} 上无观测"
                )
            if model.segment_start < window_start or model.segment_end > window_end:
                raise ValidationError(
                    f"段 [{model.segment_start},{model.segment_end}) "
                    f"超出窗口 [{window_start},{window_end})"
                )
            covered.extend(model.observations)
            if not model.feasible:
                mus_reports.append(mus_report(model.observations))
            else:
                models.append(model)

        if mus_reports:
            return WindowResult(
                status="infeasible",
                source=source,
                window_start=window_start,
                window_end=window_end,
                k=k,
                mus_reports=mus_reports,
                store_version=snap.version,
            )

        # 段必须无缝铺满窗口
        ordered = sorted(segment_starts)
        cursor = window_start
        for start in ordered:
            if start != cursor:
                raise ValidationError(
                    f"窗口 [{window_start},{window_end}) 在 {cursor} 处存在段缺口"
                )
            model = self._get_model(snap, source, start)
            cursor = model.segment_end
        if cursor != window_end:
            raise ValidationError(
                f"段未铺满窗口：结束于 {cursor}，窗口结束于 {window_end}"
            )

        min_kth, max_kth = compute_window_bounds(models, k)
        total = sum(m.total for m in models)
        min_witness = build_window_witness(
            models,
            k,
            "min",
            covered,
            snap.version,
            window_start,
            window_end,
            source,
            min_kth,
        )
        max_witness = build_window_witness(
            models,
            k,
            "max",
            covered,
            snap.version,
            window_start,
            window_end,
            source,
            max_kth,
        )
        return WindowResult(
            status="ok",
            source=source,
            window_start=window_start,
            window_end=window_end,
            k=k,
            total_samples=total,
            min_kth=min_kth,
            max_kth=max_kth,
            min_witness=min_witness,
            max_witness=max_witness,
            store_version=snap.version,
        )

    # ------------------------------------------------------------------
    # 迟到更正辅助
    # ------------------------------------------------------------------
    def affected_segments(self, changes: Sequence[Change]) -> List[tuple]:
        """根据摄入变更返回受影响的 (source, segment_start, segment_end) 列表。"""
        seen = set()
        out = []
        for ch in changes:
            item = (ch.source, ch.segment_start, ch.segment_end)
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out
