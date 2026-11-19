"""只读恢复判定引擎。

判定原则（对应题面交叉约束）：

1. 只考虑删除时刻（含）到采集时刻（不含）窗口内的事件；删除前的历史
   改写与分配不影响“删除时原文”的归属判断。
2. 窗口内物理簇区间只要被分配给过其他对象——无论之后是否释放、也无论
   现存字节是否与原文相同——一律标记 ``reallocated``，绝不输出字节。
3. 窗口内的记录缺口使连续归属无法证明，标记 ``record_gap``。
4. 窗口内的改写使现存字节不再可信，标记 ``overwritten``。
5. 归属连续的区间还要通过采集校验（缺失/错误/短读分别标记），
   两者都成立才输出原字节。
6. 末簇按原文件大小裁切；每个区段都带物理簇与簇内字节范围，可回查。
"""

from __future__ import annotations

from collections import defaultdict

from .model import (
    ACQUISITION_ERROR,
    ACQUISITION_MISSING,
    ACQUISITION_SHORT,
    MAPPING_MISSING,
    OVERWRITTEN,
    REALLOCATED,
    RECOVERED,
    RECORD_GAP,
    Allocation,
    Chunk,
    Overwrite,
    RecordGapEvent,
    RecoveryCase,
)
from .report import Segment, build_report, reason_text

# 同一字节范围命中多类问题时的报告优先级：
# 归属断裂（再分配）最严重，其次是记录不可追溯，最后才是内容层面的改写。
_TAINT_PRIORITY = (REALLOCATED, RECORD_GAP, OVERWRITTEN)


def _in_window(event_time: int | float, case: RecoveryCase) -> bool:
    return case.deletion_time <= event_time < case.acquisition_time


def _overlaps_window(start: int | float, end: int | float | None,
                     case: RecoveryCase) -> bool:
    """半开时间区间 [start, end) 与判定窗口 [删除, 采集) 是否相交。"""
    if start >= case.acquisition_time:
        return False
    if end is not None and end <= case.deletion_time:
        return False
    return True


def _intersect(start_a: int, end_a: int, start_b: int, end_b: int):
    start = max(start_a, start_b)
    end = min(end_a, end_b)
    return (start, end) if start < end else None


def _group_events(case: RecoveryCase):
    allocations: dict[int, list[Allocation]] = defaultdict(list)
    overwrites: dict[int, list[Overwrite]] = defaultdict(list)
    gaps: dict[int, list[RecordGapEvent]] = defaultdict(list)
    for event in case.allocations:
        if _overlaps_window(event.start_time, event.end_time, case):
            allocations[event.physical_cluster].append(event)
    for event in case.overwrites:
        if _in_window(event.time, case):
            overwrites[event.physical_cluster].append(event)
    for event in case.record_gaps:
        if _overlaps_window(event.start_time, event.end_time, case):
            gaps[event.physical_cluster].append(event)
    return allocations, overwrites, gaps


def _collect_taint(case: RecoveryCase, physical_cluster: int,
                   allocations, gaps, overwrites):
    """返回簇内切分点集合与每个污染区间的原因、证据引用。"""
    taint: list[tuple[int, int, str, tuple[str, ...]]] = []
    cuts = {0, case.cluster_size}

    # 再分配与记录缺口的作用域是整个物理簇。
    for event in allocations[physical_cluster]:
        taint.append((
            0,
            case.cluster_size,
            REALLOCATED,
            (f"allocation:{event.event_id}->object:{event.object_id}",),
        ))

    for event in gaps[physical_cluster]:
        taint.append((
            0,
            case.cluster_size,
            RECORD_GAP,
            (f"record_gap:{event.event_id}",),
        ))

    for event in overwrites[physical_cluster]:
        cuts.add(event.phy_start)
        cuts.add(event.phy_end)
        taint.append((
            event.phy_start,
            event.phy_end,
            OVERWRITTEN,
            (f"overwrite:{event.event_id}@t{event.time}",),
        ))

    return cuts, taint


def _taint_for_range(taint, start: int, end: int):
    """返回覆盖 [start, end) 的报告原因（按优先级）与全部相关证据。

    原因只报最严重的一类，但证据保留所有命中事件，便于人工回查
    归属断裂、记录缺口与改写之间的交错关系。
    """
    hits = {reason: [] for reason in _TAINT_PRIORITY}
    for t_start, t_end, reason, evidence in taint:
        if _intersect(start, end, t_start, t_end) is not None:
            hits[reason].extend(evidence)
    chosen = None
    all_evidence: list[str] = []
    for reason in _TAINT_PRIORITY:
        if hits[reason]:
            if chosen is None:
                chosen = reason
            all_evidence.extend(hits[reason])
    if chosen is None:
        return None, ()
    return chosen, tuple(all_evidence)


def _acquisition_for_range(chunk: Chunk | None, start: int, end: int):
    """对簇内 [start, end) 做采集层面校验，返回 (reason, data_or_None)。"""
    if chunk is None:
        return ACQUISITION_MISSING, None
    if chunk.status == "missing":
        return ACQUISITION_MISSING, None
    if chunk.status == "error":
        return ACQUISITION_ERROR, None
    # status == "ok"
    if len(chunk.data) < end:
        return ACQUISITION_SHORT, None
    # 显式拷贝：整簇切片在 CPython 下会返回同一 bytes 对象，
    # 经 bytearray 中转保证报告数据与输入证据完全解耦。
    return RECOVERED, bytes(bytearray(chunk.data[start:end]))


def _make_segment(index, logical_cluster: int,
                  file_start: int, file_end: int,
                  physical_cluster: int | None,
                  cluster_start: int | None, cluster_end: int | None,
                  status: str, detail: str, evidence: tuple[str, ...],
                  data: bytes | None) -> Segment:
    return Segment(
        index=index,
        file_offset_start=file_start,
        file_offset_end=file_end,
        logical_cluster=logical_cluster,
        status=status,
        reason=status,
        detail=detail,
        data=data,
        physical_cluster=physical_cluster,
        cluster_offset_start=cluster_start,
        cluster_offset_end=cluster_end,
        evidence=evidence,
    )


def recover_file(case: RecoveryCase) -> "RecoveryReport":  # noqa: F821
    """对一个 :class:`RecoveryCase` 执行只读取证恢复，返回完整报告。"""
    allocations, overwrites, gaps = _group_events(case)
    segments: list[Segment] = []
    index = 0
    total_clusters = (
        case.file_size + case.cluster_size - 1
    ) // case.cluster_size

    for logical_cluster in range(total_clusters):
        file_start = logical_cluster * case.cluster_size
        cluster_end_file = min(file_start + case.cluster_size, case.file_size)
        valid_len = cluster_end_file - file_start  # 末簇在此裁切

        # 簇链未覆盖的逻辑簇：无映射，整簇拒绝。
        if logical_cluster >= len(case.cluster_chain):
            segments.append(_make_segment(
                index, logical_cluster, file_start, cluster_end_file,
                None, None, None,
                MAPPING_MISSING,
                f"逻辑簇 {logical_cluster} 在簇链中没有物理簇映射，"
                f"末簇裁切后长度 {valid_len} 字节",
                (), None,
            ))
            index += 1
            continue

        physical_cluster = case.cluster_chain[logical_cluster]
        chain_ref = (f"chain:L{logical_cluster}->P{physical_cluster}",)
        cuts, taint = _collect_taint(
            case, physical_cluster, allocations, gaps, overwrites
        )
        # 末簇裁切边界也要参与切分，保证不越出原文件大小。
        cuts.add(valid_len)
        boundaries = sorted(cuts)

        chunk = case.chunks.get(physical_cluster)
        for cut_start, cut_end in zip(boundaries, boundaries[1:]):
            if cut_start >= valid_len:
                break
            cut_end = min(cut_end, valid_len)
            seg_file_start = file_start + cut_start
            seg_file_end = file_start + cut_end

            # 第一层：归属/内容证据。
            reason, taint_evidence = _taint_for_range(
                taint, cut_start, cut_end
            )
            if reason is not None:
                segments.append(_make_segment(
                    index, logical_cluster,
                    seg_file_start, seg_file_end,
                    physical_cluster, cut_start, cut_end,
                    reason,
                    f"{reason_text(reason)}；物理簇 {physical_cluster}"
                    f" 内 [{cut_start}, {cut_end}) 不可作为原字节",
                    chain_ref + taint_evidence, None,
                ))
                index += 1
                continue

            # 第二层：采集证据。
            acq_reason, data = _acquisition_for_range(
                chunk, cut_start, cut_end
            )
            if acq_reason is not RECOVERED:
                detail = reason_text(acq_reason)
                if acq_reason == ACQUISITION_ERROR and chunk is not None:
                    detail = f"{detail}: {chunk.error}"
                elif acq_reason == ACQUISITION_SHORT and chunk is not None:
                    detail = (
                        f"{detail}: 需要簇内前 {cut_end} 字节，"
                        f"实际仅采集到 {len(chunk.data)} 字节"
                    )
                segments.append(_make_segment(
                    index, logical_cluster,
                    seg_file_start, seg_file_end,
                    physical_cluster, cut_start, cut_end,
                    acq_reason, detail, chain_ref, None,
                ))
                index += 1
                continue

            segments.append(_make_segment(
                index, logical_cluster,
                seg_file_start, seg_file_end,
                physical_cluster, cut_start, cut_end,
                RECOVERED,
                f"删除后归属连续且采集成功；物理簇 {physical_cluster}"
                f" 内 [{cut_start}, {cut_end}) 与文件偏移一一对应",
                chain_ref, data,
            ))
            index += 1

    return build_report(case, segments)
