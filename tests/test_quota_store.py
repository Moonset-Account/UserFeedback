"""quota_store 的 unittest 测试。

从工作区根目录运行：python3 -m unittest discover -s tests -t .
或直接：python3 -m unittest
"""

import unittest

from quota_store import (
    ObjectStore,
    OperationReport,
    QuotaExceededError,
    Violation,
)


class DedupTest(unittest.TestCase):
    """正常场景：租户内与全池的块级去重计费。"""

    def setUp(self):
        self.store = ObjectStore(pool_capacity=1_000_000)
        for bid, size in {"A": 100, "B": 200, "C": 300, "D": 400}.items():
            self.store.register_block(bid, size)
        self.store.set_quota("t1", 10_000)
        self.store.set_quota("t2", 10_000)

    def test_same_block_charged_once_per_tenant_across_objects(self):
        self.store.put("t1", "o1", ["A", "B"])
        self.store.put("t1", "o2", ["A", "C"])
        # A 在 t1 的两个对象里只收费一次
        self.assertEqual(self.store.usage("t1"), 100 + 200 + 300)

    def test_duplicate_block_within_one_listing_charged_once(self):
        self.store.put("t1", "o1", ["A", "A", "B"])
        self.assertEqual(self.store.usage("t1"), 100 + 200)
        # 但读取必须按原清单返回（保留重复与顺序）
        self.assertEqual(self.store.read("t1", "o1"), ["A", "A", "B"])

    def test_pool_counts_shared_block_once_across_tenants(self):
        self.store.put("t1", "o1", ["A", "B"])
        self.store.put("t2", "o9", ["A", "D"])
        # 两个租户各自按自己的去重集合计费
        self.assertEqual(self.store.usage("t1"), 300)
        self.assertEqual(self.store.usage("t2"), 500)
        # 全池里 A 只占一份物理空间
        self.assertEqual(self.store.pool_used, 100 + 200 + 400)

    def test_block_freed_only_when_last_reference_removed(self):
        self.store.put("t1", "o1", ["A"])
        self.store.put("t1", "o2", ["A"])
        self.store.put("t2", "o9", ["A"])
        self.assertEqual(self.store.pool_used, 100)
        self.store.delete("t1", "o1")
        self.assertEqual(self.store.live_blocks(), {"A"})
        self.store.delete("t1", "o2")
        self.assertEqual(self.store.live_blocks(), {"A"})
        self.assertEqual(self.store.pool_used, 100)
        self.store.delete("t2", "o9")
        # 最后一个引用被移走才释放物理块
        self.assertEqual(self.store.live_blocks(), set())
        self.assertEqual(self.store.pool_used, 0)
        self.assertEqual(self.store.usage("t1"), 0)
        self.assertEqual(self.store.usage("t2"), 0)

    def test_replace_object_updates_usage(self):
        self.store.put("t1", "o1", ["A", "B"])
        self.store.put("t1", "o1", ["B", "C"])
        self.assertEqual(self.store.usage("t1"), 200 + 300)
        self.assertEqual(self.store.read("t1", "o1"), ["B", "C"])
        self.assertEqual(self.store.live_blocks(), {"B", "C"})


class BatchAndCopyTest(unittest.TestCase):
    """一笔操作内新增/替换/删除多个对象，以及跨租户复制清单。"""

    def setUp(self):
        self.store = ObjectStore(pool_capacity=1_000_000)
        for bid, size in {"A": 100, "B": 200, "C": 300, "D": 400, "E": 500}.items():
            self.store.register_block(bid, size)
        self.store.set_quota("t1", 10_000)
        self.store.set_quota("t2", 10_000)
        self.store.set_quota("t3", 10_000)

    def test_single_transaction_add_replace_delete(self):
        self.store.put("t1", "keep", ["A"])
        self.store.put("t1", "replace", ["B"])
        self.store.put("t1", "drop", ["C"])
        report = self.store.apply(
            {
                "t1": {
                    "new": ["D"],  # 新增
                    "replace": ["E"],  # 替换
                    "drop": None,  # 删除
                }
            }
        )
        self.assertEqual(self.store.list_objects("t1"), {"keep": ["A"], "replace": ["E"], "new": ["D"]})
        self.assertEqual(self.store.usage("t1"), 100 + 500 + 400)
        self.assertEqual(self.store.live_blocks(), {"A", "D", "E"})
        self.assertIsInstance(report, OperationReport)
        self.assertEqual(set(report.recounted_blocks), {"B", "C", "D", "E"})

    def test_copy_listing_snapshots_at_operation_start(self):
        self.store.put("t1", "x", ["A"])
        self.store.put("t1", "y", ["B", "C"])
        # 同一笔操作：t2 复制 t1 的清单，同时 t1 自身被改动。
        # 复制内容必须是操作开始时的快照。
        self.store.apply(
            {"t1": {"x": ["D"], "z": ["E"]}},
            copy_from=("t1", "t2"),
        )
        self.assertEqual(self.store.list_objects("t2"), {"x": ["A"], "y": ["B", "C"]})
        self.assertEqual(self.store.list_objects("t1"), {"x": ["D"], "y": ["B", "C"], "z": ["E"]})
        # 共享块全池仍只占一份：A B C D E 各一份
        self.assertEqual(self.store.pool_used, 100 + 200 + 300 + 400 + 500)

    def test_copy_replaces_target_listing_and_releases_old_blocks(self):
        self.store.put("t1", "x", ["A"])
        self.store.put("t2", "old", ["D"])
        self.store.apply(copy_from=("t1", "t2"))
        self.assertEqual(self.store.list_objects("t2"), {"x": ["A"]})
        self.assertNotIn("D", self.store.live_blocks())
        self.assertEqual(self.store.usage("t2"), 100)

    def test_copy_then_change_overlays_on_copied_listing(self):
        self.store.put("t1", "x", ["A"])
        self.store.apply({"t2": {"x": ["B"]}}, copy_from=("t1", "t2"))
        # changes 中对目标租户的修改叠加在复制结果之上
        self.assertEqual(self.store.list_objects("t2"), {"x": ["B"]})

    def test_copy_rejects_same_source_and_target(self):
        with self.assertRaises(ValueError):
            self.store.apply(copy_from=("t1", "t1"))


class QuotaEnforcementTest(unittest.TestCase):
    """超限结算：返回超限方与差额，状态保持不变。"""

    def setUp(self):
        self.store = ObjectStore(pool_capacity=600)
        for bid, size in {"A": 100, "B": 200, "C": 300, "D": 400}.items():
            self.store.register_block(bid, size)
        self.store.set_quota("t1", 300)
        self.store.set_quota("t2", 1000)

    def test_tenant_quota_exceeded_reports_offender_and_deficit(self):
        self.store.put("t1", "o1", ["A", "B"])  # 300，刚好用满
        with self.assertRaises(QuotaExceededError) as ctx:
            self.store.put("t1", "o2", ["C"])  # 结算后 600，超 300
        violations = ctx.exception.violations
        self.assertEqual(len(violations), 1)
        v = violations[0]
        self.assertIsInstance(v, Violation)
        self.assertEqual((v.scope, v.name), ("tenant", "t1"))
        self.assertEqual((v.used, v.limit, v.deficit), (600, 300, 300))
        # 状态不变：原对象仍可按原清单读取，新对象不存在
        self.assertEqual(self.store.read("t1", "o1"), ["A", "B"])
        with self.assertRaises(KeyError):
            self.store.read("t1", "o2")
        self.assertEqual(self.store.usage("t1"), 300)
        self.assertEqual(self.store.live_blocks(), {"A", "B"})

    def test_pool_capacity_exceeded_reports_pool(self):
        self.store.put("t2", "o1", ["A", "B"])  # 池占用 300
        with self.assertRaises(QuotaExceededError) as ctx:
            self.store.put("t2", "o2", ["D"])  # 池将达 700 > 600
        v = ctx.exception.violations[0]
        self.assertEqual((v.scope, v.name), ("pool", "pool"))
        self.assertEqual((v.used, v.limit, v.deficit), (700, 600, 100))
        self.assertEqual(self.store.pool_used, 300)

    def test_shared_block_does_not_push_pool_over(self):
        self.store.put("t2", "o1", ["A", "B", "C"])  # 池 600，刚好用满
        # t1 引用同样的块：全池不增加物理占用，不应触发池超限
        self.store.set_quota("t1", 1000)
        self.store.put("t1", "o1", ["A", "B", "C"])
        self.assertEqual(self.store.pool_used, 600)

    def test_failed_batch_is_atomic_across_tenants(self):
        self.store.put("t1", "o1", ["A"])
        self.store.put("t2", "o2", ["B"])
        with self.assertRaises(QuotaExceededError):
            self.store.apply({"t1": {"o1": ["C"]}, "t2": {"o2": ["A", "B", "C", "D"]}})
        # t2 的改动使池超限（100+200+300+400=1000>600），整笔回滚，t1 也不变
        self.assertEqual(self.store.read("t1", "o1"), ["A"])
        self.assertEqual(self.store.read("t2", "o2"), ["B"])
        self.assertEqual(self.store.live_blocks(), {"A", "B"})

    def test_multiple_violations_reported_together(self):
        store = ObjectStore(pool_capacity=10)
        store.register_block("A", 100)
        store.set_quota("t1", 50)
        store.set_quota("t2", 50)
        with self.assertRaises(QuotaExceededError) as ctx:
            store.apply({"t1": {"o": ["A"]}, "t2": {"o": ["A"]}})
        names = {(v.scope, v.name) for v in ctx.exception.violations}
        self.assertEqual(names, {("tenant", "t1"), ("tenant", "t2"), ("pool", "pool")})

    def test_swap_between_full_tenants_succeeds(self):
        # 题面示例：两个租户刚好用满，各持一个不同的等大块，
        # 同一笔操作互换对象内容应成功。
        store = ObjectStore(pool_capacity=200)
        store.register_block("A", 100)
        store.register_block("B", 100)
        store.set_quota("t1", 100)
        store.set_quota("t2", 100)
        store.put("t1", "obj", ["A"])
        store.put("t2", "obj", ["B"])
        report = store.apply({"t1": {"obj": ["B"]}, "t2": {"obj": ["A"]}})
        self.assertEqual(store.read("t1", "obj"), ["B"])
        self.assertEqual(store.read("t2", "obj"), ["A"])
        self.assertEqual(store.usage("t1"), 100)
        self.assertEqual(store.usage("t2"), 100)
        self.assertEqual(store.pool_used, 200)
        self.assertEqual(set(report.recounted_blocks), {"A", "B"})


class RecountReportTest(unittest.TestCase):
    """每次操作实际重新计数的块编号。"""

    def setUp(self):
        self.store = ObjectStore(pool_capacity=1_000_000)
        for bid in "ABCDEF":
            self.store.register_block(bid, 10)
        self.store.set_quota("t1", None)
        self.store.set_quota("t2", None)
        self.store.put("t1", "o1", ["A", "B"])
        self.store.put("t1", "o2", ["C"])
        self.store.put("t2", "p1", ["D"])

    def test_single_object_change_recounts_only_old_and_new_listing(self):
        report = self.store.put("t1", "o1", ["B", "E"])
        # 只涉及该对象旧清单 {A,B} 与新清单 {B,E} 里的块
        self.assertEqual(set(report.recounted_blocks), {"A", "B", "E"})
        self.assertNotIn("C", report.recounted_blocks)
        self.assertNotIn("D", report.recounted_blocks)

    def test_delete_recounts_only_deleted_listing(self):
        report = self.store.delete("t1", "o2")
        self.assertEqual(set(report.recounted_blocks), {"C"})

    def test_untouched_objects_not_recounted_in_batch(self):
        report = self.store.apply({"t1": {"o1": ["A"]}, "t2": {"p2": ["F"]}})
        self.assertEqual(set(report.recounted_blocks), {"A", "B", "F"})

    def test_report_contains_final_usage(self):
        report = self.store.put("t1", "o3", ["E", "F"])
        # t1 去重后引用 A,B,C,E,F；全池另有 t2 的 D
        self.assertEqual(report.tenant_usage, {"t1": 50})
        self.assertEqual(report.pool_used, 60)
        self.assertEqual(report.pool_capacity, 1_000_000)


class EdgeAndInterleavedTest(unittest.TestCase):
    """边界与交错场景。"""

    def test_zero_byte_block_and_empty_listing(self):
        store = ObjectStore(pool_capacity=0)
        store.register_block("Z", 0)
        store.set_quota("t", 0)
        store.put("t", "o", ["Z"])
        self.assertEqual(store.usage("t"), 0)
        self.assertEqual(store.pool_used, 0)
        self.assertEqual(store.live_blocks(), {"Z"})  # 零字节块仍占一个物理块位
        store.put("t", "empty", [])
        self.assertEqual(store.read("t", "empty"), [])
        store.delete("t", "o")
        self.assertEqual(store.live_blocks(), set())

    def test_unknown_block_rejected_and_state_unchanged(self):
        store = ObjectStore(pool_capacity=100)
        store.register_block("A", 10)
        store.set_quota("t", 100)
        store.put("t", "o", ["A"])
        with self.assertRaises(KeyError):
            store.put("t", "o", ["A", "ghost"])
        self.assertEqual(store.read("t", "o"), ["A"])

    def test_unknown_tenant_rejected(self):
        store = ObjectStore(pool_capacity=100)
        store.register_block("A", 10)
        with self.assertRaises(KeyError):
            store.put("nobody", "o", ["A"])

    def test_delete_missing_object_is_noop(self):
        store = ObjectStore(pool_capacity=100)
        store.register_block("A", 10)
        store.set_quota("t", 100)
        report = store.delete("t", "missing")
        self.assertEqual(report.recounted_blocks, ())
        self.assertEqual(store.usage("t"), 0)

    def test_negative_size_and_capacity_rejected(self):
        with self.assertRaises(ValueError):
            ObjectStore(pool_capacity=-1)
        store = ObjectStore(pool_capacity=0)
        with self.assertRaises(ValueError):
            store.register_block("A", -1)
        with self.assertRaises(ValueError):
            store.set_quota("t", -5)

    def test_interleaved_operations_across_tenants(self):
        # 交错场景：多租户交替增删改、失败操作穿插其中，最终状态须一致。
        store = ObjectStore(pool_capacity=250)
        for bid, size in {"A": 100, "B": 100, "C": 50}.items():
            store.register_block(bid, size)
        store.set_quota("t1", 150)
        store.set_quota("t2", 150)

        store.put("t1", "o1", ["A"])          # t1=100, pool=100
        store.put("t2", "o1", ["A", "C"])     # t2=150, pool=150（A 共享）
        store.put("t1", "o2", ["C"])          # t1=150, pool=150
        with self.assertRaises(QuotaExceededError):
            store.put("t1", "o3", ["B"])      # t1 将达 250 > 150，拒绝
        store.apply({"t1": {"o1": None}, "t2": {"o1": ["B"]}})
        # t1 释放对 A 的引用后，A 仍被 t2 引用？——t2 已改为 [B]，A 无人引用，释放。
        self.assertEqual(store.list_objects("t1"), {"o2": ["C"]})
        self.assertEqual(store.list_objects("t2"), {"o1": ["B"]})
        self.assertEqual(store.usage("t1"), 50)
        self.assertEqual(store.usage("t2"), 100)
        self.assertEqual(store.live_blocks(), {"B", "C"})
        self.assertEqual(store.pool_used, 150)
        # 失败操作之后继续正常工作
        store.put("t1", "o3", ["A"])
        self.assertEqual(store.live_blocks(), {"A", "B", "C"})
        self.assertEqual(store.pool_used, 250)

    def test_unlimited_quota_tenant(self):
        store = ObjectStore(pool_capacity=2_000_000_000)
        store.register_block("A", 10**9)
        store.set_quota("big", None)
        store.put("big", "o", ["A"])
        self.assertEqual(store.usage("big"), 10**9)


if __name__ == "__main__":
    unittest.main()
