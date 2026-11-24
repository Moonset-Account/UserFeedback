"""精确汇总：生成每轮每上游的最小通告集。

规则（对应题面）：

* 只有同一租户（同主）且路由策略标签相同的相邻子树才能精确汇总；
* 不同租户/不同策略的区间若重叠属于输入错误，直接拒绝；
* 汇总只能覆盖被持有的地址，空洞永远不会被宣告；
* 输出一个区间并集的规范（最少条数）CIDR 分解。
"""

from __future__ import annotations

from .prefix import BITS, SPACE_SIZE, Prefix, intervals_disjoint, merge_intervals


def decompose_interval(start: int, end: int) -> list[Prefix]:
    """把任意区间 ``[start, end)`` 规范分解为最少的对齐 CIDR 前缀。

    每一步取起点处能放下的最大对齐块，结果唯一且条数最少。
    只做端点与位运算，不枚举地址。
    """
    if start >= end:
        return []
    if not 0 <= start < end <= SPACE_SIZE:
        raise ValueError("区间超出 128 位地址空间")
    result: list[Prefix] = []
    cursor = start
    while cursor < end:
        limit = end - cursor
        # cursor 处的对齐粒度：cursor 为 0 时可对齐到整个地址空间。
        align_bits = (cursor & -cursor).bit_length() - 1 if cursor else BITS
        size_bits = limit.bit_length() - 1
        block_bits = min(align_bits, size_bits)
        block = 1 << block_bits
        result.append(Prefix(cursor, BITS - block_bits))
        cursor += block
    return result


class AggregationConflict(ValueError):
    """不同标签（租户/策略）的地址范围重叠，无法在同一上游精确汇总。"""


def minimal_advertisements(
    entries: list[tuple[Prefix, str, str]],
) -> list[tuple[Prefix, str, str]]:
    """计算最小通告集。

    参数 ``entries`` 为 ``(前缀, 租用户名, 路由策略)`` 列表。
    返回按地址排序的 ``(汇总前缀, 租户, 策略)`` 列表，条数最少且不含空洞。
    """
    groups: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for pfx, tenant, policy in entries:
        groups.setdefault((tenant, policy), []).append(pfx.interval)

    merged: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for label, ivs in groups.items():
        merged[label] = merge_intervals(ivs)

    # 不同标签的持有区间必须互不相交。
    all_labels = list(merged)
    for i, label_a in enumerate(all_labels):
        for label_b in all_labels[i + 1 :]:
            for iv_a in merged[label_a]:
                for iv_b in merged[label_b]:
                    if not intervals_disjoint(iv_a, iv_b):
                        raise AggregationConflict(
                            f"租户/策略 {label_a} 与 {label_b} 的地址范围重叠，禁止合并宣告"
                        )

    out: list[tuple[Prefix, str, str]] = []
    for (tenant, policy), ivs in merged.items():
        for start, end in ivs:
            for pfx in decompose_interval(start, end):
                out.append((pfx, tenant, policy))
    out.sort(key=lambda item: (item[0].addr, item[0].length))
    return out


def collapse_siblings(
    entries: list[tuple[Prefix, str, str]],
) -> list[tuple[Prefix, str, str]]:
    """基于 trie 兄弟折叠的参考实现，供测试交叉验证。

    不断把同标签、互为兄弟且父节点完全被同标签占满的两个前缀上提，
    直到无法继续折叠。
    """
    current = {pfx: (tenant, policy) for pfx, tenant, policy in entries}
    changed = True
    while changed:
        changed = False
        for pfx in list(current):
            if pfx.length == 0:
                continue
            sib = pfx.sibling
            parent = pfx.parent
            if sib in current and current[sib] == current[pfx]:
                # 两个等长兄弟的并集恰好填满父节点区间时才能上提。
                union = merge_intervals([pfx.interval, sib.interval])
                if union == [parent.interval]:
                    label = current.pop(pfx)
                    current.pop(sib)
                    current[parent] = label
                    changed = True
                    break
    return sorted(
        ((pfx, tenant, policy) for pfx, (tenant, policy) in current.items()),
        key=lambda item: (item[0].addr, item[0].length),
    )
