"""大页申请算法。

选择规则（严格按题面）：

1. 候选区域是节点内所有长度为 ``page_count``、起点与 ``page_count``
   对齐（``start % page_count == 0``）的连续物理区；
2. 候选区域内不得含固定页，也不得含已分配区域保留的页框；
3. 区域中的可移动页只能搬到**本节点区域外**的空闲页框，按逻辑页数
   （而非别名数）判断需要多少个搬迁目标框；
4. 在所有可行候选中，先选需要搬迁的逻辑页数最少的，搬迁数相同再按
   物理起点取小；
5. 没有可行候选（空间不足，或所有候选都含固定页）时失败，且失败前
   不修改任何状态，原映射必须仍可重读。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from .model import HugePageRegion, LogicalPage, NumaNode


class AllocationError(Exception):
    """大页申请失败：空间不足，或所有候选区域都含固定页。"""


def _is_power_of_two(value: int) -> bool:
    return isinstance(value, int) and value > 0 and (value & (value - 1)) == 0


class HugePageAllocator:
    """在多个固定大小的 NUMA 节点上执行大页申请。"""

    def __init__(self) -> None:
        self.nodes: Dict[str, NumaNode] = {}
        self._pages: Dict[str, LogicalPage] = {}
        #: 逻辑页标识 -> 所属节点标识（逻辑页不跨节点搬迁）。
        self._page_nodes: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # 拓扑与映射构建
    # ------------------------------------------------------------------
    def add_node(self, node_id: str, frame_count: int) -> NumaNode:
        """登记一个含 ``frame_count`` 个固定页框的 NUMA 节点。"""
        if node_id in self.nodes:
            raise ValueError(f"节点 {node_id} 已存在")
        node = NumaNode(node_id, frame_count)
        self.nodes[node_id] = node
        return node

    def register_page(
        self,
        node_id: str,
        page_id: str,
        frame: int,
        pinned: bool = False,
        aliases: Optional[List[str]] = None,
    ) -> LogicalPage:
        """在指定节点的指定页框上安置一个逻辑页，可同时登记别名。"""
        node = self._node(node_id)
        if page_id in self._pages:
            raise ValueError(f"逻辑页 {page_id} 已登记")
        page = LogicalPage(page_id, pinned=pinned)
        for alias in aliases or []:
            page.aliases.add(alias)
        node.place_page(page, frame)
        self._pages[page_id] = page
        self._page_nodes[page_id] = node_id
        return page

    def add_alias(self, page_id: str, virtual_address: str) -> None:
        """为已存在的逻辑页追加一个虚拟地址别名。"""
        page = self._page(page_id)
        node = self._node(self._page_nodes[page_id])
        node.attach_alias(page, virtual_address)

    def translate(self, node_id: str, virtual_address: str) -> int:
        """重读页表：返回虚拟地址当前指向的节点内 PFN。"""
        node = self._node(node_id)
        if virtual_address not in node.page_table:
            raise KeyError(f"节点 {node_id} 上不存在虚拟地址 {virtual_address}")
        return node.page_table[virtual_address]

    # ------------------------------------------------------------------
    # 核心入口
    # ------------------------------------------------------------------
    def allocate(self, node_id: str, page_count: int) -> HugePageRegion:
        """申请一个含 ``page_count`` 个连续页框的对齐大页区域。

        :raises KeyError: 节点不存在。
        :raises ValueError: ``page_count`` 不是正的二次幂。
        :raises AllocationError: 没有可行区域（空间不足或候选全含固定页）。
        """
        node = self._node(node_id)
        if not _is_power_of_two(page_count):
            raise ValueError("page_count 必须为正的二次幂页数")
        if page_count > node.frame_count:
            raise AllocationError(
                f"节点 {node_id} 仅有 {node.frame_count} 个页框，"
                f"无法容纳 {page_count} 页的区域"
            )

        candidates = self._feasible_candidates(node, page_count)
        if not candidates:
            raise AllocationError(
                self._failure_reason(node, page_count)
            )

        # 先按搬迁逻辑页数、再按物理起点排序，取最优候选。
        candidates.sort(key=lambda item: (item[1], item[0]))
        start, _migration_count, movable_pages = candidates[0]

        # 先在快照上完成全部搬迁与保留，任何异常都回滚，保证原映射可重读。
        snapshot = self._snapshot(node)
        try:
            self._execute(node, start, page_count, movable_pages)
        except Exception:
            self._restore(node, snapshot)
            raise

        return HugePageRegion(node_id=node_id, start_frame=start, page_count=page_count)

    # ------------------------------------------------------------------
    # 候选筛选
    # ------------------------------------------------------------------
    def _feasible_candidates(
        self, node: NumaNode, page_count: int
    ) -> List[Tuple[int, int, List[LogicalPage]]]:
        """返回全部可行候选：(起点, 搬迁逻辑页数, 待搬迁逻辑页列表)。

        对齐约束只作用于物理起点（``start % page_count == 0``），区域
        本身不得越界；节点长度不是页数整数倍时，尾部仍可能存在一个
        合法的对齐起点（例如 24 框节点、4 页申请时的起点 20）。
        """
        outside_free = node.free_frames()
        feasible: List[Tuple[int, int, List[LogicalPage]]] = []

        last_start = node.frame_count - page_count
        for start in range(0, last_start + 1, page_count):
            window = range(start, start + page_count)

            movable_pages: List[LogicalPage] = []
            blocked = False
            for pfn in window:
                # 固定页或已分配区域的保留页框都使候选不可用。
                if pfn in node.reserved_frames:
                    blocked = True
                    break
                page = node.frames[pfn]
                if page is None:
                    continue
                if page.pinned:
                    blocked = True
                    break
                movable_pages.append(page)

            if blocked:
                continue

            # 搬迁目标必须是本节点、候选区域之外的空闲页框。
            targets = outside_free - set(window)
            if len(targets) < len(movable_pages):
                continue

            feasible.append((start, len(movable_pages), movable_pages))

        return feasible

    def _failure_reason(self, node: NumaNode, page_count: int) -> str:
        """区分两类失败，给出可读原因。"""
        last_start = node.frame_count - page_count
        aligned_windows = list(range(0, last_start + 1, page_count))
        blocked_windows = 0
        for start in aligned_windows:
            window = range(start, start + page_count)
            if any(
                pfn in node.reserved_frames
                or (node.frames[pfn] is not None and node.frames[pfn].pinned)
                for pfn in window
            ):
                blocked_windows += 1

        free_count = len(node.free_frames())
        if blocked_windows == len(aligned_windows):
            return (
                f"节点 {node.node_id} 上所有 {page_count} 页对齐候选区域"
                "都含有固定页或已保留页框，无法申请"
            )
        return (
            f"节点 {node.node_id} 空闲页框仅 {free_count} 个，"
            f"不足以在候选区域外腾出 {page_count} 页的连续对齐区域"
        )

    # ------------------------------------------------------------------
    # 搬迁执行与快照回滚
    # ------------------------------------------------------------------
    def _execute(
        self,
        node: NumaNode,
        start: int,
        page_count: int,
        movable_pages: List[LogicalPage],
    ) -> None:
        region_frames = set(range(start, start + page_count))
        # 区域内的空闲框不能作为搬迁目的地；每次搬迁后刷新可用集合。
        available = node.free_frames() - region_frames

        for page in movable_pages:
            dst = min(available)  # 取最小空闲框，行为确定、便于测试
            node.migrate_page(page, dst)
            available.remove(dst)

        node.reserved_frames |= region_frames

    def _snapshot(self, node: NumaNode) -> Dict[str, Any]:
        return {
            "frames": dict(node.frames),
            "page_table": dict(node.page_table),
            "reserved_frames": set(node.reserved_frames),
            "page_frames": {page: page.frame for page in self._pages.values()},
            "page_aliases": {page: set(page.aliases) for page in self._pages.values()},
        }

    def _restore(self, node: NumaNode, snapshot: Dict[str, Any]) -> None:
        node.frames = dict(snapshot["frames"])
        node.page_table = dict(snapshot["page_table"])
        node.reserved_frames = set(snapshot["reserved_frames"])
        for page, frame in snapshot["page_frames"].items():
            page.frame = frame
        for page, aliases in snapshot["page_aliases"].items():
            page.aliases = set(aliases)

    # ------------------------------------------------------------------
    # 辅助查询
    # ------------------------------------------------------------------
    def _node(self, node_id: str) -> NumaNode:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise KeyError(f"未知 NUMA 节点 {node_id}") from None

    def _page(self, page_id: str) -> LogicalPage:
        try:
            return self._pages[page_id]
        except KeyError:
            raise KeyError(f"未知逻辑页 {page_id}") from None
