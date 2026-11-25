"""播报计划：在时长预算内选能区分目标与所有其他候选的最短播报。

区分规则
--------
对某一组“被朗读的字段” F，目标 t 与另一候选 o 可区分，当且仅当 F 中存在
某个字段编号 f，使得 t 与 o 在该字段上的听感签名互不相交：

* 两边都有该字段：两个别名集合归一后的听感键集合若相交，则它们“可能听起来
  一样”，视为无法区分；完全不相交才算区分。
* o 没有该字段：o 根本发不出这个声音，故可区分。

安全取向是“宁可认为不可区分”：只要存在某个读法让两笔听起来相同，就不把它
当成区分依据。

最短
----
按 (总时长, 朗读的字段编号序列) 的字典序取最小：

1. 总时长更短者优先（在预算内）；
2. 总时长相同（等长）时，字段编号序列按字典序更小者优先。

朗读的字段编号序列，是所选播报段按 segment_index 升序、段内按 field_ids
给定顺序拼接出的编号元组。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .model import Candidate, CandidateBook, Segment
from .sound import HomophoneTable, sound_key

__all__ = ["ConfirmationPlan", "sound_signature", "plan_confirmation"]


@dataclass(frozen=True)
class ConfirmationPlan:
    """一次确认播报的计划。"""

    target_id: str
    segments: tuple[Segment, ...]
    field_ids: tuple[int, ...]
    total_duration_ms: int

    def __post_init__(self) -> None:
        if self.total_duration_ms != sum(s.duration_ms for s in self.segments):
            raise ValueError("总时长与各段时长之和不一致")
        expected: list[int] = []
        for seg in self.segments:
            expected.extend(seg.field_ids)
        if tuple(expected) != self.field_ids:
            raise ValueError("字段编号序列与播报段不一致")

    @property
    def segment_indices(self) -> tuple[int, ...]:
        return tuple(s.segment_index for s in self.segments)


def sound_signature(
    candidate: Candidate, field_id: int, table: HomophoneTable | None = None
) -> frozenset[str] | None:
    """候选某字段的听感签名；候选没有该字段时返回 None。"""
    try:
        f = candidate.field_by_id(field_id)
    except KeyError:
        return None
    keys = frozenset(sound_key(alias, table) for alias in f.aliases)
    if not keys or "" in keys:
        raise ValueError(
            f"候选 {candidate.candidate_id!r} 字段 {field_id} 的别名归一后为空"
        )
    return keys


def _fields_differ(
    target: Candidate,
    other: Candidate,
    field_ids: Iterable[int],
    table: HomophoneTable | None,
) -> bool:
    """在给定字段集合上，目标与其他候选是否可以区分。"""
    for fid in field_ids:
        t_sig = sound_signature(target, fid, table)
        if t_sig is None:  # 目标都没有的字段不应出现在朗读集合里
            raise ValueError(f"目标 {target.candidate_id!r} 没有字段 {fid}")
        o_sig = sound_signature(other, fid, table)
        if o_sig is None:
            return True
        if t_sig.isdisjoint(o_sig):
            return True
    return False


def _validate_inputs(
    book: CandidateBook,
    target: Candidate,
    required_fields: Sequence[int],
    segments: Sequence[Segment],
) -> tuple[Segment, ...]:
    target_ids = {f.field_id for f in target.fields}
    for fid in required_fields:
        if fid not in target_ids:
            raise ValueError(f"必读字段 {fid} 不在目标候选 {target.candidate_id!r} 中")
    seen_idx: set[int] = set()
    for seg in segments:
        if seg.segment_index in seen_idx:
            raise ValueError(f"播报段编号重复: {seg.segment_index}")
        seen_idx.add(seg.segment_index)
        for fid in seg.field_ids:
            if fid not in target_ids:
                raise ValueError(
                    f"播报段 {seg.segment_index} 含目标候选不存在的字段 {fid}"
                )
    # 预先触发签名计算的别名校验，尽早暴露非法别名。
    for cand in book.candidates:
        for f in cand.fields:
            sound_signature(cand, f.field_id, None)
    return tuple(sorted(segments, key=lambda s: s.segment_index))


def plan_confirmation(
    book: CandidateBook,
    target_id: str,
    required_fields: Iterable[int],
    segments: Sequence[Segment],
    budget_ms: int,
    *,
    homophones: HomophoneTable | None = None,
) -> ConfirmationPlan | None:
    """为 ``target_id`` 选出最短的确认播报。

    返回 ``ConfirmationPlan``；若预算内不存在既覆盖必读字段、又能区分目标的
    播报，返回 ``None``。结构性问题（未知目标、字段编号非法、段编号重复等）
    会抛 ``ValueError``/``KeyError``。
    """
    if budget_ms < 0:
        raise ValueError("时长预算不能为负")
    target = book.by_id(target_id)
    required = tuple(dict.fromkeys(required_fields))
    ordered = _validate_inputs(book, target, required, segments)
    others = book.others(target_id)

    required_set = set(required)
    # 每个必读字段最早可被哪一段覆盖的后缀信息，用于可行性剪枝。
    n = len(ordered)

    best_key: tuple[int, tuple[int, ...]] | None = None
    best_segments: tuple[Segment, ...] | None = None

    chosen: list[Segment] = []

    def evaluate() -> None:
        nonlocal best_key, best_segments
        if not chosen:
            return
        field_ids: list[int] = []
        for seg in chosen:
            field_ids.extend(seg.field_ids)
        if not required_set.issubset(field_ids):
            return
        if any(not _fields_differ(target, o, field_ids, homophones) for o in others):
            return
        duration = sum(seg.duration_ms for seg in chosen)
        key = (duration, tuple(field_ids))
        if best_key is None or key < best_key:
            best_key = key
            best_segments = tuple(chosen)

    def dfs(i: int, duration: int, covered: set[int]) -> None:
        if duration > budget_ms:
            return
        if best_key is not None and duration > best_key[0]:
            return
        if i == n:
            evaluate()
            return
        # 不选第 i 段
        dfs(i + 1, duration, covered)
        # 选第 i 段
        seg = ordered[i]
        chosen.append(seg)
        dfs(i + 1, duration + seg.duration_ms, covered | set(seg.field_ids))
        chosen.pop()

    dfs(0, 0, set())

    if best_segments is None:
        return None
    field_ids: list[int] = []
    for seg in best_segments:
        field_ids.extend(seg.field_ids)
    return ConfirmationPlan(
        target_id=target_id,
        segments=best_segments,
        field_ids=tuple(field_ids),
        total_duration_ms=best_key[0],
    )
