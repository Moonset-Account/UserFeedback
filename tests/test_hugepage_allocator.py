"""大页申请模块的验收测试（unittest）。

覆盖题面交叉约束：

* 可接受结果：空闲整窗直接分配、需要搬迁时正确迁移并更新全部别名、
  order-0 边界、整节点边界、二次申请交错；
* 应被阻止的结果：所有候选含固定页、区域外空闲框不足、页数非二次幂、
  未知节点；
* 边界/交错：最少搬迁优先 + 起点取小的两级选择、失败后原映射可重读、
  已分配区域保留框对后续申请的影响、按逻辑页而非别名计数。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hugepage_allocator import (  # noqa: E402
    AllocationError,
    HugePageAllocator,
)


def build_allocator(node_id, frame_count):
    allocator = HugePageAllocator()
    allocator.add_node(node_id, frame_count)
    return allocator


class TestHappyPath(unittest.TestCase):
    """可接受结果。"""

    def test_free_aligned_window_returns_region_without_migration(self):
        allocator = build_allocator("n0", 16)
        # 占用 0~3，窗口 [4,8) 与 [8,12)、[12,16) 全空。
        allocator.register_page("n0", "p0", 0)
        allocator.register_page("n0", "p1", 1)
        allocator.register_page("n0", "p2", 2)
        allocator.register_page("n0", "p3", 3)

        region = allocator.allocate("n0", 4)

        self.assertEqual(region.node_id, "n0")
        self.assertEqual(region.start_frame, 4)
        self.assertEqual(region.page_count, 4)
        self.assertEqual(region.end_frame, 8)
        self.assertEqual(list(region.frames()), [4, 5, 6, 7])
        # 对齐约束：起点必须是页数的整数倍。
        self.assertEqual(region.start_frame % region.page_count, 0)
        # 原有逻辑页未被搬迁。
        self.assertEqual(allocator._pages["p0"].frame, 0)
        # 区域页框被保留，空闲集合中不再出现。
        node = allocator.nodes["n0"]
        self.assertTrue({4, 5, 6, 7} <= node.reserved_frames)
        self.assertEqual(node.free_frames(), {8, 9, 10, 11, 12, 13, 14, 15})

    def test_migration_moves_page_and_all_aliases_outside_region(self):
        allocator = build_allocator("n0", 24)
        # 窗口 [0,4) 内有一个可移动页，带两个虚拟地址别名；
        # 窗口 [4,8)~[16,20) 以及尾部对齐窗口 [20,24) 均被固定页封死，
        # 唯一可行候选是 [0,4)；21..23 同时充当区域外空闲搬迁目标。
        allocator.register_page(
            "n0", "m1", 0, aliases=["va-a", "va-b"]
        )
        for pfn in range(4, 21):
            allocator.register_page("n0", f"pin{pfn}", pfn, pinned=True)

        region = allocator.allocate("n0", 4)

        self.assertEqual(region.start_frame, 0)
        page = allocator._pages["m1"]
        # 逻辑页搬到区域之外（区域外最小空闲框为 21）。
        self.assertNotIn(page.frame, range(0, 4))
        self.assertEqual(page.frame, 21)
        # 源框已腾空并纳入保留区域。
        self.assertIsNone(allocator.nodes["n0"].frames[0])
        # 全部别名随逻辑页指向新框。
        self.assertEqual(allocator.translate("n0", "va-a"), 21)
        self.assertEqual(allocator.translate("n0", "va-b"), 21)
        # 固定页原地不动。
        self.assertEqual(allocator._pages["pin4"].frame, 4)
    def test_order_zero_single_page_region(self):
        allocator = build_allocator("n0", 4)
        allocator.register_page("n0", "p0", 0, pinned=True)

        region = allocator.allocate("n0", 1)

        self.assertEqual(region.page_count, 1)
        self.assertEqual(region.start_frame, 1)  # 0 被固定，取下一个框
        self.assertEqual(region.start_frame % 1, 0)

    def test_full_node_region(self):
        allocator = build_allocator("n0", 8)
        region = allocator.allocate("n0", 8)
        self.assertEqual((region.start_frame, region.end_frame), (0, 8))
        self.assertEqual(allocator.nodes["n0"].free_frames(), set())

    def test_add_alias_after_registration(self):
        allocator = build_allocator("n0", 8)
        allocator.register_page("n0", "p0", 0)
        allocator.add_alias("p0", "va-x")
        self.assertEqual(allocator.translate("n0", "va-x"), 0)


class TestSelectionPolicy(unittest.TestCase):
    """最少搬迁优先、再按物理起点取小。"""

    def test_prefer_window_with_fewest_movable_pages(self):
        allocator = build_allocator("n0", 16)
        # [0,4) 含 2 个可移动页，[4,8) 含 1 个，其余窗口全空。
        allocator.register_page("n0", "a", 0)
        allocator.register_page("n0", "b", 1)
        allocator.register_page("n0", "c", 4)

        region = allocator.allocate("n0", 4)

        # 全空窗口 [8,12) 搬迁数为 0，应当最先被选中。
        self.assertEqual(region.start_frame, 8)

    def test_tie_on_migration_count_prefers_smallest_start(self):
        allocator = build_allocator("n0", 16)
        # 所有 4 页窗口都恰好含 1 个可移动页，搬迁数相同。
        for pfn in range(0, 16, 4):
            allocator.register_page("n0", f"p{pfn}", pfn)

        region = allocator.allocate("n0", 4)
        self.assertEqual(region.start_frame, 0)
        # 起点 0 的逻辑页被搬到区域外最小空闲框（区域 [0,4) 外为 5）。
        self.assertEqual(allocator._pages["p0"].frame, 5)

    def test_migration_count_is_logical_pages_not_aliases(self):
        allocator = build_allocator("n0", 8)
        # 一个逻辑页带 3 个别名，只应消耗 1 个搬迁目标框。
        allocator.register_page("n0", "p0", 0, aliases=["v1", "v2", "v3"])
        allocator.register_page("n0", "p1", 4)  # 第二个窗口也有 1 页
        # 两窗口搬迁数同为 1，取起点小的 [0,4)；区域外最小空闲框为 5。
        region = allocator.allocate("n0", 4)
        self.assertEqual(region.start_frame, 0)
        self.assertEqual(allocator._pages["p0"].frame, 5)
        for alias in ("v1", "v2", "v3"):
            self.assertEqual(allocator.translate("n0", alias), 5)


class TestRejectedResults(unittest.TestCase):
    """应被阻止或拒绝的结果。"""

    def test_all_candidates_pinned_fails(self):
        allocator = build_allocator("n0", 16)
        # 两个 8 页对齐窗口 [0,8)、[8,16) 各含固定页。
        allocator.register_page("n0", "pin0", 0, pinned=True)
        allocator.register_page("n0", "pin8", 8, pinned=True)

        with self.assertRaises(AllocationError):
            allocator.allocate("n0", 8)

        # 失败后状态不变，原映射仍可重读。
        self.assertEqual(allocator._pages["pin0"].frame, 0)
        self.assertEqual(allocator._pages["pin8"].frame, 8)
        self.assertEqual(allocator.nodes["n0"].reserved_frames, set())

    def test_insufficient_external_free_frames_fails(self):
        allocator = build_allocator("n0", 16)
        # 两个窗口各含 5 个可移动页，但节点只有 6 个空闲框，
        # 任一窗口都需要 5 个区域外目标框，可行 → 本用例需制造不可行：
        # 改为每个窗口含 7 个可移动页，空闲框仅 2 个。
        for pfn in list(range(0, 7)) + list(range(8, 15)):
            allocator.register_page("n0", f"p{pfn}", pfn)

        with self.assertRaises(AllocationError):
            allocator.allocate("n0", 8)

        # 总空闲页数（2）远小于申请量（8），且任何映射都未被改动。
        self.assertEqual(len(allocator.nodes["n0"].free_frames()), 2)
        for pfn in list(range(0, 7)) + list(range(8, 15)):
            self.assertEqual(allocator._pages[f"p{pfn}"].frame, pfn)

    def test_fragmentation_total_free_enough_but_no_region_possible(self):
        """即使总空闲页足够，固定页切碎空间也要失败。"""
        allocator = build_allocator("n0", 16)
        # 8 页申请：两个对齐窗口都被固定页挡住，但总空闲有 14 框。
        allocator.register_page("n0", "pin3", 3, pinned=True)
        allocator.register_page("n0", "pin11", 11, pinned=True)
        node = allocator.nodes["n0"]
        self.assertEqual(len(node.free_frames()), 14)

        with self.assertRaises(AllocationError):
            allocator.allocate("n0", 8)

        # 固定页与页表保持原样。
        self.assertEqual(allocator._pages["pin3"].frame, 3)
        self.assertEqual(allocator._pages["pin11"].frame, 11)
        self.assertEqual(node.reserved_frames, set())

    def test_request_larger_than_node_fails(self):
        allocator = build_allocator("n0", 4)
        with self.assertRaises(AllocationError):
            allocator.allocate("n0", 8)

    def test_non_power_of_two_rejected(self):
        allocator = build_allocator("n0", 16)
        for bad in (0, 3, 6, 7, 12, -4):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    allocator.allocate("n0", bad)

    def test_unknown_node_rejected(self):
        allocator = HugePageAllocator()
        with self.assertRaises(KeyError):
            allocator.allocate("nope", 4)


class TestBoundaryAndInterleaving(unittest.TestCase):
    """边界与交错场景。"""

    def test_failed_allocation_keeps_original_mappings_readable(self):
        allocator = build_allocator("n0", 16)
        allocator.register_page(
            "n0", "m0", 0, aliases=["va0"]
        )
        allocator.register_page(
            "n0", "m8", 8, aliases=["va8"]
        )
        allocator.register_page("n0", "pin1", 1, pinned=True)
        allocator.register_page("n0", "pin9", 9, pinned=True)

        with self.assertRaises(AllocationError):
            allocator.allocate("n0", 8)

        # 原映射可重读：别名解析、逻辑页驻留框、页表三者一致。
        self.assertEqual(allocator.translate("n0", "va0"), 0)
        self.assertEqual(allocator.translate("n0", "va8"), 8)
        self.assertEqual(allocator._pages["m0"].frame, 0)
        self.assertEqual(allocator._pages["m8"].frame, 8)
        self.assertEqual(allocator.nodes["n0"].page_table["va0"], 0)
        self.assertEqual(allocator.nodes["n0"].page_table["va8"], 8)

    def test_second_allocation_avoids_reserved_region(self):
        allocator = build_allocator("n0", 16)
        first = allocator.allocate("n0", 4)
        self.assertEqual(first.start_frame, 0)

        # 第二次申请：[0,4) 已保留，须选其它窗口；在窗口 [4,8) 放一个
        # 可移动页，验证它会被搬走而不是复用保留框。
        allocator.register_page("n0", "p4", 4)
        second = allocator.allocate("n0", 4)

        self.assertEqual(second.start_frame, 8)  # [8,12) 全空，搬迁数 0
        self.assertEqual(allocator._pages["p4"].frame, 4)  # 未被触碰
        # 两个区域的保留框互不重叠。
        self.assertEqual(
            allocator.nodes["n0"].reserved_frames,
            {0, 1, 2, 3, 8, 9, 10, 11},
        )

    def test_reserved_frame_blocks_otherwise_free_candidate(self):
        allocator = build_allocator("n0", 8)
        allocator.allocate("n0", 4)  # 保留 [0,4)
        # [4,8) 放固定页，使剩余唯一窗口也不可用。
        allocator.register_page("n0", "pin5", 5, pinned=True)
        with self.assertRaises(AllocationError):
            allocator.allocate("n0", 4)

    def test_migration_destination_never_inside_target_region(self):
        allocator = build_allocator("n0", 24)
        # [0,4) 三页可移动；其余整窗（含尾部 [20,24)）全部用固定页封死，
        # 唯一候选为 [0,4)，21..23 是仅有的区域外空闲框，搬迁目的地
        # 只能落在那里，且绝不可能落入目标区域内部。
        for pfn in range(0, 3):
            allocator.register_page("n0", f"m{pfn}", pfn)
        for pfn in range(4, 21):
            allocator.register_page("n0", f"pin{pfn}", pfn, pinned=True)

        region = allocator.allocate("n0", 4)
        self.assertEqual(region.start_frame, 0)
        for pfn in range(0, 3):
            self.assertGreaterEqual(allocator._pages[f"m{pfn}"].frame, 21)

    def test_tail_aligned_window_when_node_size_not_multiple(self):
        """回归：24 框节点申请 4 页时，起点 20 的尾部对齐窗口必须被枚举。"""
        allocator = build_allocator("n0", 24)
        # 占用 0..19，使唯一可行候选是尾部窗口 [20,24)。
        for pfn in range(0, 20):
            allocator.register_page("n0", f"p{pfn}", pfn)

        region = allocator.allocate("n0", 4)
        self.assertEqual(region.start_frame, 20)
        self.assertEqual(region.end_frame, 24)
        self.assertEqual(region.start_frame % region.page_count, 0)

    def test_rollback_on_execution_failure_restores_everything(self):
        """搬迁执行阶段抛异常时，快照回滚保证页框/页表/别名全部还原。"""
        allocator = build_allocator("n0", 8)
        # 两个 4 页对齐窗口各放一个可移动页，唯一候选需要真实搬迁。
        allocator.register_page("n0", "p0", 0, aliases=["va"])
        allocator.register_page("n0", "p4", 4)
        node = allocator.nodes["n0"]
        original_frames = dict(node.frames)
        original_table = dict(node.page_table)

        # 让 migrate_page 在执行期必然失败，触发 allocate 的回滚分支。
        def boom(page, dst):  # noqa: ANN001
            raise RuntimeError("simulated migration fault")

        node.migrate_page = boom
        with self.assertRaises(RuntimeError):
            allocator.allocate("n0", 4)

        self.assertEqual(node.frames, original_frames)
        self.assertEqual(node.page_table, original_table)
        self.assertEqual(node.reserved_frames, set())
        self.assertEqual(allocator._pages["p0"].frame, 0)
        self.assertEqual(allocator.translate("n0", "va"), 0)

    def test_nodes_are_isolated(self):
        allocator = HugePageAllocator()
        allocator.add_node("n0", 8)
        allocator.add_node("n1", 8)
        # 两个节点使用相同 PFN 和相同虚拟地址，映射必须各自独立。
        allocator.register_page("n0", "a", 0, aliases=["va"])
        allocator.register_page("n1", "b", 0, pinned=True, aliases=["va"])

        region0 = allocator.allocate("n0", 4)
        self.assertEqual(region0.start_frame, 4)
        # n1 的 0 号框被固定，首个可行单页区域从 1 开始。
        region1 = allocator.allocate("n1", 1)
        self.assertEqual(region1.start_frame, 1)
        self.assertEqual(allocator.translate("n0", "va"), 0)
        self.assertEqual(allocator.translate("n1", "va"), 0)
        # n0 上的保留区域不影响 n1 的空闲集合。
        self.assertEqual(allocator.nodes["n1"].reserved_frames, {1})
        self.assertEqual(allocator.nodes["n0"].reserved_frames, {4, 5, 6, 7})


if __name__ == "__main__":
    unittest.main(verbosity=2)
