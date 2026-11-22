"""TopK 算子：从多个分页分片中拉取带分类配额的去重 TopK。

公开 API 见 README.md。
"""

from .models import Bound, Candidate, HeapEntry, TopKResult
from .shard import Shard, build_shards
from .merger import TopKMerger
from .driver import continue_merge, merge_topk, merge_topk_concurrent
from .reference import full_scan_topk

__all__ = [
    "Bound",
    "Candidate",
    "HeapEntry",
    "TopKResult",
    "Shard",
    "build_shards",
    "TopKMerger",
    "continue_merge",
    "merge_topk",
    "merge_topk_concurrent",
    "full_scan_topk",
]
