"""全量顺序扫描参照实现（测试预言机）。

不使用任何分页边界或早停逻辑：收集全部分片的候选、去重、按全局顺序
（分数降序、同分 ID 升序）排序，再为每个分类截取前 ``quota[c]`` 个。
若 TopK 算子正确，其输出必须与本函数的输出逐元素一致。
"""

from __future__ import annotations

from typing import Mapping, Sequence

from .models import Candidate
from .shard import Shard


def full_scan_topk(
    shards: Sequence[Shard], quotas: Mapping[str, int]
) -> tuple[Candidate, ...]:
    """对分片全部页做顺序扫描，返回去重后的每分类 TopK（全局有序）。"""
    # 通过临时把每个分片指针拨到开头再全部读完，避免依赖算子的读取账本；
    # 读取后恢复原指针，保证参照扫描不污染分片状态。
    saved = {shard.shard_id: shard.pages_read for shard in shards}
    try:
        unique: dict[str, Candidate] = {}
        for shard in shards:
            shard.seek(0)
            while True:
                page = shard.read_page()
                if page is None:
                    break
                for cand in page.candidates:
                    prior = unique.get(cand.id)
                    if prior is None:
                        unique[cand.id] = cand
                    elif (prior.score, prior.category) != (
                        cand.score,
                        cand.category,
                    ):
                        raise ValueError(
                            f"ID {cand.id} 的副本属性不一致，参照扫描中止"
                        )
        ordered = sorted(unique.values(), key=lambda c: (-c.score, c.id))
        remaining = dict(quotas)
        selected: list[Candidate] = []
        for cand in ordered:
            left = remaining.get(cand.category, 0)
            if left > 0:
                selected.append(cand)
                remaining[cand.category] = left - 1
        return tuple(selected)
    finally:
        for shard in shards:
            shard.seek(saved[shard.shard_id])
