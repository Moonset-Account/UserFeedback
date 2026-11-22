"""核心数据模型。

排序规则（全算子统一使用）：

* 分数降序；
* 分数相同时 ID 升序。

每分类用最小堆维护已选项，堆顶是该分类“最差的已选项”：分数最低者，
同分时 ID 最大者（见 ``HeapEntry``）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    """单个候选对象。

    同一稳定 ID 的副本在固定查询版本下 ``score`` 与 ``category`` 必须相同，
    合并器会对违反该不变量的输入抛出 ``ValueError``。
    """

    id: str
    score: float
    category: str


@dataclass(frozen=True, order=True)
class HeapEntry:
    """最小堆条目：堆顶必须是同分类中排名最差的已选项。

    “最差”定义（全局排序为分数降序、同分 ID 升序的逆序）：

    * 分数越低越差 -> ``score`` 升序放在首字段；
    * 同分时 ID 越大越差 -> 逆序字符串放在次字段。

    不能用 ``(-score, id)`` 元组：``-score`` 升序会把最高分压到堆顶，
    且同分会按 ID 升序，两处方向都反了。
    """

    score: float
    id_desc: "_ReverseText"
    candidate: Candidate

    @staticmethod
    def of(cand: Candidate) -> "HeapEntry":
        return HeapEntry(
            score=cand.score, id_desc=_ReverseText(cand.id), candidate=cand
        )


class _ReverseText(str):
    """字符串包装：比较方向取反，使 ID 越大在堆中越“小”。"""

    def __lt__(self, other: object) -> bool:
        if isinstance(other, _ReverseText):
            return str.__gt__(self, other)
        return NotImplemented

    def __le__(self, other: object) -> bool:
        if isinstance(other, _ReverseText):
            return str.__ge__(self, other)
        return NotImplemented

    def __gt__(self, other: object) -> bool:
        if isinstance(other, _ReverseText):
            return str.__lt__(self, other)
        return NotImplemented

    def __ge__(self, other: object) -> bool:
        if isinstance(other, _ReverseText):
            return str.__le__(self, other)
        return NotImplemented

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _ReverseText):
            return str.__eq__(self, other)
        return NotImplemented

    def __hash__(self) -> int:
        return str.__hash__(self)


@dataclass(frozen=True)
class Bound:
    """某分片、某分类“后续页”的最优可能边界。

    含义（题面约定）：后续页中该分类的任何候选都满足

    * ``score <= max_score``；
    * 若 ``score == max_score``，则 ``id >= min_id_at_max``。

    即后续页不存在严格优于 ``(max_score, min_id_at_max)`` 的该分类候选
    （排序键为分数降序、同分 ID 升序）。
    """

    category: str
    max_score: float
    min_id_at_max: str

    def better_than(self, score: float, obj_id: str) -> bool:
        """边界是否严格优于给定排序位置 ``(score, obj_id)``。

        严格优于定义：边界分数更高，或分数相同但边界的最小 ID 更小。
        若只是相等（同分且同 ID 位置），后续页至多出现同一 ID 的副本，
        去重后不会改变选中集合，因此不算严格优于。
        """
        if self.max_score > score:
            return True
        if self.max_score < score:
            return False
        return self.min_id_at_max < obj_id


@dataclass(frozen=True)
class Page:
    """一次分页读取的结果。"""

    shard_id: str
    page_index: int
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class ShardSummary:
    """单个分片在算子结束时留下的审计信息。"""

    shard_id: str
    pages_read: int
    total_pages: int
    # 停止时刻该分片给出的后续页边界（已耗尽则为空映射）。
    frontier_bounds: frozenset[Bound]


@dataclass(frozen=True)
class TopKResult:
    """算子的最终产物。"""

    # 去重后的选中对象，已按 (score 降序, id 升序) 排好序。
    selected: tuple[Candidate, ...]
    # 每个分类实际保留数量。
    per_category_counts: dict[str, int]
    # 每个分片实际读取过的页下标。
    pages_read: dict[str, tuple[int, ...]]
    # 停止时刻使用过的边界（参与早停证明的全部边界）。
    used_bounds: dict[str, tuple[Bound, ...]]
    # 是否在读完所有页之前提前停止。
    early_stopped: bool
    # 停止原因，便于测试与审计。
    stop_reason: str
    # 每个分片的完整摘要。
    shard_summaries: tuple[ShardSummary, ...]

    def page_count(self, shard_id: str) -> int:
        return len(self.pages_read.get(shard_id, ()))
