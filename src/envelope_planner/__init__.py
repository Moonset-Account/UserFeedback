"""群组密钥下一期封装信封规划模块（LKH 分发树场景）。

核心语义（见 README）：
- 树中每个节点有彼此独立的节点钥；成员叶子持有根到自己叶子路径上的全部节点钥。
- 在节点 v 放置一个信封，只有路径经过 v（即位于 v 子树内）且实际持钥的叶子能打开。
- 选点安全约束：节点钥未泄露、节点可用（给出封装成本）、其可打开集合中不含离组者。
- 必须覆盖每一个在册成员叶子；不可行时逐叶给出原因。
- 可行方案中总成本最低；平局取排序后节点编号序列字典序最小。
"""

from .planner import (
    DistributionTree,
    Envelope,
    EnvelopePlan,
    InputError,
    UncoveredLeaf,
    plan_envelopes,
    simulate_delivery,
)

__all__ = [
    "DistributionTree",
    "Envelope",
    "EnvelopePlan",
    "InputError",
    "UncoveredLeaf",
    "plan_envelopes",
    "simulate_delivery",
]
