"""确认会话：消费播放完成与复诵事件，并在提交时做防替换校验。

安全要点
--------
1. **未播完的段不计入确认**。只有收到 ``completed=True`` 的播放完成事件的段，
   它朗读的字段才进入“已被听到”的集合；必读字段若未被已播完的段覆盖，不能确认。
2. **提交时重新计算区分性**。计划阶段的区分结论不可信：若播报过程中通话里新增
   了另一笔与目标“听起来完全相同”的候选，提交必须退回补充确认，绝不能沿用
   旧的区分结果。因此 ``submit`` 总是拿当前候选快照重新判定。
3. **复诵必须唯一指向目标**。把复诵到的别名拿去和当前所有候选逐字段对音，
   只有恰好一个候选能对上，才算唯一指向；否则拒绝或要求补充。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence

from .model import Candidate, CandidateBook, PlaybackEvent, RepetitionEvent
from .planner import ConfirmationPlan, _fields_differ, sound_signature
from .sound import HomophoneTable, sound_key

__all__ = [
    "SubmitStatus",
    "SubmitResult",
    "ConfirmationSession",
    "candidates_consistent_with_repetitions",
]


class SubmitStatus(str, Enum):
    CONFIRMED = "confirmed"
    SUPPLEMENTAL_REQUIRED = "supplemental_required"
    REJECTED = "rejected"


@dataclass(frozen=True)
class SubmitResult:
    status: SubmitStatus
    reason: str
    detail: str = ""
    confirmed_fields: tuple[int, ...] = ()
    ambiguous_candidate_ids: tuple[str, ...] = ()

    @property
    def confirmed(self) -> bool:
        return self.status is SubmitStatus.CONFIRMED


def _alias_matches(
    signature: frozenset[str] | None, alias: str, table: HomophoneTable | None = None
) -> bool:
    if signature is None:
        return False
    return sound_key(alias, table) in signature


def candidates_consistent_with_repetitions(
    book: CandidateBook,
    repetitions: Iterable[RepetitionEvent],
    table: HomophoneTable | None = None,
) -> tuple[str, ...]:
    """返回能解释全部复诵事件的候选编号（听起来可能的那一笔）。"""
    reps = tuple(repetitions)
    if not reps:
        return tuple(c.candidate_id for c in book.candidates)
    consistent: list[str] = []
    for cand in book.candidates:
        ok = True
        for rep in reps:
            if not rep.matched:
                ok = False
                break
            sig = sound_signature(cand, rep.field_id, table)
            if not _alias_matches(sig, rep.alias, table):
                ok = False
                break
        if ok:
            consistent.append(cand.candidate_id)
    return tuple(consistent)


@dataclass
class ConfirmationSession:
    """一次确认播报的会话。"""

    plan: ConfirmationPlan
    book: CandidateBook
    homophones: HomophoneTable | None = None
    _playback: dict[int, bool] = field(default_factory=dict, init=False)
    _repetitions: list[RepetitionEvent] = field(default_factory=list, init=False)
    _confirmed: bool = field(default=False, init=False)

    @property
    def confirmed(self) -> bool:
        return self._confirmed

    def __post_init__(self) -> None:
        planned = set(self.plan.segment_indices)
        if not self.plan.segments:
            raise ValueError("播报计划为空")
        if len(planned) != len(self.plan.segments):
            raise ValueError("播报计划的段编号重复")
        # 计划必须来自该候选集：目标存在。
        self.book.by_id(self.plan.target_id)

    # -- 事件输入 -----------------------------------------------------
    @property
    def target_id(self) -> str:
        return self.plan.target_id

    def on_playback(self, event: PlaybackEvent) -> None:
        """记录一段的播放完成情况。完成状态不可回退。"""
        if self._confirmed:
            raise RuntimeError("会话已确认，不能再接收播放事件")
        if event.target_id != self.plan.target_id:
            raise ValueError(
                f"播放事件目标 {event.target_id!r} 与会话目标 "
                f"{self.plan.target_id!r} 不一致"
            )
        if event.segment_index not in set(self.plan.segment_indices):
            raise ValueError(f"播放事件含计划外段编号 {event.segment_index}")
        if event.completed:
            self._playback[event.segment_index] = True
        else:
            self._playback.setdefault(event.segment_index, False)

    def on_repetition(self, event: RepetitionEvent) -> None:
        if self._confirmed:
            raise RuntimeError("会话已确认，不能再接收复诵事件")
        self._repetitions.append(event)

    def update_candidates(self, book: CandidateBook) -> None:
        """通话中候选集发生变化（例如新增一笔）时更新快照。"""
        if self._confirmed:
            raise RuntimeError("会话已确认，不能再更新候选集")
        book.by_id(self.plan.target_id)
        self.book = book

    # -- 查询 ---------------------------------------------------------
    def completed_segment_indices(self) -> tuple[int, ...]:
        return tuple(
            s.segment_index
            for s in self.plan.segments
            if self._playback.get(s.segment_index, False)
        )

    def announced_fields(self) -> tuple[int, ...]:
        """仅由已播完的段贡献的字段编号序列。"""
        ids: list[int] = []
        for seg in self.plan.segments:
            if self._playback.get(seg.segment_index, False):
                ids.extend(seg.field_ids)
        return tuple(ids)

    # -- 提交 ---------------------------------------------------------
    def submit(
        self,
        required_fields: Iterable[int],
        *,
        book: CandidateBook | None = None,
    ) -> SubmitResult:
        """提交确认。

        ``required_fields`` 为本次确认的必读字段编号；``book`` 为提交时刻的候选
        快照，缺省使用会话内最新快照。
        """
        current = book if book is not None else self.book
        target = current.by_id(self.plan.target_id)
        required = tuple(dict.fromkeys(required_fields))
        for fid in required:
            if fid not in {f.field_id for f in target.fields}:
                raise ValueError(f"必读字段 {fid} 不在目标候选中")
        if self._confirmed:
            raise RuntimeError("会话已确认，不能重复提交")

        announced = self.announced_fields()
        announced_set = set(announced)

        if not announced:
            return SubmitResult(
                status=SubmitStatus.SUPPLEMENTAL_REQUIRED,
                reason="no_completed_segments",
                detail="没有任何播报段完成，不能确认",
            )

        # 1) 必读字段必须全部由已播完的段覆盖。
        missing = tuple(fid for fid in required if fid not in announced_set)
        if missing:
            return SubmitResult(
                status=SubmitStatus.SUPPLEMENTAL_REQUIRED,
                reason="unfinished_segments",
                detail=(
                    "必读字段尚未由已播完的段覆盖（未播完的段不计入确认）: "
                    + ", ".join(map(str, missing))
                ),
                confirmed_fields=tuple(
                    fid for fid in announced if fid in set(required)
                ),
            )

        # 2) 用当前候选集重算区分性，绝不沿用计划期结论。
        others = current.others(self.plan.target_id)
        indistinguishable = tuple(
            o.candidate_id
            for o in others
            if not _fields_differ(target, o, announced, self.homophones)
        )
        if indistinguishable:
            return SubmitResult(
                status=SubmitStatus.SUPPLEMENTAL_REQUIRED,
                reason="new_indistinguishable_candidate",
                detail=(
                    "所播内容无法与当前候选区分，需补充确认: "
                    + ", ".join(indistinguishable)
                ),
                confirmed_fields=announced,
                ambiguous_candidate_ids=indistinguishable,
            )

        # 3) 复诵必须覆盖每个已播字段，且每条都匹配。
        reps_by_field: dict[int, list[RepetitionEvent]] = {}
        for rep in self._repetitions:
            reps_by_field.setdefault(rep.field_id, []).append(rep)

        repeated_unannounced = tuple(
            fid for fid in reps_by_field if fid not in announced_set
        )
        if repeated_unannounced:
            return SubmitResult(
                status=SubmitStatus.SUPPLEMENTAL_REQUIRED,
                reason="repetition_for_unannounced_field",
                detail=(
                    "复诵了未在已播段中出现的字段，需补充确认: "
                    + ", ".join(map(str, repeated_unannounced))
                ),
                confirmed_fields=announced,
            )

        not_repeated = tuple(fid for fid in announced if fid not in reps_by_field)
        if not_repeated:
            return SubmitResult(
                status=SubmitStatus.SUPPLEMENTAL_REQUIRED,
                reason="missing_repetition",
                detail=(
                    "以下已播字段缺少复诵: " + ", ".join(map(str, not_repeated))
                ),
                confirmed_fields=announced,
            )

        if any(not rep.matched for rep in self._repetitions):
            return SubmitResult(
                status=SubmitStatus.REJECTED,
                reason="repetition_mismatch",
                detail="存在与播报内容不一致的复诵",
                confirmed_fields=announced,
            )

        # 4) 复诵内容要唯一指向目标。
        consistent = candidates_consistent_with_repetitions(
            current, self._repetitions, self.homophones
        )
        if consistent != (self.plan.target_id,):
            return SubmitResult(
                status=SubmitStatus.REJECTED
                if not consistent
                else SubmitStatus.SUPPLEMENTAL_REQUIRED,
                reason="repetition_not_unique" if consistent else "repetition_mismatch",
                detail=(
                    "复诵内容无法唯一指向目标，可能的候选: "
                    + (", ".join(consistent) if consistent else "无")
                ),
                confirmed_fields=announced,
                ambiguous_candidate_ids=tuple(
                    cid for cid in consistent if cid != self.plan.target_id
                ),
            )

        self._confirmed = True
        return SubmitResult(
            status=SubmitStatus.CONFIRMED,
            reason="ok",
            detail="播报已完整、复诵一致且唯一指向目标",
            confirmed_fields=announced,
        )
