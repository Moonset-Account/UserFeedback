"""分页调度驱动。

顺序版 ``merge_topk`` 以轮转方式在分片之间交替拉页，天然覆盖“交错场景”；
并发版 ``merge_topk_concurrent`` 为每个分片起一个线程，通过锁保护的合并器
模拟真实的多路并发拉取。两者结果都必须与全量顺序扫描一致。
"""

from __future__ import annotations

import threading
from typing import Mapping, Sequence

from .merger import TopKMerger
from .models import TopKResult
from .shard import Shard


def _snapshot_bounds(shards: Sequence[Shard]) -> dict[str, dict]:
    return {shard.shard_id: shard.frontier_bounds() for shard in shards}


def merge_topk(
    shards: Sequence[Shard], quotas: Mapping[str, int]
) -> TopKResult:
    """顺序轮转拉取，直到证明未读候选不可能改变选中集合或全部耗尽。"""
    return continue_merge(TopKMerger(quotas), shards)


def continue_merge(
    merger: TopKMerger, shards: Sequence[Shard]
) -> TopKResult:
    """用给定合并器（可已吸收过若干页）继续顺序轮转拉取。

    用于断点续跑：调用方先把恢复出来的历史页重新喂给同一个 ``merger``，
    再调用本函数从分片当前游标继续。
    """
    active = list(shards)

    # 初始证明：所有分片本来就为空（或恢复后已无可改变结果的未读页）。
    ok, reason = merger.can_stop(_snapshot_bounds(shards))
    if ok:
        return merger.build_result(shards, reason, early_stopped=_any_unread(shards))

    while active:
        next_active: list[Shard] = []
        for shard in active:
            page = shard.read_page()
            if page is None:
                continue
            merger.add_page(shard.shard_id, page.page_index, page.candidates)
            # 每吸收一页就重新做一次全局证明。
            ok, reason = merger.can_stop(_snapshot_bounds(shards))
            if ok:
                return merger.build_result(
                    shards, reason, early_stopped=_any_unread(shards)
                )
            if not shard.exhausted():
                next_active.append(shard)
        active = next_active

    ok, reason = merger.can_stop(_snapshot_bounds(shards))
    # 全部耗尽时证明必然成立；若不成立说明实现有缺陷。
    if not ok:
        raise RuntimeError(f"所有分片已耗尽但仍无法证明停止: {reason}")
    return merger.build_result(shards, "所有分片已耗尽", early_stopped=False)


def _any_unread(shards: Sequence[Shard]) -> bool:
    return any(shard.pages_read < shard.total_pages for shard in shards)


def merge_topk_concurrent(
    shards: Sequence[Shard], quotas: Mapping[str, int]
) -> TopKResult:
    """并发拉取版：每分片一个线程，共享同一个加锁合并器。

    并发下可能比顺序版多读若干页（证明成立与线程实际停下之间存在竞态），
    但选中集合必须完全一致，且读取页数不会超过分片总页数。
    """
    merger = _ThreadSafeMerger(quotas)
    stop_event = threading.Event()

    def worker(shard: Shard) -> None:
        while not stop_event.is_set():
            page = shard.read_page()
            if page is None:
                return
            merger.add_page(shard.shard_id, page.page_index, page.candidates)
            if merger.can_stop(_snapshot_bounds(shards))[0]:
                stop_event.set()
                return

    threads = [threading.Thread(target=worker, args=(shard,)) for shard in shards]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    ok, reason = merger.can_stop(_snapshot_bounds(shards))
    if not ok:
        raise RuntimeError(f"并发结束后仍无法证明停止: {reason}")
    return merger.build_result(
        shards, reason, early_stopped=_any_unread(shards)
    )


class _ThreadSafeMerger(TopKMerger):
    """给合并器的写操作与证明加同一把锁。"""

    def __init__(self, quotas: Mapping[str, int]) -> None:
        super().__init__(quotas)
        self._guard = threading.Lock()

    def add_page(self, shard_id, page_index, candidates) -> None:  # type: ignore[no-untyped-def]
        with self._guard:
            super().add_page(shard_id, page_index, candidates)

    def can_stop(self, shard_bounds):  # type: ignore[no-untyped-def]
        with self._guard:
            return super().can_stop(shard_bounds)

    def build_result(self, shards, stop_reason, early_stopped):  # type: ignore[no-untyped-def]
        with self._guard:
            return super().build_result(shards, stop_reason, early_stopped)
