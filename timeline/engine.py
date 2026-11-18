"""时间线引擎：把设备/记录/重启/锚点/先后关系编译为差分约束系统并求解。

核心变量
--------
REF               固定为 0 的参考变量（真实时间轴原点）。
off:<dev>#<seg>   设备每个重启段的时钟偏移（真实时间 = 本地读数 + 偏移）。
t:<record>        每条记录的真实时间点。

约束
----
记录观测误差： |t_r - (off_seg + local_r)| <= obs_error_r
同段单调性：   同段内本地时钟递增 => t_i <= t_j（重启后新段不继承）
共同观测锚点： |t_a - t_b| <= tolerance
绝对时间锚点： |t_r - abs_ts| <= tolerance
明确先后：     t_after - t_before >= min_gap

可行性由 Bellman-Ford 负环检测判定；负环即锚点/先后关系互相矛盾。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from .model import (
    AbsoluteAnchor,
    ConflictError,
    ConflictReport,
    Device,
    IntervalClash,
    Ordering,
    Record,
    SegmentKey,
    SharedEventAnchor,
    TimeRange,
)
from .solver import ConstraintGraph, NegativeCycle, Tag

EPS = 1e-12


@dataclass(frozen=True)
class OrderingResult:
    relation: str  # "before" | "after" | "undetermined"
    reasons: tuple[str, ...]  # 因果链上的来源标签；undetermined 时为空


class TimelineEngine:
    def __init__(self) -> None:
        self._devices: dict[str, Device] = {}
        self._records: dict[str, Record] = {}
        self._shared_anchors: list[SharedEventAnchor] = []
        self._absolute_anchors: list[AbsoluteAnchor] = []
        self._orderings: list[Ordering] = []
        # 最近一次求解的缓存
        self._ranges: dict[str, TimeRange] = {}
        self._offset_bounds: dict[SegmentKey, tuple[float, float]] = {}
        self._last_affected: set[SegmentKey] = set()
        self._solved = False
        # 段间无向连接（共同观测/跨段先后产生），用于增量影响面分析
        self._seg_adj: dict[SegmentKey, set[SegmentKey]] = {}

    # ------------------------------------------------------------------ #
    # 数据录入
    # ------------------------------------------------------------------ #
    def add_device(self, device: Device) -> None:
        if device.device_id in self._devices:
            raise ValueError(f"设备已存在: {device.device_id}")
        self._devices[device.device_id] = device
        self._solved = False

    def add_record(self, record: Record) -> None:
        if record.record_id in self._records:
            raise ValueError(f"记录已存在: {record.record_id}")
        if record.device_id not in self._devices:
            raise ValueError(f"未知设备: {record.device_id}")
        self._records[record.record_id] = record
        self._solved = False

    def add_ordering(self, ordering: Ordering) -> Ordering:
        """登记明确先后关系并立即试解；矛盾时抛 ConflictError 且不改变已解状态。"""
        for rid in (ordering.before, ordering.after):
            if rid not in self._records:
                raise ValueError(f"未知记录: {rid}")
        self._orderings.append(ordering)
        affected = self._affected_for_ordering(ordering)
        try:
            self.solve(affected_override=affected)
        except ConflictError:
            self._orderings.pop()
            raise
        return ordering

    def add_shared_anchor(self, anchor: SharedEventAnchor) -> None:
        for rid in (anchor.record_a, anchor.record_b):
            if rid not in self._records:
                raise ValueError(f"未知记录: {rid}")
        self._shared_anchors.append(anchor)
        affected = self._affected_for_shared(anchor)
        try:
            self.solve(affected_override=affected)
        except ConflictError:
            self._shared_anchors.pop()
            self._rollback_seg_edge(anchor)
            raise

    def add_absolute_anchor(self, anchor: AbsoluteAnchor) -> None:
        if anchor.record_id not in self._records:
            raise ValueError(f"未知记录: {anchor.record_id}")
        self._absolute_anchors.append(anchor)
        seg = self.segment_of(anchor.record_id)
        affected = self._component(seg)
        try:
            self.solve(affected_override=affected)
        except ConflictError:
            self._absolute_anchors.pop()
            raise

    # 便捷别名：题面所称“新增锚点”
    add_anchor = add_shared_anchor

    # ------------------------------------------------------------------ #
    # 段归属
    # ------------------------------------------------------------------ #
    def segment_of(self, record_id: str) -> SegmentKey:
        rec = self._records[record_id]
        dev = self._devices[rec.device_id]
        return SegmentKey(rec.device_id, dev.segment_index_for(rec.local_ts))

    def all_segments(self) -> set[SegmentKey]:
        segs: set[SegmentKey] = set()
        for rec in self._records.values():
            segs.add(self.segment_of(rec.record_id))
        return segs

    # ------------------------------------------------------------------ #
    # 约束图构建
    # ------------------------------------------------------------------ #
    def _off_name(self, seg: SegmentKey) -> str:
        return f"off:{seg.device_id}#{seg.seg_index}"

    def _t_name(self, record_id: str) -> str:
        return f"t:{record_id}"

    def _build_graph(self) -> tuple[ConstraintGraph, int, dict[str, SegmentKey]]:
        g = ConstraintGraph()
        ref = g.add_variable("REF")
        seg_by_var: dict[str, SegmentKey] = {}

        seg_vars: dict[SegmentKey, int] = {}
        for seg in sorted(self.all_segments(), key=lambda s: (s.device_id, s.seg_index)):
            v = g.add_variable(self._off_name(seg))
            seg_vars[seg] = v
            seg_by_var[g.name_of(v)] = seg

        t_vars: dict[str, int] = {}
        for rid, rec in self._records.items():
            t_vars[rid] = g.add_variable(self._t_name(rid))

        # 1) 记录观测误差：|t_r - off_seg - local| <= obs_error
        for rid, rec in self._records.items():
            seg = self.segment_of(rid)
            off = seg_vars[seg]
            t = t_vars[rid]
            tag = Tag("record", rec.source)
            # t - off <= local + err ; t - off >= local - err
            g.add_le(t, off, rec.local_ts + rec.obs_error, tag)
            g.add_ge(t, off, rec.local_ts - rec.obs_error, tag)

        # 2) 同段本地时钟单调：同段内按本地读数升序，相邻记录 t_i <= t_j
        by_seg: dict[SegmentKey, list[Record]] = {}
        for rec in self._records.values():
            by_seg.setdefault(self.segment_of(rec.record_id), []).append(rec)
        for seg, recs in by_seg.items():
            recs.sort(key=lambda r: r.local_ts)
            for prev, nxt in zip(recs, recs[1:]):
                tag = Tag("monotone", f"monotone:{seg.device_id}#seg{seg.seg_index}")
                # t_prev - t_next <= 0
                g.add_le(t_vars[prev.record_id], t_vars[nxt.record_id], 0.0, tag)

        # 3) 共同观测锚点：|t_a - t_b| <= tolerance
        for anc in self._shared_anchors:
            tag = Tag("anchor", anc.source)
            g.add_eq_diff(
                t_vars[anc.record_a], t_vars[anc.record_b], 0.0, anc.tolerance, tag
            )

        # 4) 绝对时间锚点：|t_r - abs_ts| <= tolerance
        for anc in self._absolute_anchors:
            tag = Tag("anchor", anc.source)
            g.add_eq_diff(t_vars[anc.record_id], ref, anc.abs_ts, anc.tolerance, tag)

        # 5) 明确先后：t_after - t_before >= min_gap
        for od in self._orderings:
            tag = Tag("ordering", od.source)
            g.add_ge(t_vars[od.after], t_vars[od.before], od.min_gap, tag)

        return g, ref, seg_by_var

    # ------------------------------------------------------------------ #
    # 求解
    # ------------------------------------------------------------------ #
    def solve(
        self, affected_override: set[SegmentKey] | None = None
    ) -> dict[str, TimeRange]:
        g, ref, seg_by_var = self._build_graph()
        cycle = g.find_negative_cycle(ref)
        if cycle is not None:
            report = self._build_conflict_report(cycle, g, seg_by_var)
            raise ConflictError(report)

        bounds = g.solve_bounds(ref)

        new_ranges: dict[str, TimeRange] = {}
        new_offsets: dict[SegmentKey, tuple[float, float]] = {}
        for seg in self.all_segments():
            v = g.index_of(self._off_name(seg))
            new_offsets[seg] = bounds[v]
        for rid, rec in self._records.items():
            seg = self.segment_of(rid)
            lo, hi = bounds[g.index_of(self._t_name(rid))]
            new_ranges[rid] = TimeRange(rid, lo, hi, seg)

        if affected_override is None:
            affected = set(self.all_segments())
        else:
            affected = {s for s in affected_override if s in new_offsets}
        self._last_affected = affected
        self._ranges = new_ranges
        self._offset_bounds = new_offsets
        self._solved = True
        return new_ranges

    def _require_solved(self) -> None:
        if not self._solved:
            self.solve()

    def time_ranges(self) -> dict[str, TimeRange]:
        self._require_solved()
        return dict(self._ranges)

    def time_range_of(self, record_id: str) -> TimeRange:
        self._require_solved()
        return self._ranges[record_id]

    def offset_bounds(self) -> dict[SegmentKey, tuple[float, float]]:
        """每个重启段的偏移可行区间（无锚点段为 (-inf, inf)）。"""
        self._require_solved()
        return dict(self._offset_bounds)

    def last_affected_segments(self) -> set[SegmentKey]:
        """最近一次增量求解实际重算的设备段。"""
        return set(self._last_affected)

    def last_recomputed_records(self) -> set[str]:
        return {
            rid
            for rid, rng in self._ranges.items()
            if rng.segment in self._last_affected
        }

    # ------------------------------------------------------------------ #
    # 增量影响面：段连接图上的连通分量
    # ------------------------------------------------------------------ #
    def _ensure_seg_node(self, seg: SegmentKey) -> None:
        self._seg_adj.setdefault(seg, set())

    def _affected_for_shared(self, anchor: SharedEventAnchor) -> set[SegmentKey]:
        sa = self.segment_of(anchor.record_a)
        sb = self.segment_of(anchor.record_b)
        self._ensure_seg_node(sa)
        self._ensure_seg_node(sb)
        self._seg_adj[sa].add(sb)
        self._seg_adj[sb].add(sa)
        return self._component(sa)

    def _rollback_seg_edge(self, anchor: SharedEventAnchor) -> None:
        sa = self.segment_of(anchor.record_a)
        sb = self.segment_of(anchor.record_b)
        self._seg_adj.get(sa, set()).discard(sb)
        self._seg_adj.get(sb, set()).discard(sa)

    def _affected_for_ordering(self, ordering: Ordering) -> set[SegmentKey]:
        sa = self.segment_of(ordering.before)
        sb = self.segment_of(ordering.after)
        if sa == sb:
            self._ensure_seg_node(sa)
            return self._component(sa)
        self._ensure_seg_node(sa)
        self._ensure_seg_node(sb)
        self._seg_adj[sa].add(sb)
        self._seg_adj[sb].add(sa)
        return self._component(sa)

    def _component(self, start: SegmentKey) -> set[SegmentKey]:
        self._ensure_seg_node(start)
        seen = {start}
        q = deque([start])
        while q:
            cur = q.popleft()
            for nxt in self._seg_adj.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
        return seen

    # ------------------------------------------------------------------ #
    # 因果关系与顺序判定
    # ------------------------------------------------------------------ #
    def _build_causal_graph(
        self,
    ) -> tuple[dict[str, list[tuple[str, str]]], dict[str, str]]:
        """返回 (邻接表 边带来源标签, 边标签集合)。"""
        adj: dict[str, list[tuple[str, str]]] = {rid: [] for rid in self._records}
        edge_sources: dict[str, str] = {}

        # 同段单调性边（相邻本地读数）
        by_seg: dict[SegmentKey, list[Record]] = {}
        for rec in self._records.values():
            by_seg.setdefault(self.segment_of(rec.record_id), []).append(rec)
        for seg, recs in by_seg.items():
            recs.sort(key=lambda r: r.local_ts)
            label = f"monotone:{seg.device_id}#seg{seg.seg_index}"
            for prev, nxt in zip(recs, recs[1:]):
                adj[prev.record_id].append((nxt.record_id, label))
                edge_sources[f"{prev.record_id}->{nxt.record_id}"] = label

        # 明确先后边
        for od in self._orderings:
            adj[od.before].append((od.after, od.source))
            edge_sources[f"{od.before}->{od.after}"] = od.source
        return adj, edge_sources

    def _reachability(self, start: str, adj: dict[str, list[tuple[str, str]]]):
        parent: dict[str, tuple[str, str]] = {}
        seen = {start}
        q = deque([start])
        while q:
            cur = q.popleft()
            for nxt, label in adj[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    parent[nxt] = (cur, label)
                    q.append(nxt)
        return seen, parent

    @staticmethod
    def _reasons_to(target: str, parent: dict[str, tuple[str, str]]) -> tuple[str, ...]:
        chain: list[str] = []
        cur = target
        while cur in parent:
            prev, label = parent[cur]
            chain.append(label)
            cur = prev
        chain.reverse()
        return tuple(chain)

    def compare(self, record_a: str, record_b: str) -> OrderingResult:
        """判定两条记录的先后。

        - 因果依据（明确先后 / 同段单调链）可达 => before/after，给出依据来源；
        - 无因果依据但区间严格分离 => 按区间证据判定；
        - 区间相交（含端点相触）且无因果依据 => undetermined（顺序未定）。
        """
        self._require_solved()
        adj, _ = self._build_causal_graph()
        reach_a, parent_a = self._reachability(record_a, adj)
        reach_b, parent_b = self._reachability(record_b, adj)
        if record_b in reach_a:
            return OrderingResult("before", self._reasons_to(record_b, parent_a))
        if record_a in reach_b:
            return OrderingResult("after", self._reasons_to(record_a, parent_b))

        ra = self._ranges[record_a]
        rb = self._ranges[record_b]
        if ra.upper < rb.lower - EPS:
            return OrderingResult("before", (f"range:{record_a}.upper<{record_b}.lower",))
        if rb.upper < ra.lower - EPS:
            return OrderingResult("after", (f"range:{record_b}.upper<{record_a}.lower",))
        return OrderingResult("undetermined", ())

    def undetermined_pairs(self) -> list[tuple[str, str]]:
        """所有“区间相交且缺少因果依据”的记录对（按 record_id 排序输出）。"""
        self._require_solved()
        adj, _ = self._build_causal_graph()
        ids = sorted(self._records)
        out: list[tuple[str, str]] = []
        for i, a in enumerate(ids):
            reach, _ = self._reachability(a, adj)
            for b in ids[i + 1:]:
                if b in reach:
                    continue
                reach_b, _ = self._reachability(b, adj)
                if a in reach_b:
                    continue
                if self._ranges[a].overlaps(self._ranges[b], EPS):
                    out.append((a, b))
        return out

    # ------------------------------------------------------------------ #
    # 冲突报告
    # ------------------------------------------------------------------ #
    def _build_conflict_report(
        self,
        cycle: NegativeCycle,
        g: ConstraintGraph,
        seg_by_var: dict[str, SegmentKey],
    ) -> ConflictReport:
        sources: list[str] = []
        seg_set: set[SegmentKey] = set()
        anchor_ids_on_cycle: set[str] = set()
        # 环上每个节点都会作为某条边的前驱出现，遍历 _u 即可覆盖全部涉事变量。
        # 负环可能经过偏移变量(off:)或记录变量(t:)，两者都要映射回设备段。
        for _u, _w, tag in cycle.edges:
            if tag.source not in sources:
                sources.append(tag.source)
            name = g.name_of(_u)
            if name in seg_by_var:
                seg_set.add(seg_by_var[name])
            elif name.startswith("t:"):
                rid = name[2:]
                if rid in self._records:
                    seg_set.add(self.segment_of(rid))
            if tag.kind == "anchor":
                anchor_ids_on_cycle.add(self._anchor_id_of_source(tag.source))

        # 隔离每个涉事锚点，求它在选定钉住段上的偏移可行区间
        anchor_intervals: dict[str, tuple[SegmentKey, tuple[float, float]]] = {}
        involved_anchors = self._involved_anchors(anchor_ids_on_cycle)
        ordered_segs = sorted(seg_set, key=lambda s: (s.device_id, s.seg_index))
        pin_seed = ordered_segs[0] if ordered_segs else None
        for anc in involved_anchors:
            pin, interval = self._isolated_anchor_interval(anc, pin_seed)
            anchor_intervals[anc[0]] = (pin, interval)
        # 共同观测锚点若报告段落在其它涉事段上，也尝试按涉事段补算，
        # 便于同段多锚点的区间冲突被识别。
        for anc in involved_anchors:
            if anc[2] != "shared":
                continue
            for seg in (self.segment_of(anc[3][0]), self.segment_of(anc[3][1])):
                if seg in seg_set and seg not in [
                    anchor_intervals[anc[0]][0]
                ]:
                    _pin, interval = self._isolated_anchor_interval(anc, seg)
                    anchor_intervals[f"{anc[0]}@{seg.device_id}#seg{seg.seg_index}"] = (
                        seg,
                        interval,
                    )

        clashes = self._interval_clashes(anchor_intervals)
        clash_sources = {s for c in clashes for s in c.sources}
        for s in clash_sources:
            if s not in sources:
                sources.append(s)

        message = (
            "锚点/先后关系互相矛盾，约束系统出现负环；"
            f"涉事来源={tuple(sources)}，涉事段="
            f"{tuple(str(s) for s in ordered_segs)}"
        )
        if clashes:
            message += "；偏移区间冲突=" + "; ".join(
                f"{c.segment}: {c.anchor_a}{c.interval_a} vs {c.anchor_b}{c.interval_b}"
                for c in clashes
            )
        return ConflictReport(
            message=message,
            conflicting_sources=tuple(sources),
            conflicting_segments=tuple(ordered_segs),
            anchor_intervals=anchor_intervals,
            interval_clashes=tuple(clashes),
        )

    def _anchor_id_of_source(self, source: str) -> str:
        for anc in (*self._shared_anchors, *self._absolute_anchors):
            if anc.source == source or anc.anchor_id == source:
                return anc.anchor_id
        return source

    def _involved_anchors(self, cycle_anchor_ids: set[str]):
        out = []
        for anc in self._shared_anchors:
            if anc.anchor_id in cycle_anchor_ids or not cycle_anchor_ids:
                out.append(
                    (
                        anc.anchor_id,
                        anc.source,
                        "shared",
                        (anc.record_a, anc.record_b),
                        anc.tolerance,
                    )
                )
        for anc in self._absolute_anchors:
            if anc.anchor_id in cycle_anchor_ids or not cycle_anchor_ids:
                out.append(
                    (
                        anc.anchor_id,
                        anc.source,
                        "absolute",
                        (anc.record_id,),
                        anc.tolerance,
                    )
                )
        return out

    def _isolated_anchor_interval(
        self, anchor_tuple, pin_seed: SegmentKey | None
    ) -> tuple[SegmentKey, tuple[float, float]]:
        """仅保留该锚点自身的差分约束，求它在目标设备段上隐含的偏移可行区间。

        最小图不预先钉住任何偏移（钉住会与锚点约束叠加而失真）；
        绝对锚点经 REF 给出有界区间，共同观测锚点给出两段偏移之差的有界区间。
        pin_seed 仅用于在多段时优先选择与冲突段一致的报告段。
        """
        del pin_seed  # 不再需要外部钉住段
        anchor_id, _src, kind, rec_ids, tol = anchor_tuple
        segs = [self.segment_of(rid) for rid in rec_ids]
        target = segs[0]

        g = ConstraintGraph()
        ref = g.add_variable("REF")
        off_vars: dict[SegmentKey, int] = {}
        for seg in segs:
            off_vars[seg] = g.add_variable(self._off_name(seg))

        tag = Tag("anchor", anchor_id)
        if kind == "absolute":
            rec = self._records[rec_ids[0]]
            seg = self.segment_of(rec.record_id)
            # |off - (abs - local)| <= tol + obs_error
            g.add_eq_diff(
                off_vars[seg],
                ref,
                _anchor_abs_ts(anchor_id, self._absolute_anchors) - rec.local_ts,
                tol + rec.obs_error,
                tag,
            )
        else:
            ra = self._records[rec_ids[0]]
            rb = self._records[rec_ids[1]]
            sa = self.segment_of(ra.record_id)
            sb = self.segment_of(rb.record_id)
            # |(off_a + la) - (off_b + lb)| <= tol + ea + eb
            # => |(off_a - off_b) - (lb - la)| <= tol+ea+eb
            g.add_eq_diff(
                off_vars[sa],
                off_vars[sb],
                rb.local_ts - ra.local_ts,
                tol + ra.obs_error + rb.obs_error,
                tag,
            )
        if g.find_negative_cycle(ref) is not None:
            # 单锚点自身就矛盾（理论上 tol>=0 不会发生）
            return target, (math.nan, math.nan)
        bounds = g.solve_bounds(ref)
        return target, bounds[off_vars[target]]

    def _interval_clashes(
        self,
        anchor_intervals: dict[str, tuple[SegmentKey, tuple[float, float]]],
    ) -> list[IntervalClash]:
        by_pin: dict[SegmentKey, list[tuple[str, tuple[float, float]]]] = {}
        for anchor_id, (pin, interval) in anchor_intervals.items():
            if any(math.isnan(x) for x in interval):
                continue
            by_pin.setdefault(pin, []).append((anchor_id, interval))
        clashes: list[IntervalClash] = []
        for pin, items in by_pin.items():
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    id_a, (la, ha) = items[i]
                    id_b, (lb, hb) = items[j]
                    if min(ha, hb) < max(la, lb) - EPS:
                        clashes.append(
                            IntervalClash(
                                segment=pin,
                                anchor_a=id_a,
                                interval_a=(la, ha),
                                anchor_b=id_b,
                                interval_b=(lb, hb),
                                sources=(
                                    self._anchor_source(id_a),
                                    self._anchor_source(id_b),
                                ),
                            )
                        )
        return clashes

    def _anchor_source(self, anchor_id: str) -> str:
        for anc in (*self._shared_anchors, *self._absolute_anchors):
            if anc.anchor_id == anchor_id:
                return anc.source
        return anchor_id


def _anchor_abs_ts(anchor_id: str, anchors: list[AbsoluteAnchor]) -> float:
    for anc in anchors:
        if anc.anchor_id == anchor_id:
            return anc.abs_ts
    raise KeyError(anchor_id)  # pragma: no cover - 内部调用保证存在
