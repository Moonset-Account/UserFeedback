"""公开端计数一致化模块（差分隐私发布后处理）。

公开 API：
    Node, RegionTree      —— 地区树数据结构
    Reconciliation        —— 校正结果
    ConsistencyError      —— 输入树不合法/不可行时抛出
    reconcile             —— 主求解入口（贪心 + 堆，支持百万级总数）
    brute_force_optimal   —— 小树穷举求解，供核对
"""

from .tree import Node, RegionTree
from .solver import (
    ConsistencyError,
    Reconciliation,
    brute_force_optimal,
    reconcile,
)

__all__ = [
    "Node",
    "RegionTree",
    "Reconciliation",
    "ConsistencyError",
    "reconcile",
    "brute_force_optimal",
]
