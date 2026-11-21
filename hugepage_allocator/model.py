"""NUMA 大页分配器的数据模型。

模型约定（对应题面）：

* 每个 NUMA 节点拥有固定数量的物理页框（frame），页框以节点内的
  PFN（物理帧号，0 .. frame_count-1）标识；
* 一个逻辑页 :class:`LogicalPage` 驻留在恰好一个页框上，可被多个虚拟
  地址（别名）同时引用；
* 被设备固定（pinned）的逻辑页不可移动，因此含固定页的页框不能作为
  搬迁目标，也不能落入大页区域；
* 节点同时维护一张页表，记录每个虚拟地址当前指向哪个页框；逻辑页迁移
  时，其全部别名都要随逻辑页改指向新页框。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Set


class LogicalPage:
    """一个逻辑页。

    :param page_id: 逻辑页标识（在一个分配器内全局唯一）。
    :param pinned: 为 ``True`` 时该页被设备固定，不可搬迁。
    """

    def __init__(self, page_id: str, pinned: bool = False) -> None:
        self.page_id = page_id
        self.pinned = pinned
        #: 当前驻留页框的节点内 PFN；未安置时为 None。
        self.frame: Optional[int] = None
        #: 引用本逻辑页的全部虚拟地址别名。
        self.aliases: Set[str] = set()

    def __repr__(self) -> str:  # pragma: no cover - 仅便于调试
        state = "pinned" if self.pinned else "movable"
        return f"LogicalPage({self.page_id!r}, {state}, frame={self.frame!r})"


@dataclass(frozen=True)
class HugePageRegion:
    """一次成功大页申请返回的物理区。

    区域覆盖 ``start_frame`` 起、长度为 ``page_count`` 的连续页框，
    且 ``start_frame`` 与 ``page_count`` 对齐（起点是页数量的整数倍）。
    """

    node_id: str
    start_frame: int
    page_count: int

    @property
    def end_frame(self) -> int:
        """区域结束边界（不含），即最后一个页框 PFN + 1。"""
        return self.start_frame + self.page_count

    def frames(self) -> range:
        """返回区域覆盖的全部页框 PFN。"""
        return range(self.start_frame, self.end_frame)


class NumaNode:
    """固定页框数量的 NUMA 节点。"""

    def __init__(self, node_id: str, frame_count: int) -> None:
        if frame_count <= 0:
            raise ValueError("frame_count 必须为正整数")
        self.node_id = node_id
        self.frame_count = frame_count
        #: PFN -> 驻留其上的逻辑页；空闲页框对应值为 None。
        self.frames: Dict[int, Optional[LogicalPage]] = {
            pfn: None for pfn in range(frame_count)
        }
        #: 虚拟地址 -> PFN 的页表。
        self.page_table: Dict[str, int] = {}
        #: 已分配大页区域保留（占用）的页框，后续分配不得复用。
        self.reserved_frames: Set[int] = set()

    def free_frames(self) -> Set[int]:
        """当前真正空闲（无逻辑页驻留且未被已分配区域保留）的页框。"""
        return {
            pfn
            for pfn, page in self.frames.items()
            if page is None and pfn not in self.reserved_frames
        }

    def place_page(self, page: LogicalPage, frame: int) -> None:
        """把逻辑页安置到指定空闲页框，并登记别名映射。"""
        if not (0 <= frame < self.frame_count):
            raise ValueError(f"页框 {frame} 超出节点 {self.node_id} 范围")
        if self.frames[frame] is not None:
            raise ValueError(f"页框 {frame} 已被占用")
        if page.frame is not None:
            raise ValueError(f"逻辑页 {page.page_id} 已驻留在页框 {page.frame}")
        page.frame = frame
        self.frames[frame] = page
        for alias in page.aliases:
            self.page_table[alias] = frame

    def attach_alias(self, page: LogicalPage, virtual_address: str) -> None:
        """为已驻留的逻辑页新增一个虚拟地址别名。"""
        if page.frame is None:
            raise ValueError("逻辑页尚未驻留，无法登记别名")
        if virtual_address in self.page_table:
            raise ValueError(f"虚拟地址 {virtual_address} 已存在映射")
        page.aliases.add(virtual_address)
        self.page_table[virtual_address] = page.frame

    def migrate_page(self, page: LogicalPage, dst_frame: int) -> None:
        """把逻辑页搬迁到本节点的另一个空闲页框。

        源页框变为空闲，页表中该逻辑页的**全部**别名都改指向新页框。
        固定页调用本方法会抛出 :class:`RuntimeError`。
        """
        if page.pinned:
            raise RuntimeError(f"固定页 {page.page_id} 不可搬迁")
        src_frame = page.frame
        if src_frame is None:
            raise RuntimeError(f"逻辑页 {page.page_id} 未驻留，无法搬迁")
        if src_frame == dst_frame:
            return
        if not (0 <= dst_frame < self.frame_count):
            raise ValueError(f"页框 {dst_frame} 超出节点 {self.node_id} 范围")
        if self.frames[dst_frame] is not None:
            raise ValueError(f"目标页框 {dst_frame} 已被占用")

        self.frames[src_frame] = None
        self.frames[dst_frame] = page
        page.frame = dst_frame
        for alias in page.aliases:
            self.page_table[alias] = dst_frame
