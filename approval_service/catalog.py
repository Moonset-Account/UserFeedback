"""目录存储与“某时刻可见视图”的解析。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Tuple

from .model import DirectoryRecord, require_aware


@dataclass(frozen=True)
class ViewResolution:
    """对某时刻可见且已生效记录的解析结果。

    - conflict=True：存在无法通过 supersedes 链消解的冲突记录；
    - representative：无冲突时代表主体当前部门/在岗状态的记录；
    - 空 applicable 表示该时刻没有任何可用目录记录。
    """

    applicable: Tuple[DirectoryRecord, ...]
    roots: Tuple[DirectoryRecord, ...]
    representative: Optional[DirectoryRecord]
    conflict: bool
    conflicting_record_ids: Tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.applicable


_EMPTY = ViewResolution((), (), None, False, ())


def resolve_view(visible_records, at: datetime) -> ViewResolution:
    """基于判定时刻可见的记录解析当前有效视图。

    可见性由调用方按 received_at <= at 过滤；本函数再按 effective_at <= at
    过滤已生效记录，并按显式 supersedes 链排除被取代的记录。
    """
    require_aware(at, "at")
    applicable = [r for r in visible_records if r.effective_at <= at]
    if not applicable:
        return _EMPTY

    superseded_ids = {r.supersedes for r in applicable if r.supersedes}
    roots = [r for r in applicable if r.record_id not in superseded_ids]

    distinct_states = {(r.department, r.on_duty) for r in roots}
    if len(distinct_states) > 1:
        conflicting = tuple(
            sorted(
                r.record_id
                for r in roots
                if any(
                    other is not r
                    and (other.department, other.on_duty)
                    != (r.department, r.on_duty)
                    for other in roots
                )
            )
        )
        return ViewResolution(
            applicable=tuple(sorted(applicable, key=_sort_key)),
            roots=tuple(sorted(roots, key=_sort_key)),
            representative=None,
            conflict=True,
            conflicting_record_ids=conflicting,
        )

    representative = max(roots, key=lambda r: (r.effective_at, r.received_at, r.record_id))
    return ViewResolution(
        applicable=tuple(sorted(applicable, key=_sort_key)),
        roots=tuple(sorted(roots, key=_sort_key)),
        representative=representative,
        conflict=False,
        conflicting_record_ids=(),
    )


def _sort_key(r: DirectoryRecord):
    return r.effective_at, r.received_at, r.record_id


class DirectoryCatalog:
    """目录修订存储；记录一旦写入即不可变、不可覆盖。"""

    def __init__(self) -> None:
        self._records: Dict[str, DirectoryRecord] = {}

    def add_record(self, record: DirectoryRecord) -> None:
        if record.record_id in self._records:
            raise ValueError(f"目录记录 id 重复: {record.record_id}")
        self._records[record.record_id] = record

    def get_record(self, record_id: str) -> DirectoryRecord:
        return self._records[record_id]

    def visible_records(self, subject_id: str, at: datetime) -> Tuple[DirectoryRecord, ...]:
        """返回 at 时刻系统已经接收到的该主体全部目录记录。

        迟到但已接收的历史更正会出现；received_at 晚于 at 的记录对该时刻
        的判定不可见，从而保留“当时系统实际可见输入”的语义。
        """
        require_aware(at, "at")
        return tuple(
            sorted(
                (
                    r
                    for r in self._records.values()
                    if r.subject_id == subject_id and r.received_at <= at
                ),
                key=_sort_key,
            )
        )
