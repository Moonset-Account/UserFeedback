"""NUMA 节点大页申请模块。

对外暴露：

* :class:`HugePageAllocator` —— 大页分配器；
* :class:`NumaNode` —— 固定页框数量的 NUMA 节点；
* :class:`LogicalPage` —— 可被多个虚拟地址别名引用的逻辑页；
* :class:`HugePageRegion` —— 分配结果（节点内对齐且连续的物理区）；
* :class:`AllocationError` —— 空间不足或候选区域全部含固定页时抛出。
"""

from .allocator import AllocationError, HugePageAllocator
from .model import HugePageRegion, LogicalPage, NumaNode

__all__ = [
    "HugePageAllocator",
    "NumaNode",
    "LogicalPage",
    "HugePageRegion",
    "AllocationError",
]
