"""RelayAllocator 与冲突判定测试。"""

import unittest

from anon_relay import (
    ConflictingPinError,
    InsufficientRelaysError,
    InvalidReservationError,
    RelayAllocator,
    Reservation,
    conflicts,
)
from anon_relay.allocator import MAX_RESERVATIONS


def res(rid, a, b, start, end, cooldown=0, pin=None):
    return Reservation(
        reservation_id=rid,
        caller=a,
        callee=b,
        start=start,
        end=end,
        cooldown=cooldown,
        pinned_relay=pin,
    )


class ConflictRuleTests(unittest.TestCase):
    def test_disjoint_intervals_no_conflict_even_shared_party(self):
        # 占区间端点相接（半开），不算重叠。
        r1 = res("R1", "A", "B", 0, 60, cooldown=30)   # [0, 90)
        r2 = res("R2", "A", "C", 90, 120, cooldown=0)  # [90, 120)
        self.assertFalse(conflicts(r1, r2))
        alloc = RelayAllocator([r1, r2]).allocate()
        self.assertEqual(alloc.relay_count, 1)

    def test_overlap_without_shared_party_no_conflict(self):
        r1 = res("R1", "A", "B", 0, 60)
        r2 = res("R2", "C", "D", 10, 50)
        self.assertFalse(conflicts(r1, r2))
        alloc = RelayAllocator([r1, r2]).allocate()
        self.assertEqual(alloc.relay_of("R1"), alloc.relay_of("R2"))

    def test_overlap_with_shared_party_conflicts(self):
        r1 = res("R1", "A", "B", 0, 60, cooldown=30)   # [0, 90)
        r2 = res("R2", "A", "C", 60, 120, cooldown=30)  # [60, 150)
        self.assertTrue(conflicts(r1, r2))
        alloc = RelayAllocator([r1, r2]).allocate()
        self.assertEqual(alloc.relay_count, 2)
        self.assertNotEqual(alloc.relay_of("R1"), alloc.relay_of("R2"))

    def test_cooldown_creates_conflict_beyond_validity(self):
        # 有效期不重叠，但 R1 冷却期与 R2 有效期重叠且共享 A。
        r1 = res("R1", "A", "B", 0, 60, cooldown=30)   # [0, 90)
        r2 = res("R2", "A", "C", 70, 100)              # [70, 100)
        self.assertTrue(conflicts(r1, r2))
        self.assertEqual(RelayAllocator([r1, r2]).allocate().relay_count, 2)

    def test_shared_callee_also_conflicts(self):
        r1 = res("R1", "A", "X", 0, 60)
        r2 = res("R2", "B", "X", 10, 50)
        self.assertTrue(conflicts(r1, r2))

    def test_symmetric(self):
        r1 = res("R1", "A", "B", 0, 60, cooldown=10)
        r2 = res("R2", "B", "C", 55, 80)
        self.assertEqual(conflicts(r1, r2), conflicts(r2, r1))


class MinimalityTests(unittest.TestCase):
    def _reference_chromatic(self, reservations):
        """独立实现的参考色数：暴力枚举颜色数（与生产代码不同算法）。"""
        n = len(reservations)
        adj = [set() for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                if conflicts(reservations[i], reservations[j]):
                    adj[i].add(j)
                    adj[j].add(i)

        def can_color(k):
            colors = [-1] * n

            def backtrack(v):
                if v == n:
                    return True
                for c in range(k):
                    if all(colors[u] != c for u in adj[v]):
                        colors[v] = c
                        if backtrack(v + 1):
                            return True
                        colors[v] = -1
                return False

            return backtrack(0)

        for k in range(1, n + 1):
            if can_color(k):
                return k
        return n

    def test_clique_of_three_needs_three_relays(self):
        # 同一号码 A 的三条预约占区间两两重叠 -> 3 团。
        rs = [
            res("R1", "A", "B", 0, 100),
            res("R2", "A", "C", 10, 90),
            res("R3", "A", "D", 20, 80),
        ]
        alloc = RelayAllocator(rs).allocate()
        self.assertEqual(alloc.relay_count, 3)
        self.assertEqual(alloc.relay_count, self._reference_chromatic(rs))

    def test_five_cycle_needs_three_colors(self):
        # 构造冲突图 C5：共享号码 S_i 恰好出现在 E_i 与 E_{i+1 mod 5} 中，
        # 每个号码只属于两条预约，保证图中只有 5 条边、无弦。
        rs = [
            res("E0", "S4", "S0", 0, 100),
            res("E1", "S0", "S1", 0, 100),
            res("E2", "S1", "S2", 0, 100),
            res("E3", "S2", "S3", 0, 100),
            res("E4", "S3", "S4", 0, 100),
        ]
        allocator = RelayAllocator(rs)
        self.assertEqual(len(allocator.conflict_pairs()), 5)
        alloc = allocator.allocate()
        self.assertEqual(alloc.relay_count, 3)
        self.assertEqual(alloc.relay_count, self._reference_chromatic(rs))

    def test_independent_set_uses_single_relay(self):
        rs = [
            res("R1", "A", "B", 0, 10),
            res("R2", "C", "D", 20, 30),
            res("R3", "E", "F", 40, 50),
        ]
        alloc = RelayAllocator(rs).allocate()
        self.assertEqual(alloc.relay_count, 1)

    def test_empty_batch(self):
        alloc = RelayAllocator([]).allocate()
        self.assertEqual(alloc.relay_count, 0)
        self.assertEqual(dict(alloc.mapping), {})

    def test_dense_batch_matches_reference(self):
        # 14 条预约的交错批：共享号码 + 重叠/不重叠区间混合。
        rs = []
        for i in range(MAX_RESERVATIONS):
            other = f"Q{i}"
            # 偶数预约共享主叫 S，奇数预约使用独立主叫；区间交错。
            caller = "S" if i % 2 == 0 else f"P{i}"
            rs.append(res(f"R{i:02d}", caller, other, i * 5, i * 5 + 40,
                          cooldown=10))
        alloc = RelayAllocator(rs).allocate()
        # 分配必须满足全部冲突约束。
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                if conflicts(rs[i], rs[j]):
                    self.assertNotEqual(
                        alloc.relay_of(rs[i].reservation_id),
                        alloc.relay_of(rs[j].reservation_id),
                    )
        self.assertEqual(alloc.relay_count, self._reference_chromatic(rs))


class PinnedRelayTests(unittest.TestCase):
    def test_pinned_relay_is_preserved(self):
        r1 = res("R1", "A", "B", 0, 60, pin="LINE-9")
        r2 = res("R2", "C", "D", 0, 60)
        alloc = RelayAllocator([r1, r2]).allocate()
        self.assertEqual(alloc.relay_of("R1"), "LINE-9")
        # 无冲突时新预约可复用公布号码。
        self.assertEqual(alloc.relay_of("R2"), "LINE-9")
        self.assertEqual(alloc.relay_count, 1)

    def test_conflicting_pins_same_relay_rejected(self):
        r1 = res("R1", "A", "B", 0, 60, pin="LINE-9")
        r2 = res("R2", "A", "C", 10, 50, pin="LINE-9")
        with self.assertRaises(ConflictingPinError):
            RelayAllocator([r1, r2]).allocate()

    def test_pins_on_different_relays_force_count(self):
        r1 = res("R1", "A", "B", 0, 60, pin="L1")
        r2 = res("R2", "C", "D", 0, 60, pin="L2")  # 本无冲突
        alloc = RelayAllocator([r1, r2]).allocate()
        self.assertEqual(alloc.relay_count, 2)
        self.assertEqual({alloc.relay_of("R1"), alloc.relay_of("R2")}, {"L1", "L2"})

    def test_pin_outside_pool_rejected(self):
        r1 = res("R1", "A", "B", 0, 60, pin="GHOST")
        with self.assertRaises(InvalidReservationError):
            RelayAllocator([r1], relay_pool=["R01", "R02"]).allocate()

    def test_pool_too_small_rejected(self):
        rs = [
            res("R1", "A", "B", 0, 100),
            res("R2", "A", "C", 0, 100),
            res("R3", "A", "D", 0, 100),
        ]
        with self.assertRaises(InsufficientRelaysError):
            RelayAllocator(rs, relay_pool=["R01", "R02"]).allocate()

    def test_pool_labels_used_in_order(self):
        rs = [
            res("R1", "A", "B", 0, 100),
            res("R2", "A", "C", 0, 100),
        ]
        alloc = RelayAllocator(rs, relay_pool=["alpha", "beta"]).allocate()
        self.assertEqual(set(alloc.relays), {"alpha", "beta"})

    def test_allocation_is_cached_and_deterministic(self):
        rs = [res("R1", "A", "B", 0, 10), res("R2", "A", "C", 5, 15)]
        allocator = RelayAllocator(rs)
        self.assertIs(allocator.allocate(), allocator.allocate())


class ValidationTests(unittest.TestCase):
    def test_too_many_reservations(self):
        rs = [res(f"R{i}", f"A{i}", f"B{i}", 0, 10) for i in range(MAX_RESERVATIONS + 1)]
        with self.assertRaises(InvalidReservationError):
            RelayAllocator(rs)

    def test_duplicate_id(self):
        with self.assertRaises(InvalidReservationError):
            RelayAllocator([
                res("R1", "A", "B", 0, 10),
                res("R1", "C", "D", 0, 10),
            ])

    def test_empty_number(self):
        with self.assertRaises(InvalidReservationError):
            RelayAllocator([res("R1", "", "B", 0, 10)])

    def test_same_caller_callee(self):
        with self.assertRaises(InvalidReservationError):
            RelayAllocator([res("R1", "A", "A", 0, 10)])

    def test_bad_interval(self):
        with self.assertRaises(InvalidReservationError):
            RelayAllocator([res("R1", "A", "B", 60, 60)])
        with self.assertRaises(InvalidReservationError):
            RelayAllocator([res("R2", "A", "B", 60, 50)])

    def test_negative_cooldown(self):
        with self.assertRaises(InvalidReservationError):
            RelayAllocator([res("R1", "A", "B", 0, 10, cooldown=-1)])


if __name__ == "__main__":
    unittest.main()
