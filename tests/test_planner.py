"""规划器 build_step_plan 的单元测试。

覆盖：重排环与重排链、DROP 优先、COPY/APPEND 源槽写前保护（目标槽恰是
另一个孩子未读源槽的交错情况）、容量不足在覆写前拒绝、临时槽被占用拒绝、
以及计划的逐步可执行性（由 PhysicalMemory 逐条物理断言）。
"""

import unittest

from beam_manager import (
    AppendPage,
    CapacityError,
    ChildSpec,
    CopyPage,
    DropPage,
    MovePage,
    PhysicalMemory,
    build_step_plan,
)


def execute_on(memory: PhysicalMemory, ops):
    """逐条执行，等价于逐步调用 apply。"""
    for op in ops:
        memory.apply(op)


class PlannerRingAndChainTest(unittest.TestCase):
    def test_three_cycle_uses_temp_slot_once(self):
        width, heap = 3, 2
        mem = PhysicalMemory(width=width, heap_slots=heap)
        temp = mem.temp_slot
        # 页 1,2,3 占据家槽 0,1,2；目标旋转 0->1, 1->2, 2->0
        for pid, slot in ((1, 0), (2, 1), (3, 2)):
            mem.create_empty(pid, slot)
            mem.apply(AppendPage(pid=pid, dst=slot, token=pid * 10))

        children = [
            ChildSpec(new_pid=None, source_pid=3, home=0, token=1),
            ChildSpec(new_pid=None, source_pid=1, home=1, token=1),
            ChildSpec(new_pid=None, source_pid=2, home=2, token=1),
        ]
        ops = build_step_plan(
            width=width,
            heap_slots=heap,
            slots=dict(mem.slot_of_pid),
            existing_pids=[1, 2, 3],
            dead_pids=[],
            children=children,
        )

        moves = [op for op in ops if isinstance(op, MovePage)]
        self.assertEqual(len([o for o in moves if o.dst == temp]), 1)
        self.assertEqual(len(moves), 4)  # 环首入临时槽 + 两次顺移 + 接回
        execute_on(mem, moves)
        self.assertEqual(mem.slot_for(3), 0)
        self.assertEqual(mem.slot_for(1), 1)
        self.assertEqual(mem.slot_for(2), 2)
        self.assertTrue(mem.temp_is_free())

    def test_true_chain_uses_free_end_without_temp(self):
        # 宽度2：家槽0,1；堆槽2,3；临时槽4。
        # 页1->槽0, 页2->槽1, 死页9->槽2。
        # 目标：页2 独占进入 home0（1->0），页1 作为历史页迁到空堆槽3。
        # DROP 9 先释放槽2，页1 先搬到自由端，页2 再进入槽0 —— 纯链、无环。
        width, heap = 2, 2
        mem = PhysicalMemory(width=width, heap_slots=heap)
        mem.create_empty(1, 0)
        mem.create_empty(2, 1)
        mem.create_empty(9, 2)
        children = [
            ChildSpec(new_pid=None, source_pid=2, home=0, token=7),
        ]
        ops = build_step_plan(
            width=width,
            heap_slots=heap,
            slots=dict(mem.slot_of_pid),
            existing_pids=[1, 2],
            dead_pids=[9],
            children=children,
        )
        self.assertFalse(
            any(isinstance(o, MovePage) and o.dst == mem.temp_slot
                for o in ops),
            "重排链存在自由端，不应使用临时槽",
        )
        self.assertIsInstance(ops[0], DropPage)
        move_pids = [o.pid for o in ops if isinstance(o, MovePage)]
        # 页1 先撤到自由端，页2 才能进入槽0
        self.assertEqual(move_pids, [1, 2])
        execute_on(mem, ops)
        self.assertEqual(mem.slot_for(2), 0)
        self.assertIn(mem.slot_for(1), (2, 3))
        self.assertEqual(mem.page_tokens(2), (7,))
        self.assertTrue(mem.temp_is_free())

    def test_drop_happens_before_any_write(self):
        width, heap = 3, 1
        mem = PhysicalMemory(width=width, heap_slots=heap)
        mem.create_empty(1, 0)
        mem.create_empty(2, 3)  # 堆槽
        # 页 2 死亡，腾出的堆槽给一个结束孩子
        children = [
            ChildSpec(new_pid=3, source_pid=1, home=0, token=5),
            ChildSpec(new_pid=4, source_pid=1, home=None, token=6),
        ]
        ops = build_step_plan(
            width=width,
            heap_slots=heap,
            slots=dict(mem.slot_of_pid),
            existing_pids=[1],
            dead_pids=[2],
            children=children,
        )
        self.assertIsInstance(ops[0], DropPage)
        self.assertEqual(ops[0].pid, 2)
        execute_on(mem, ops)
        self.assertNotIn(2, mem.pages)
        self.assertEqual(mem.slot_for(4), 3)

    def test_temp_slot_occupied_is_rejected(self):
        width, heap = 3, 2
        slots = {1: 0, 2: 1, 3: 2, 9: width + heap}  # 9 占着临时槽
        children = [
            ChildSpec(new_pid=None, source_pid=3, home=0, token=1),
            ChildSpec(new_pid=None, source_pid=1, home=1, token=1),
            ChildSpec(new_pid=None, source_pid=2, home=2, token=1),
        ]
        with self.assertRaises(CapacityError):
            build_step_plan(
                width=width,
                heap_slots=heap,
                slots=slots,
                existing_pids=[1, 2, 3, 9],
                dead_pids=[],
                children=children,
            )


class PlannerHazardOrderTest(unittest.TestCase):
    """目标槽恰是另一个孩子未读源槽时，顺序必须保证读后写。"""

    def test_copy_source_protected_until_all_reads_done(self):
        width, heap = 3, 3
        mem = PhysicalMemory(width=width, heap_slots=heap)
        # 父末页 pid1 在家槽0，历史页 pid2 在空闲家槽2
        mem.create_empty(1, 0)
        mem.apply(AppendPage(pid=1, dst=0, token=100))
        mem.create_empty(2, 2)

        # pid1 被两个孩子 COW：存活孩子新页目标家槽1，结束孩子新页目标堆槽。
        # 共享源 pid1 必须先撤离家槽0（搬到堆槽），两个 COPY 才能开始；
        # 撤离之后、最后一次 COPY 完成之前，任何操作都不得改写 pid1 的内容。
        children = [
            ChildSpec(new_pid=10, source_pid=1, home=0, token=1),
            ChildSpec(new_pid=11, source_pid=1, home=None, token=2),
            ChildSpec(new_pid=None, source_pid=2, home=1, token=3),
        ]
        ops = build_step_plan(
            width=width,
            heap_slots=heap,
            slots=dict(mem.slot_of_pid),
            existing_pids=[1, 2],
            dead_pids=[],
            children=children,
        )

        # 维护 pid -> 当前槽位 的视图，验证每次 COPY 读取瞬间，其源槽仍由
        # 源页自己占用，且 COPY 目的槽绝不与源页当前槽位重合。
        location = {1: 0, 2: 2}
        slot_owner = {0: 1, 2: 2}
        reads_of_pid1 = 0
        for op in ops:
            if isinstance(op, CopyPage) and op.src_pid == 1:
                self.assertEqual(slot_owner[location[1]], 1)
                self.assertNotEqual(
                    op.dst, location[1],
                    "COPY 目的槽恰是未读源槽，会在读取前覆写源页",
                )
                reads_of_pid1 += 1
                location[op.pid] = op.dst
                slot_owner[op.dst] = op.pid
            elif isinstance(op, MovePage):
                location[op.pid] = op.dst
                slot_owner[op.src] = None
                slot_owner[op.dst] = op.pid
            elif isinstance(op, AppendPage) and op.fresh:
                location[op.pid] = op.dst
                slot_owner[op.dst] = op.pid
        self.assertEqual(reads_of_pid1, 2)

        # 所有 MOVE 必须排在所有 COPY 之前（先腾源、再复制、最后追加）
        last_move = max(
            (i for i, o in enumerate(ops) if isinstance(o, MovePage)),
            default=-1,
        )
        first_copy = next(
            i for i, o in enumerate(ops) if isinstance(o, CopyPage)
        )
        self.assertLess(last_move, first_copy)

        execute_on(mem, ops)
        self.assertEqual(mem.page_tokens(10), (100, 1))
        self.assertEqual(mem.page_tokens(11), (100, 2))
        self.assertEqual(mem.page_tokens(1), (100,))   # 共享源内容冻结
        self.assertTrue(mem.temp_is_free())

    def test_no_overwrite_ever_targets_occupied_slot(self):
        """随机化多轮规划：执行器的物理断言保证无覆写未读源。"""
        import random

        rng = random.Random(20261126)
        width, heap = 4, 6
        for trial in range(40):
            mem = PhysicalMemory(width=width, heap_slots=heap)
            for pid, slot in enumerate(range(width), start=1):
                mem.create_empty(pid, slot)
            existing = list(range(1, width + 1))
            next_pid = width + 1
            for step in range(6):
                # 每个现存家槽页随机 1~2 个孩子，构造分叉与重排
                children = []
                home_cursor = 0
                for pid in existing:
                    for _ in range(rng.randint(1, 2)):
                        if home_cursor < width and rng.random() > 0.15:
                            # 家槽固定为 0..k-1；source_pid 与家槽的错位
                            # 由遍历顺序自然产生重排环/链
                            home = home_cursor
                            home_cursor += 1
                            new_pid = next_pid
                            next_pid += 1
                            children.append(
                                ChildSpec(new_pid=new_pid, source_pid=pid,
                                          home=home, token=step)
                            )
                        elif rng.random() > 0.5:
                            new_pid = next_pid
                            next_pid += 1
                            children.append(
                                ChildSpec(new_pid=new_pid, source_pid=pid,
                                          home=None, token=step)
                            )
                if not children:
                    break
                surviving = {
                    c.source_pid for c in children
                } | {c.new_pid for c in children if c.new_pid is not None}
                old = set(mem.slot_of_pid)
                try:
                    ops = build_step_plan(
                        width=width,
                        heap_slots=heap,
                        slots=dict(mem.slot_of_pid),
                        existing_pids=sorted(old & surviving),
                        dead_pids=sorted(old - surviving),
                        children=children,
                    )
                except CapacityError:
                    break  # 容量拒绝是合法结果，且发生在任何覆写之前
                snapshot_before = mem.snapshot()
                mem.apply_all(ops)  # 任何违例都会在这里抛 AssertionError
                self.assertTrue(mem.temp_is_free())
                existing = sorted(
                    c.new_pid if c.new_pid is not None else c.source_pid
                    for c in children if c.home is not None
                )
                # 仅保留存活束继续；结束页留堆槽
                del snapshot_before


class PlannerCapacityTest(unittest.TestCase):
    def test_insufficient_slots_rejected_before_any_mutation(self):
        width, heap = 2, 0  # 只有 2 个家槽 + 临时槽
        mem = PhysicalMemory(width=width, heap_slots=heap)
        mem.create_empty(1, 0)
        mem.create_empty(2, 1)
        before = mem.snapshot()

        # 两个父页都要保留，再加一个结束孩子的新页 -> 3 页 > 2 常规槽
        children = [
            ChildSpec(new_pid=None, source_pid=1, home=0, token=1),
            ChildSpec(new_pid=3, source_pid=2, home=1, token=2),
            ChildSpec(new_pid=4, source_pid=2, home=None, token=3),
        ]
        with self.assertRaises(CapacityError):
            build_step_plan(
                width=width,
                heap_slots=heap,
                slots=dict(mem.slot_of_pid),
                existing_pids=[1, 2],
                dead_pids=[],
                children=children,
            )
        # 拒绝时物理内存原封不动
        self.assertEqual(mem.snapshot(), before)

    def test_finished_beam_without_heap_slot_rejected(self):
        width, heap = 1, 0
        mem = PhysicalMemory(width=width, heap_slots=heap)
        mem.create_empty(1, 0)
        children = [
            ChildSpec(new_pid=2, source_pid=1, home=None, token=9),
        ]
        with self.assertRaises(CapacityError):
            build_step_plan(
                width=width,
                heap_slots=heap,
                slots=dict(mem.slot_of_pid),
                existing_pids=[1],
                dead_pids=[],
                children=children,
            )


if __name__ == "__main__":
    unittest.main()
