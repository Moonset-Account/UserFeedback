"""物理 KV 页、搬页操作与单步调度规划器。

设计要点（对应题面约束）：

* 物理内存由若干编号槽位组成，其中 ``width`` 个是各存活束的“家槽”，
  另有若干堆槽用于存放共享/历史只读页，外加唯一一个临时槽。
* 后端一次只能移动一页，因此所有操作都被排成线性、可逐步执行的序列：
  :class:`DropPage` / :class:`MovePage` / :class:`CopyPage` / :class:`AppendPage`。
* 临时槽仅在重排出现环时启用（环上任意一页先移入临时槽，环即被打开），
  调度结束时临时槽必然恢复为空。
* 目标槽可能恰是另一个孩子尚未读取的源槽：规划器以“先搬空/淘汰、再复制、
  最后追加”的顺序并配合按目的槽迭代的拓扑调度，保证任何写入发生前源页
  都已被读取完毕；执行器对每条约束再次做物理断言。
* 容量不足在任何覆写发生之前就以 :class:`CapacityError` 拒绝，不返回半成品计划。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

__all__ = [
    "PageOp",
    "DropPage",
    "MovePage",
    "CopyPage",
    "AppendPage",
    "PhysicalMemory",
    "CapacityError",
    "build_step_plan",
]


class CapacityError(RuntimeError):
    """物理槽容量（或临时槽可用性）不足，计划在任何覆写前被拒绝。"""


# --------------------------------------------------------------------------- #
# 操作
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PageOp:
    """所有页操作的不可变基类。"""

    pid: int

    def describe(self) -> str:  # pragma: no cover - 由子类覆盖
        raise NotImplementedError


@dataclass(frozen=True)
class DropPage(PageOp):
    """丢弃引用计数归零的死页，释放其槽位。"""

    src: int

    def describe(self) -> str:
        return f"DROP  pid={self.pid} slot={self.src}"


@dataclass(frozen=True)
class MovePage(PageOp):
    """把一页从 ``src`` 物理移动到 ``dst``（移动后源槽变空）。"""

    src: int
    dst: int

    def describe(self) -> str:
        return f"MOVE  pid={self.pid} slot {self.src} -> {self.dst}"


@dataclass(frozen=True)
class CopyPage(PageOp):
    """把 ``src_pid`` 复制为新页 ``pid``，写入物理槽 ``dst``。

    用于同父分叉的写时复制：共享只读历史页保持不动，每个孩子拿到独立可写末页。
    """

    src_pid: int
    dst: int

    def describe(self) -> str:
        return (
            f"COPY  new_pid={self.pid} from pid={self.src_pid} "
            f"-> slot {self.dst}"
        )


@dataclass(frozen=True)
class AppendPage(PageOp):
    """在 ``dst`` 槽的页上追加一个 token（语义上的写入位置）。

    全新页（``fresh=True``）在执行时会先在空槽上创建，再追加首个 token。
    """

    dst: int
    token: int
    fresh: bool = False

    def describe(self) -> str:
        kind = "FRESH+APPEND" if self.fresh else "APPEND"
        return f"{kind} pid={self.pid} slot={self.dst} token={self.token}"


# --------------------------------------------------------------------------- #
# 物理内存与执行器（同时是规划正确性的在线校验器）
# --------------------------------------------------------------------------- #
class PhysicalMemory:
    """固定槽位数的物理 KV 内存，逐操作执行并做完整安全断言。

    槽位编号：

    * ``0 .. width-1``：存活束的家槽（各束独立可写末页所在）；
    * ``width .. width+heap_slots-1``：堆槽（共享只读页/结束束页）；
    * ``width+heap_slots``：唯一临时槽。
    """

    def __init__(self, width: int, heap_slots: int) -> None:
        if width <= 0:
            raise ValueError("width 必须为正整数")
        if heap_slots < 0:
            raise ValueError("heap_slots 不能为负")
        self.width = width
        self.heap_slots = heap_slots
        self.temp_slot = width + heap_slots
        self.total_slots = width + heap_slots + 1
        # slot -> pid；None 表示空槽
        self.slot_of_pid: dict[int, int] = {}
        self._occupancy: dict[int, int] = {s: -1 for s in range(self.total_slots)}
        # pid -> 页内容（token id 序列，充当 KV 内容载体）
        self.pages: dict[int, list[int]] = {}

    # -- 基本查询 ---------------------------------------------------------- #
    def slot_for(self, pid: int) -> int:
        return self.slot_of_pid[pid]

    def occupant(self, slot: int) -> int:
        return self._occupancy[slot]

    def is_empty(self, slot: int) -> bool:
        return self._occupancy[slot] == -1

    def temp_is_free(self) -> bool:
        return self.is_empty(self.temp_slot)

    def free_slots(self) -> List[int]:
        return [s for s in range(self.total_slots) if self.is_empty(s)]

    def page_tokens(self, pid: int) -> tuple[int, ...]:
        return tuple(self.pages[pid])

    def create_empty(self, pid: int, slot: int) -> None:
        """在空槽上创建一个空页（用于根束初始化）。"""
        self._check_slot(slot)
        if not self.is_empty(slot):
            raise AssertionError(f"create_empty 前提失败：槽 {slot} 非空")
        if pid in self.pages:
            raise AssertionError(f"页 pid={pid} 已存在")
        self.pages[pid] = []
        self._occupancy[slot] = pid
        self.slot_of_pid[pid] = slot

    # -- 逐操作执行 -------------------------------------------------------- #
    def apply(self, op: PageOp) -> None:
        if isinstance(op, DropPage):
            self._drop(op)
        elif isinstance(op, MovePage):
            self._move(op)
        elif isinstance(op, CopyPage):
            self._copy(op)
        elif isinstance(op, AppendPage):
            self._append(op)
        else:  # pragma: no cover
            raise TypeError(f"未知操作类型: {op!r}")

    def apply_all(self, ops: Sequence[PageOp]) -> None:
        for op in ops:
            self.apply(op)

    def _check_slot(self, slot: int) -> None:
        if not 0 <= slot < self.total_slots:
            raise AssertionError(f"槽位 {slot} 越界")

    def _drop(self, op: DropPage) -> None:
        self._check_slot(op.src)
        if self._occupancy[op.src] != op.pid:
            raise AssertionError(
                f"DROP 前提失败：槽 {op.src} 中不是 pid={op.pid}，"
                f"实际={self._occupancy[op.src]}"
            )
        self._occupancy[op.src] = -1
        del self.slot_of_pid[op.pid]
        del self.pages[op.pid]

    def _move(self, op: MovePage) -> None:
        self._check_slot(op.src)
        self._check_slot(op.dst)
        if self._occupancy[op.src] != op.pid:
            raise AssertionError(
                f"MOVE 前提失败：源槽 {op.src} 中不是 pid={op.pid}"
            )
        if not self.is_empty(op.dst):
            raise AssertionError(
                f"MOVE 前提失败：目的槽 {op.dst} 非空，"
                f"占用者 pid={self._occupancy[op.dst]}（禁止覆写未读源）"
            )
        self._occupancy[op.src] = -1
        self._occupancy[op.dst] = op.pid
        self.slot_of_pid[op.pid] = op.dst

    def _copy(self, op: CopyPage) -> None:
        self._check_slot(op.dst)
        if op.src_pid not in self.pages:
            raise AssertionError(f"COPY 源页 pid={op.src_pid} 不存在")
        if op.pid in self.pages:
            raise AssertionError(f"COPY 新页 pid={op.pid} 已存在")
        if not self.is_empty(op.dst):
            raise AssertionError(
                f"COPY 前提失败：目的槽 {op.dst} 非空，"
                f"占用者 pid={self._occupancy[op.dst]}（禁止覆写未读源）"
            )
        self.pages[op.pid] = list(self.pages[op.src_pid])
        self._occupancy[op.dst] = op.pid
        self.slot_of_pid[op.pid] = op.dst

    def _append(self, op: AppendPage) -> None:
        self._check_slot(op.dst)
        if op.fresh:
            if not self.is_empty(op.dst):
                raise AssertionError(
                    f"FRESH 前提失败：槽 {op.dst} 非空，"
                    f"占用者 pid={self._occupancy[op.dst]}"
                )
            if op.pid in self.pages:
                raise AssertionError(f"新页 pid={op.pid} 已存在")
            self.pages[op.pid] = []
            self._occupancy[op.dst] = op.pid
            self.slot_of_pid[op.pid] = op.dst
        else:
            if self._occupancy[op.dst] != op.pid:
                raise AssertionError(
                    f"APPEND 前提失败：槽 {op.dst} 中不是 pid={op.pid}"
                )
        self.pages[op.pid].append(op.token)

    # -- 调试 -------------------------------------------------------------- #
    def snapshot(self) -> dict[int, int]:
        """返回槽位 -> pid 的快照（-1 表示空槽）。"""
        return dict(self._occupancy)


# --------------------------------------------------------------------------- #
# 纯函数规划器
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ChildSpec:
    """规划器视角下的一个新末页需求。

    ``home`` 为非负整数表示存活束，其目的槽固定为该家槽；
    为 ``None`` 表示结束假设，末页必须落到堆槽。
    """

    new_pid: Optional[int]      # None 表示直接继承父末页（独占）
    source_pid: Optional[int]   # COPY 来源；None 表示全新页
    home: Optional[int]
    token: int


def build_step_plan(
    *,
    width: int,
    heap_slots: int,
    slots: dict[int, int],
    existing_pids: Sequence[int],
    dead_pids: Sequence[int],
    children: Sequence[ChildSpec],
    retained_pids: Sequence[int] = (),
) -> List[PageOp]:
    """生成一个时间步内可逐步执行的完整操作序列。

    参数
    ----
    width:
        存活束宽度（家槽数）。
    heap_slots:
        堆槽数量；临时槽编号为 ``width + heap_slots``。
    slots:
        现存页 ``pid -> 物理槽`` 映射（规划器不修改它）。
    existing_pids:
        本步新束仍然引用的旧页。
    retained_pids:
        不被任何新束引用、但必须永久保留的旧页（结束假设冻结的历史页）。
        它们参与容量核算，且不会被 DROP。
    dead_pids:
        引用归零、本步必须丢弃的旧页。
    children:
        新末页需求列表，存活束的 ``home`` 必须覆盖 ``0..k-1``（k 为存活数）。

    异常
    ----
    CapacityError:
        容量不足或临时槽被占用；抛出时不会产生任何操作（调用方尚未覆写任何页）。
    """
    total_slots = width + heap_slots + 1
    temp_slot = width + heap_slots
    slots = dict(slots)
    retained_set = set(retained_pids)

    # 输入一致性检查
    for pid in existing_pids:
        if pid not in slots:
            raise ValueError(f"存活页 pid={pid} 缺少槽位映射")
    for pid in dead_pids:
        if pid not in slots:
            raise ValueError(f"死页 pid={pid} 缺少槽位映射")
    for pid in retained_pids:
        if pid not in slots:
            raise ValueError(f"保留页 pid={pid} 缺少槽位映射")
    if set(existing_pids) & set(dead_pids):
        raise ValueError("同一 pid 不能既存活又死亡")
    if retained_set & set(dead_pids):
        raise ValueError("同一 pid 不能既保留又死亡")

    active_homes = sorted(c.home for c in children if c.home is not None)
    expected_homes = list(range(len(active_homes)))
    if active_homes != expected_homes:
        raise ValueError(
            f"存活束家槽必须恰好覆盖 0..{len(expected_homes) - 1}，"
            f"实际={active_homes}"
        )

    # 新页集合（COPY 或 FRESH 产生）
    new_pids = [c.new_pid for c in children if c.new_pid is not None]
    if len(set(new_pids)) != len(new_pids):
        raise ValueError("新页 pid 重复")
    if set(new_pids) & set(slots):
        raise ValueError("新页 pid 与现存页冲突")

    surviving_existing = sorted(set(existing_pids) | retained_set)
    total_pids = len(surviving_existing) + len(new_pids)
    if total_pids > total_slots - 1:
        raise CapacityError(
            f"容量不足：需要 {total_pids} 个页槽（临时槽除外），"
            f"可用 {total_slots - 1} 个"
        )
    if any(s == temp_slot for s in slots.values()):
        raise CapacityError("临时槽在步骤开始时被占用，无法用于打破重排环")

    # ---------------- 1. 目的槽分配 ---------------- #
    # 虚拟占用表：-1 空；否则记录占用 pid。
    # 死页会在第 2 阶段最先 DROP，因此分配阶段就把它们的槽位视为空槽。
    dead_set = set(dead_pids)
    occ: dict[int, int] = {s: -1 for s in range(total_slots)}
    for pid, slot in slots.items():
        if pid not in dead_set:
            occ[slot] = pid

    targets: dict[int, int] = {}   # pid -> 目的槽

    # 1a. 存活束末页固定家槽
    for child in children:
        if child.home is None:
            continue
        pid = child.new_pid if child.new_pid is not None else child.source_pid
        assert pid is not None
        targets[pid] = child.home

    # 1b. 结束假设末页 -> 堆槽（确定性：取最低空闲堆槽）
    def lowest_free(lo: int, hi: int) -> int:
        claimed_slots = set(targets.values())
        for s in range(lo, hi):  # 上界不含临时槽，临时槽永不参与常规分配
            if occ[s] == -1 and s not in claimed_slots:
                return s
        return -1

    for child in children:
        if child.home is not None:
            continue
        pid = child.new_pid if child.new_pid is not None else child.source_pid
        assert pid is not None
        slot = lowest_free(width, temp_slot)
        if slot == -1:
            raise CapacityError(
                f"容量不足：结束假设 pid={pid} 没有可用堆槽"
            )
        targets[pid] = slot

    # 1c. 未被认领的存活旧页 -> 任意空闲槽（堆槽优先，确定性取最低；
    #     堆槽不足时可使用未被存活束认领的空闲家槽；临时槽永不参与）
    claimed = set(targets)
    for pid in sorted(surviving_existing):
        if pid in claimed:
            continue
        slot = lowest_free(width, temp_slot)
        if slot == -1:
            slot = lowest_free(0, width)
        if slot == -1:
            raise CapacityError(f"容量不足：历史页 pid={pid} 无处安放")
        targets[pid] = slot

    # ---------------- 2. 淘汰死页（最先执行，安全释放槽位） ---------------- #
    ops: List[PageOp] = []
    for pid in sorted(dead_pids):
        ops.append(DropPage(pid=pid, src=slots[pid]))

    # ---------------- 3. 迁移旧页（MOVE，含重排环） ---------------- #
    # 仅考虑“现存且目的槽与现槽不同”的页；目的槽固定，出度/入度均 <= 1。
    moves: dict[int, int] = {}
    for pid in surviving_existing:
        dst = targets[pid]
        src = slots[pid]
        if dst != src:
            moves[pid] = dst

    dst_to_pid = {dst: pid for pid, dst in moves.items()}
    moved: set[int] = set()
    # 槽位 -> 当前视图占用者；环首被临时安置时随之更新
    view = dict(occ)

    def virtual_free(slot: int) -> bool:
        holder = view[slot]
        if holder == -1:
            return True
        return holder in moved

    # ---- 阶段 A：所有非环的迁移（重排链），按目的槽升序推进 ----
    # 链的自由端（目的槽本就为空/将被 DROP）先移动，入度 <=1 保证无死锁。
    while True:
        progressed = False
        for dst in sorted(moves.values()):
            pid = dst_to_pid[dst]
            if pid in moved:
                continue
            if virtual_free(dst):
                ops.append(MovePage(pid=pid, src=slots[pid], dst=dst))
                moved.add(pid)
                view[slots[pid]] = -1
                view[dst] = pid
                progressed = True
        if not progressed:
            break

    # ---- 阶段 B：剩余的是若干不相交闭环，用唯一临时槽逐个打破 ----
    # 对每个环：环首（pid 最小者）移入临时槽 -> 其原槽变空，环被打开成链
    # -> 沿“空槽缺口”正向顺移 -> 最后把环首从临时槽接回自己的目的槽。
    remaining = sorted(pid for pid in moves if pid not in moved)
    done_leads: list[tuple[int, int]] = []
    while remaining:
        ring_lead = remaining[0]
        lead_src = slots[ring_lead]
        lead_dst = moves[ring_lead]
        ops.append(MovePage(pid=ring_lead, src=lead_src, dst=temp_slot))
        moved.add(ring_lead)
        view[lead_src] = -1
        view[temp_slot] = ring_lead

        # 正向顺移：谁的目的槽刚空出来，谁就移动；每次都重新扫描保证确定性
        empty_gap = lead_src
        while True:
            nxt = dst_to_pid.get(empty_gap)
            if nxt is None or nxt in moved:
                break
            ops.append(
                MovePage(pid=nxt, src=slots[nxt], dst=empty_gap)
            )
            moved.add(nxt)
            view[slots[nxt]] = -1
            view[empty_gap] = nxt
            empty_gap = slots[nxt]

        # 此时 lead_dst 已由链上最后一页腾出，接回环首
        ops.append(MovePage(pid=ring_lead, src=temp_slot, dst=lead_dst))
        view[temp_slot] = -1
        view[lead_dst] = ring_lead
        done_leads.append((ring_lead, lead_dst))
        remaining = sorted(pid for pid in moves if pid not in moved)

    # 临时槽必须已全部归还为空
    assert view[temp_slot] == -1, "临时槽在调度结束时仍被占用"

    # ---------------- 4. 新页 COPY / FRESH + APPEND ---------------- #
    # 此时所有目的槽都应已腾空（COPY/APPEND 执行器会再次物理断言）。
    # 按孩子给定顺序（调用方已按束排名排好）生成，保证可复现。
    for child in children:
        dst = targets[
            child.new_pid if child.new_pid is not None else child.source_pid
        ]
        if child.new_pid is not None:
            if child.source_pid is not None:
                ops.append(
                    CopyPage(
                        pid=child.new_pid,
                        src_pid=child.source_pid,
                        dst=dst,
                    )
                )
            ops.append(
                AppendPage(
                    pid=child.new_pid,
                    dst=dst,
                    token=child.token,
                    fresh=child.source_pid is None,
                )
            )
        else:
            # 独占继承：旧页已在第 3 阶段就位，这里仅向其追加 token
            assert child.source_pid is not None
            ops.append(
                AppendPage(
                    pid=child.source_pid,
                    dst=dst,
                    token=child.token,
                    fresh=False,
                )
            )

    return ops
