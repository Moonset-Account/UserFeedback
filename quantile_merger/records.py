"""原始记录类型与输入校验。

一条 :class:`Observation` 表示某个副本对同一源、同一时间段的一套分桶计数。
桶为整数闭区间，互不相交且恰好覆盖整个有限值域 ``[lo, hi]``，
各桶计数之和即为该段样本总数（不同观测必须一致，否则联合不可行）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


class ValidationError(ValueError):
    """观测记录本身结构非法（区别于多条记录之间的约束矛盾）。"""


@dataclass(frozen=True)
class Bucket:
    """整数闭区间桶 ``[lo, hi]`` 及其样本计数。"""

    lo: int
    hi: int
    count: int

    def __post_init__(self) -> None:
        if not isinstance(self.lo, int) or not isinstance(self.hi, int):
            raise ValidationError("桶边界必须是整数")
        if not isinstance(self.count, int) or isinstance(self.count, bool):
            raise ValidationError("桶计数必须是整数")
        if self.lo > self.hi:
            raise ValidationError(f"桶区间非法: [{self.lo}, {self.hi}]")
        if self.count < 0:
            raise ValidationError(f"桶计数不能为负: {self.count}")

    def to_dict(self) -> dict:
        return {"lo": self.lo, "hi": self.hi, "count": self.count}

    @classmethod
    def from_dict(cls, d: dict) -> "Bucket":
        return cls(int(d["lo"]), int(d["hi"]), int(d["count"]))


@dataclass(frozen=True)
class Observation:
    """单条分桶观测（一个观测系列的一个修订）。

    ``series_id`` 标识观测系列；同一 ``(source, segment_start, series_id)``
    下，较大的 ``revision`` 替换较小修订。
    """

    source: str
    segment_start: int
    segment_length: int
    lo: int
    hi: int
    buckets: Tuple[Bucket, ...]
    series_id: str
    revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            raise ValidationError("source 必须是非空字符串")
        if not isinstance(self.series_id, str) or not self.series_id:
            raise ValidationError("series_id 必须是非空字符串")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool):
            raise ValidationError("revision 必须是整数")
        if self.revision < 0:
            raise ValidationError("revision 不能为负")
        if (
            not isinstance(self.segment_start, int)
            or not isinstance(self.segment_length, int)
            or isinstance(self.segment_length, bool)
        ):
            raise ValidationError("段起点/长度必须是整数")
        if self.segment_length <= 0:
            raise ValidationError("segment_length 必须为正")
        if not isinstance(self.lo, int) or not isinstance(self.hi, int):
            raise ValidationError("值域边界必须是整数")
        if self.lo > self.hi:
            raise ValidationError(f"值域非法: [{self.lo}, {self.hi}]")
        if not self.buckets:
            raise ValidationError("观测至少包含一个桶")
        if not isinstance(self.buckets, tuple):
            object.__setattr__(self, "buckets", tuple(self.buckets))
        prev_hi = self.lo - 1
        for b in self.buckets:
            if b.lo != prev_hi + 1:
                raise ValidationError(
                    f"桶必须无缝覆盖 [{self.lo},{self.hi}]，"
                    f"在 {b.lo} 处出现断档或重叠"
                )
            if b.hi > self.hi:
                raise ValidationError("桶超出值域上界")
            prev_hi = b.hi
        if prev_hi != self.hi:
            raise ValidationError(
                f"桶必须恰好覆盖 [{self.lo},{self.hi}]，实际终止于 {prev_hi}"
            )

    @property
    def total(self) -> int:
        """该观测声明的段内样本总数。"""
        return sum(b.count for b in self.buckets)

    @property
    def segment_end(self) -> int:
        """段的排他结束时刻（段为 [start, start+length)）。"""
        return self.segment_start + self.segment_length

    def key(self) -> tuple:
        return (self.source, self.segment_start, self.series_id)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "segment_start": self.segment_start,
            "segment_length": self.segment_length,
            "lo": self.lo,
            "hi": self.hi,
            "buckets": [b.to_dict() for b in self.buckets],
            "series_id": self.series_id,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Observation":
        return cls(
            source=d["source"],
            segment_start=int(d["segment_start"]),
            segment_length=int(d["segment_length"]),
            lo=int(d["lo"]),
            hi=int(d["hi"]),
            buckets=tuple(Bucket.from_dict(b) for b in d["buckets"]),
            series_id=d["series_id"],
            revision=int(d["revision"]),
        )
