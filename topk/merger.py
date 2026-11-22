"""TopK 合并器与早停证明。

合并器本身不关心分页调度，只接收一页页候选并维护：

* ``_seen``：已处理 ID -> 首次见到的候选，用于去重与一致性校验；
* ``_heaps``：每个分类一个最小堆，堆顶是该分类最差的已选项；
* 读取账本：每个分片读过哪些页。

早停证明（``can_stop``）
------------------------

设分片 ``s`` 对分类 ``c`` 给出的后续页边界为
``B(s,c) = (max_score, min_id_at_max)``。

1. 若分类 ``c`` **未满额**，任何未读的 ``c`` 候选都可能入选，因此要求
   所有分片对 ``c`` 都不再有候选（无边界）。
2. 若分类 ``c`` **已满额**，设其最差已选项位置为 ``(w_score, w_id)``。
   只有当未读候选严格优于该位置时才可能挤掉它，故要求所有分片的
   ``B(s,c)`` 都不严格优于 ``(w_score, w_id)``。
3. 边界与最差位置“相等”（同分且同 ID）时，后续页至多出现同一 ID 的副本，
   去重后不改变选中集合，安全；证明不足时返回 ``False``，驱动继续读。

配额为 0 或配额表中不存在的分类永远不会入选，其边界不参与证明。
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from typing import Mapping, Optional, Sequence

from .models import Bound, Candidate, HeapEntry, ShardSummary, TopKResult
from .shard import Shard


class TopKMerger:
    def __init__(self, quotas: Mapping[str, int]) -> None:
        self._quotas: dict[str, int] = dict(quotas)
        for cat, quota in self._quotas.items():
            if quota < 0:
                raise ValueError(f"分类 {cat} 的配额不能为负: {quota}")
        self._seen: dict[str, Candidate] = {}
        self._heaps: dict[str, list[HeapEntry]] = defaultdict(list)
        self._pages_read: dict[str, list[int]] = defaultdict(list)

    # ------------------------------------------------------------------ #
    # 候选吸收
    # ------------------------------------------------------------------ #
    def add_page(self, shard_id: str, page_index: int,
                 candidates: Sequence[Candidate]) -> None:
        self._pages_read[shard_id].append(page_index)
        for cand in candidates:
            self._add_candidate(cand)

    def _add_candidate(self, cand: Candidate) -> None:
        prior = self._seen.get(cand.id)
        if prior is not None:
            if prior.score != cand.score or prior.category != cand.category:
                raise ValueError(
                    f"ID {cand.id} 的副本属性不一致: "
                    f"首次=({prior.score}, {prior.category}) "
                    f"再次=({cand.score}, {cand.category})"
                )
            # 同一查询版本下的重复副本，去重忽略。
            return
        self._seen[cand.id] = cand

        quota = self._quotas.get(cand.category, 0)
        if quota <= 0:
            return
        heap = self._heaps[cand.category]
        heapq.heappush(heap, HeapEntry.of(cand))
        if len(heap) > quota:
            heapq.heappop(heap)

    # ------------------------------------------------------------------ #
    # 早停证明
    # ------------------------------------------------------------------ #
    def worst_selected(self, category: str) -> Optional[tuple[float, str]]:
        """分类最差已选项位置 ``(score, id)``；未选中任何对象时为 None。"""
        heap = self._heaps.get(category)
        if not heap:
            return None
        entry = heap[0]
        return (entry.score, entry.candidate.id)

    def category_full(self, category: str) -> bool:
        quota = self._quotas.get(category, 0)
        return quota > 0 and len(self._heaps.get(category, ())) >= quota

    def can_stop(
        self, shard_bounds: Mapping[str, Mapping[str, Bound]]
    ) -> tuple[bool, str]:
        """判定当前状态下未读候选是否不可能改变选中集合。

        ``shard_bounds`` 形如 ``{shard_id: {category: Bound}}``，必须覆盖
        全部参与分片（耗尽分片给空映射）。返回 ``(是否可停, 原因)``。
        """
        for category, quota in self._quotas.items():
            if quota <= 0:
                continue
            if not self.category_full(category):
                # 未满额：任一分片仍可能给出该分类的新候选。
                for shard_id, bounds in shard_bounds.items():
                    if category in bounds:
                        return (
                            False,
                            f"分类 {category} 未满额，分片 {shard_id} 仍有候选",
                        )
                continue
            # 已满额：最差已选项仍可能被更优的同类未读候选替换。
            worst_score, worst_id = self.worst_selected(category)  # type: ignore[misc]
            for shard_id, bounds in shard_bounds.items():
                bound = bounds.get(category)
                if bound is None:
                    continue
                if bound.better_than(worst_score, worst_id):
                    return (
                        False,
                        f"分类 {category} 最差已选项 ({worst_score}, {worst_id}) "
                        f"可能被分片 {shard_id} 的边界 {bound} 替换",
                    )
        return True, "所有分类要么满额且边界不优于最差已选项，要么无未读候选"

    # ------------------------------------------------------------------ #
    # 结果组装
    # ------------------------------------------------------------------ #
    def selected_sorted(self) -> tuple[Candidate, ...]:
        all_items: list[Candidate] = []
        for heap in self._heaps.values():
            all_items.extend(entry.candidate for entry in heap)
        all_items.sort(key=lambda c: (-c.score, c.id))
        return tuple(all_items)

    def per_category_counts(self) -> dict[str, int]:
        return {
            cat: len(heap)
            for cat, heap in self._heaps.items()
            if self._quotas.get(cat, 0) > 0
        }

    def pages_read_snapshot(self) -> dict[str, tuple[int, ...]]:
        return {
            shard_id: tuple(sorted(pages))
            for shard_id, pages in self._pages_read.items()
        }

    def build_result(
        self,
        shards: Sequence[Shard],
        stop_reason: str,
        early_stopped: bool,
    ) -> TopKResult:
        used_bounds: dict[str, tuple[Bound, ...]] = {}
        summaries: list[ShardSummary] = []
        for shard in shards:
            bounds = shard.frontier_bounds()
            used_bounds[shard.shard_id] = tuple(
                sorted(bounds.values(), key=lambda b: b.category)
            )
            summaries.append(
                ShardSummary(
                    shard_id=shard.shard_id,
                    pages_read=shard.pages_read,
                    total_pages=shard.total_pages,
                    frontier_bounds=frozenset(bounds.values()),
                )
            )
        return TopKResult(
            selected=self.selected_sorted(),
            per_category_counts=self.per_category_counts(),
            pages_read=self.pages_read_snapshot(),
            used_bounds=used_bounds,
            early_stopped=early_stopped,
            stop_reason=stop_reason,
            shard_summaries=tuple(summaries),
        )
