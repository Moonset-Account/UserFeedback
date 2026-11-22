"""分页分片。

分片对外只暴露两种动作：

1. ``read_page()``：顺序读下一页，耗尽后返回 ``None``；
2. ``frontier_bounds()``：给出**后续页**按分类的最高可能分数，以及该最高分
   对应的最小可能 ID。

本地合成实现里，边界直接由后续页的真实数据计算，因此是紧边界（tight）。
构造器也接受调用方提供的自定义边界，用于测试“边界偏松时算子必须继续读”
这一保守性要求。
"""

from __future__ import annotations

import threading
from typing import Iterable, Mapping, Optional, Sequence

from .models import Bound, Candidate, Page


def _tight_bounds(candidates: Iterable[Candidate]) -> dict[str, Bound]:
    """计算候选集合上每分类的紧边界。"""
    best_score: dict[str, float] = {}
    best_id: dict[str, str] = {}
    for cand in candidates:
        cat = cand.category
        if cat not in best_score:
            best_score[cat] = cand.score
            best_id[cat] = cand.id
            continue
        if cand.score > best_score[cat]:
            best_score[cat] = cand.score
            best_id[cat] = cand.id
        elif cand.score == best_score[cat] and cand.id < best_id[cat]:
            best_id[cat] = cand.id
    return {
        cat: Bound(category=cat, max_score=score, min_id_at_max=best_id[cat])
        for cat, score in best_score.items()
    }


class Shard:
    """单个分页分片，可被多线程安全地顺序拉取。"""

    def __init__(
        self,
        shard_id: str,
        pages: Sequence[Sequence[Candidate]],
        *,
        bound_overrides: Optional[Mapping[int, Mapping[str, Bound]]] = None,
    ) -> None:
        self._shard_id = shard_id
        self._pages: tuple[tuple[Candidate, ...], ...] = tuple(
            tuple(page) for page in pages
        )
        self._cursor = 0
        self._lock = threading.Lock()
        # 游标 k 表示已读完前 k 页；_bounds_by_cursor[k] 必须只描述
        # 未读后续页 k..n-1 的紧边界，k == n 时为空。
        self._bounds_by_cursor: dict[int, dict[str, Bound]] = {}
        overrides = bound_overrides or {}
        suffix_bounds: list[dict[str, Bound]] = [
            {} for _ in range(len(self._pages) + 1)
        ]
        accumulated: list[Candidate] = []
        for idx in range(len(self._pages) - 1, -1, -1):
            accumulated.extend(self._pages[idx])
            suffix_bounds[idx] = _tight_bounds(accumulated)
        for idx, bounds in enumerate(suffix_bounds):
            if idx in overrides:
                self._bounds_by_cursor[idx] = dict(overrides[idx])
            else:
                self._bounds_by_cursor[idx] = dict(bounds)

    @property
    def shard_id(self) -> str:
        return self._shard_id

    @property
    def total_pages(self) -> int:
        return len(self._pages)

    @property
    def pages_read(self) -> int:
        with self._lock:
            return self._cursor

    def exhausted(self) -> bool:
        with self._lock:
            return self._cursor >= len(self._pages)

    def read_page(self) -> Optional[Page]:
        """读取下一页；已耗尽返回 ``None``。"""
        with self._lock:
            if self._cursor >= len(self._pages):
                return None
            page = Page(
                shard_id=self._shard_id,
                page_index=self._cursor,
                candidates=self._pages[self._cursor],
            )
            self._cursor += 1
            return page

    def frontier_bounds(self) -> dict[str, Bound]:
        """当前未读后续页的边界（拷贝，调用方可自由持有）。"""
        with self._lock:
            return dict(self._bounds_by_cursor[self._cursor])

    def seek(self, page_index: int) -> None:
        """把读指针重置到指定页（用于断点续跑测试）。"""
        with self._lock:
            if not 0 <= page_index <= len(self._pages):
                raise IndexError(f"非法页下标: {page_index}")
            self._cursor = page_index


def build_shards(
    raw: Mapping[str, Sequence[Sequence[Candidate]]],
) -> tuple[Shard, ...]:
    """按 ``{shard_id: [page0, page1, ...]}`` 构造一组分片。"""
    return tuple(
        Shard(shard_id=shard_id, pages=pages) for shard_id, pages in raw.items()
    )
