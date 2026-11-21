"""VersionedStore 验收测试。

覆盖题面交叉约束：
1. 可接受结果：分区两节点并发写同一对象，合并后多版本并列读出。
2. 应被阻止的结果：过时上下文写入不得替代未读过的并发版本；
   被覆盖的旧值不得从迟到的旧副本复活。
3. 边界/交错：上下文只覆盖部分版本时只替代读过的版本；
   一边删除已读版本、另一边并发改出新值，合并后新值可读且能识别并发删除；
   客户端读全冲突再写应收敛为单值；三节点按不同顺序反复交换状态不复活。
"""
import unittest

from versioned_store import VersionedStore, VectorClock


def make_pair():
    return VersionedStore("A"), VersionedStore("B")


class TestMultiVersionReadWrite(unittest.TestCase):
    def test_concurrent_writes_both_returned_after_merge(self):
        """可接受：断开后各自写同一对象，合并后两个值并列返回。"""
        a, b = make_pair()
        a.put("k", "from-A")
        b.put("k", "from-B")

        a.merge_from(b)
        b.merge_from(a)

        for node in (a, b):
            values, _ = node.get("k")
            self.assertEqual(sorted(values), ["from-A", "from-B"])

    def test_sequential_write_replaces_read_version(self):
        """基于读取上下文的写入替代旧版本，读出单值。"""
        a, _ = make_pair()
        _, ctx1 = a.get("k")
        a.put("k", "v1", ctx1)
        values, ctx2 = a.get("k")
        self.assertEqual(values, ["v1"])
        a.put("k", "v2", ctx2)
        values, _ = a.get("k")
        self.assertEqual(values, ["v2"])

    def test_stale_context_does_not_replace_unread_concurrent_version(self):
        """应被阻止：拿着只读过 v1 的上下文写入，不得覆盖并发的 v2。"""
        a, b = make_pair()
        _, ctx0 = a.get("k")
        a.put("k", "v1", ctx0)
        b.merge_from(a)
        _, ctx_b = b.get("k")  # B 读到 v1

        # B 基于 v1 写 v2；A 仍拿着旧上下文（只读过 v1 之前的空）写 v3。
        b.put("k", "v2", ctx_b)
        a.put("k", "v3", ctx0)

        a.merge_from(b)
        values, _ = a.get("k")
        # v1 被两者都读过并替代；v2 与 v3 并发，必须并列保留。
        self.assertEqual(sorted(values), ["v2", "v3"])

    def test_partial_context_replaces_only_versions_actually_read(self):
        """边界：上下文只覆盖部分兄弟时，只替代读过的那个。"""
        a, b = make_pair()
        a.put("k", "vA")
        b.put("k", "vB")
        a.merge_from(b)  # a 现在有 vA、vB 两个兄弟

        # 真实路径：D 在未见到 vB 前只读到 vA，随后带着该上下文写新值。
        d = VersionedStore("D")
        d.merge_from(a)          # d 见到 {vA, vB}
        _, ctx_full = d.get("k")
        # 裁剪上下文：只保留覆盖 vA 的部分（模拟客户端只读到 vA）。
        va_clock = [v.clock for v in d.versions("k") if v.value == "vA"][0]
        ctx_partial = va_clock.copy()
        d.put("k", "vNew", ctx_partial)

        values, _ = d.get("k")
        # vA 被读过并替代；vB 未被读过，必须与 vNew 并列。
        self.assertEqual(sorted(values), ["vB", "vNew"])
        self.assertTrue(ctx_full.dominates_or_equal(ctx_partial))


class TestDeleteSemantics(unittest.TestCase):
    def test_delete_removes_read_versions(self):
        a, _ = make_pair()
        a.put("k", "v1")
        _, ctx = a.get("k")
        a.delete("k", ctx)
        values, _ = a.get("k")
        self.assertEqual(values, [])

    def test_concurrent_delete_and_write_merge_keeps_new_value(self):
        """交错：A 删除已读版本，B 并发改出新值；合并后新值可读且能识别并发删除。"""
        a, b = make_pair()
        a.put("k", "v1")
        b.merge_from(a)
        _, ctx_a = a.get("k")
        _, ctx_b = b.get("k")

        a.delete("k", ctx_a)          # A 删除读过的 v1
        b.put("k", "v2", ctx_b)       # B 并发改出 v2

        a.merge_from(b)
        b.merge_from(a)

        for node in (a, b):
            values, _ = node.get("k")
            self.assertEqual(values, ["v2"])           # 新值仍可读
            self.assertTrue(node.has_concurrent_delete("k"))  # 识别并发删除

    def test_delete_wins_over_old_value_no_resurrection(self):
        """删除支配旧值：合并后旧值不得复活。"""
        a, b = make_pair()
        a.put("k", "v1")
        b.merge_from(a)
        _, ctx = a.get("k")
        a.delete("k", ctx)

        a.merge_from(b)  # b 还持有 v1
        values, _ = a.get("k")
        self.assertEqual(values, [])
        b.merge_from(a)  # 反向合并后 b 也收敛为删除
        values, _ = b.get("k")
        self.assertEqual(values, [])


class TestReadRepairConvergence(unittest.TestCase):
    def test_client_read_all_conflicts_then_write_converges(self):
        """客户端读到全部冲突后再写一次，应收敛为单值。"""
        a, b = make_pair()
        a.put("k", "from-A")
        b.put("k", "from-B")
        a.merge_from(b)

        values, ctx = a.get("k")
        self.assertEqual(sorted(values), ["from-A", "from-B"])

        # 客户端解决冲突，带着读全的上下文写回。
        a.put("k", "resolved", ctx)
        b.merge_from(a)

        for node in (a, b):
            values, _ = node.get("k")
            self.assertEqual(values, ["resolved"])
            self.assertEqual(len(node.versions("k")), 1)


class TestThreeNodeExchangeNoResurrection(unittest.TestCase):
    def test_repeated_exchanges_in_different_orders(self):
        """三节点按不同顺序反复交换状态，被覆盖的旧值不得复活。"""
        a = VersionedStore("A")
        b = VersionedStore("B")
        c = VersionedStore("C")

        # 初始写入并同步。
        a.put("k", "v1")
        a.merge_from(b); b.merge_from(a)
        c.merge_from(a); a.merge_from(c)

        # C 保存一份迟到旧副本（只含 v1）。
        stale_snapshot = c.snapshot()

        # A 读到 v1 后改写为 v2（v1 被覆盖）。
        _, ctx = a.get("k")
        a.put("k", "v2", ctx)

        # 多轮、不同顺序的交换。
        exchange_orders = [
            (("A", a), ("B", b)),
            (("B", b), ("C", c)),
            (("C", c), ("A", a)),
            (("A", a), ("C", c)),
            (("C", c), ("B", b)),
            (("B", b), ("A", a)),
        ]
        for _ in range(3):
            for (_, n1), (_, n2) in exchange_orders:
                n1.merge_from(n2)
                n2.merge_from(n1)
                # 每轮都混入迟到旧副本，尝试让 v1 复活。
                n1.merge_snapshot(stale_snapshot)
                n2.merge_snapshot(stale_snapshot)

        for node in (a, b, c):
            values, _ = node.get("k")
            self.assertEqual(values, ["v2"])
            self.assertEqual(len(node.versions("k")), 1)

    def test_stale_snapshot_does_not_revive_after_delete(self):
        """迟到旧副本不得复活已被删除的值。"""
        a = VersionedStore("A")
        b = VersionedStore("B")
        c = VersionedStore("C")

        a.put("k", "v1")
        b.merge_from(a)
        c.merge_from(a)
        stale = b.snapshot()  # b 的迟到旧副本（含 v1）

        _, ctx = a.get("k")
        a.delete("k", ctx)  # A 删除 v1

        # 不同顺序反复交换，并反复混入旧副本。
        for _ in range(3):
            c.merge_from(a)
            c.merge_snapshot(stale)
            a.merge_from(c)
            b.merge_from(a)
            b.merge_snapshot(stale)
            a.merge_from(b)

        for node in (a, b, c):
            values, _ = node.get("k")
            self.assertEqual(values, [])


class TestVectorClock(unittest.TestCase):
    def test_dominance_and_concurrency(self):
        c1 = VectorClock({"A": 1})
        c2 = VectorClock({"A": 1, "B": 1})
        c3 = VectorClock({"A": 2})

        self.assertTrue(c2.dominates(c1))
        self.assertTrue(c3.dominates(c1))
        self.assertTrue(c2.is_concurrent(c3))
        self.assertFalse(c1.is_concurrent(c1))
        self.assertTrue(c1.dominates_or_equal(c1))


if __name__ == "__main__":
    unittest.main()
