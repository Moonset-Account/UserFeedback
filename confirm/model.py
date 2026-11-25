"""确认服务的数据模型。

术语：
* 候选操作（Candidate）：已认证通话中用户可能指代的一笔待授权操作，带若干字段；
* 字段别名（aliases）：某字段值可以怎样被朗读出来，例如「尾号 1234」读作 “一二三四”；
* 必读字段（required_fields）：任何确认播报都必须包含的字段编号；
* 播报段（Segment）：一段播报，包含若干字段，有明确时长，播放完成事件到达时才算“播完”；
* 复诵事件（Repetition）：用户复诵了某字段的某个别名，带匹配得分或是否匹配。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

__all__ = [
    "Field",
    "Candidate",
    "CandidateBook",
    "Segment",
    "PlaybackEvent",
    "RepetitionEvent",
]


@dataclass(frozen=True)
class Field:
    """候选操作的一个字段。

    field_id 为字段编号（整数，越小越“靠前”，用于等长时的确定性命中）；
    aliases 为该字段值所有“听起来可能”的朗读形式。
    """

    field_id: int
    name: str
    aliases: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.aliases:
            raise ValueError(f"字段 {self.name!r} 至少需要一个可朗读别名")
        if not isinstance(self.field_id, int) or isinstance(self.field_id, bool):
            raise TypeError("field_id 必须是整数")
        for alias in self.aliases:
            if not isinstance(alias, str) or not alias:
                raise ValueError("别名必须是非空字符串")

    def alias_list(self) -> tuple[str, ...]:
        return self.aliases


@dataclass(frozen=True)
class Candidate:
    """一笔候选待授权操作。"""

    candidate_id: str
    fields: tuple[Field, ...]
    label: str = ""

    def __post_init__(self) -> None:
        if not self.candidate_id:
            raise ValueError("candidate_id 不能为空")
        if not self.fields:
            raise ValueError(f"候选 {self.candidate_id!r} 至少需要一个字段")
        seen: set[int] = set()
        for f in self.fields:
            if f.field_id in seen:
                raise ValueError(f"候选 {self.candidate_id!r} 字段编号重复: {f.field_id}")
            seen.add(f.field_id)
        if not isinstance(self.label, str):
            raise TypeError("label 必须是字符串")

    def field_by_id(self, field_id: int) -> Field:
        for f in self.fields:
            if f.field_id == field_id:
                return f
        raise KeyError(f"候选 {self.candidate_id!r} 没有字段 {field_id}")


@dataclass(frozen=True)
class CandidateBook:
    """一次已认证通话中全部候选操作的快照。"""

    candidates: tuple[Candidate, ...]

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ValueError("候选集合不能为空")
        ids: set[str] = set()
        for c in self.candidates:
            if c.candidate_id in ids:
                raise ValueError(f"候选编号重复: {c.candidate_id!r}")
            ids.add(c.candidate_id)

    def by_id(self, candidate_id: str) -> Candidate:
        for c in self.candidates:
            if c.candidate_id == candidate_id:
                return c
        raise KeyError(f"没有候选 {candidate_id!r}")

    def others(self, target_id: str) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.candidate_id != target_id)


@dataclass(frozen=True)
class Segment:
    """一段播报。

    segment_index 为播报内的顺序编号；field_ids 为该段朗读的字段编号；
    duration_ms 为播报时长。只有收到该段播放完成事件，字段才计入确认。
    """

    segment_index: int
    field_ids: tuple[int, ...]
    duration_ms: int

    def __post_init__(self) -> None:
        if self.duration_ms < 0:
            raise ValueError("播报时长不能为负")
        if not self.field_ids:
            raise ValueError("播报段至少包含一个字段")
        if len(set(self.field_ids)) != len(self.field_ids):
            raise ValueError("同一段内字段编号不能重复")

    def covers(self, field_id: int) -> bool:
        return field_id in self.field_ids


@dataclass(frozen=True)
class PlaybackEvent:
    """逐段播放完成事件。"""

    segment_index: int
    target_id: str
    completed: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.segment_index, int) or self.segment_index < 0:
            raise ValueError("segment_index 必须是非负整数")
        if self.completed is not True and self.completed is not False:
            raise TypeError("completed 必须是布尔值")


@dataclass(frozen=True)
class RepetitionEvent:
    """用户对某个字段的复诵事件。

    ``matched`` 表示复诵内容是否与播报内容一致；``alias`` 为复诵出的别名原文，
    服务据此判断该复诵是否唯一指向某个候选字段。
    """

    field_id: int
    alias: str
    matched: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.field_id, int) or isinstance(self.field_id, bool):
            raise TypeError("field_id 必须是整数")
        if not isinstance(self.alias, str) or not self.alias:
            raise ValueError("复诵别名必须是非空字符串")
        if self.matched is not True and self.matched is not False:
            raise TypeError("matched 必须是布尔值")
