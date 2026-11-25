"""演示场景与端到端属性测试。"""

import random
import unittest

from anon_relay import (
    CallStatus,
    RelayAllocator,
    Reservation,
    conflicts,
    resolve_callback,
)
from anon_relay.demo import build_demo, run_demo


class DemoTests(unittest.TestCase):
    def test_demo_allocation_uses_two_relays_and_separates_conflicting_pair(self):
        reservations, allocation, scenarios = build_demo()
        self.assertEqual(allocation.relay_count, 2)
        r1, r2 = reservations
        self.assertTrue(conflicts(r1, r2))
        self.assertNotEqual(
            allocation.relay_of("R1"), allocation.relay_of("R2")
        )

    def test_demo_callback_scenarios_match_expectations(self):
        reservations, allocation, scenarios = build_demo()
        for s in scenarios:
            result = resolve_callback(
                s.caller, s.relay, s.at, reservations, allocation
            )
            self.assertEqual(result.status, s.expected_status, msg=s.description)
            if s.expected_status is CallStatus.CONNECTED:
                self.assertEqual(result.other_party, s.expected_other,
                                 msg=s.description)
            else:
                self.assertIsNone(result.other_party, msg=s.description)

    def test_run_demo_returns_results(self):
        results = run_demo()
        self.assertEqual(len(results), 10)
        statuses = [r.status for r in results]
        self.assertIn(CallStatus.CONNECTED, statuses)
        self.assertIn(CallStatus.ENDED, statuses)
        self.assertIn(CallStatus.DENIED, statuses)


class EndToEndPropertyTests(unittest.TestCase):
    """合成数据上的不变量：合法分配 + 回拨安全性。"""

    @staticmethod
    def _generate(seed):
        rng = random.Random(seed)
        numbers = [f"N{k}" for k in range(8)]
        rs = []
        for i in range(14):
            a, b = rng.sample(numbers, 2)
            start = rng.randrange(0, 100)
            end = start + rng.randrange(5, 40)
            cooldown = rng.randrange(0, 25)
            rs.append(Reservation(
                reservation_id=f"R{i:02d}",
                caller=a,
                callee=b,
                start=start,
                end=end,
                cooldown=cooldown,
            ))
        return rs

    def test_allocation_respects_every_conflict_pair(self):
        for seed in range(25):
            with self.subTest(seed=seed):
                rs = self._generate(seed)
                alloc = RelayAllocator(rs).allocate()
                for i in range(len(rs)):
                    for j in range(i + 1, len(rs)):
                        if conflicts(rs[i], rs[j]):
                            self.assertNotEqual(
                                alloc.relay_of(rs[i].reservation_id),
                                alloc.relay_of(rs[j].reservation_id),
                            )

    def test_minimality_against_independent_reference(self):
        # 与 test_allocator 中独立的参考色数实现交叉验证。
        def reference_count(reservations):
            n = len(reservations)
            adj = [set() for _ in range(n)]
            for i in range(n):
                for j in range(i + 1, n):
                    if conflicts(reservations[i], reservations[j]):
                        adj[i].add(j)
                        adj[j].add(i)

            def can_color(k):
                colors = [-1] * n

                def bt(v):
                    if v == n:
                        return True
                    forbidden = {colors[u] for u in adj[v]}
                    for c in range(k):
                        if c not in forbidden:
                            colors[v] = c
                            if bt(v + 1):
                                return True
                            colors[v] = -1
                    return False

                return bt(0)

            for k in range(1, n + 1):
                if can_color(k):
                    return k
            return n

        for seed in range(10):
            with self.subTest(seed=seed):
                rs = self._generate(seed)
                alloc = RelayAllocator(rs).allocate()
                self.assertEqual(alloc.relay_count, reference_count(rs))

    def test_callback_security_invariants(self):
        rng = random.Random(20261126)
        rs = self._generate(7)
        alloc = RelayAllocator(rs).allocate()
        known = {n for r in rs for n in (r.caller, r.callee)}
        strangers = [f"Z{k}" for k in range(5)]
        timeline = list(range(-10, 170, 7))

        for relay in alloc.relays:
            on_relay = [r for r in rs
                        if alloc.relay_of(r.reservation_id) == relay]
            parties = {n for r in on_relay for n in (r.caller, r.callee)}
            for at in timeline:
                # 不变量 1：陌生主叫永远被拒绝且不泄露身份。
                for stranger in strangers:
                    result = resolve_callback(stranger, relay, at, rs, alloc)
                    self.assertEqual(result.status, CallStatus.DENIED)
                    self.assertIsNone(result.other_party)
                    self.assertIsNone(result.reservation_id)

                # 不变量 2：接通时对端必然是同一条预约的另一端，且唯一。
                for party in parties:
                    result = resolve_callback(party, relay, at, rs, alloc)
                    if result.status is CallStatus.CONNECTED:
                        owner = next(
                            r for r in on_relay
                            if r.reservation_id == result.reservation_id
                        )
                        self.assertTrue(owner.involves(party))
                        expected_other = (
                            owner.callee if party == owner.caller else owner.caller
                        )
                        self.assertEqual(result.other_party, expected_other)
                        self.assertTrue(owner.start <= at < owner.end)
                    elif result.status is CallStatus.ENDED:
                        self.assertIsNone(result.other_party)
                        owner = next(
                            r for r in on_relay
                            if r.reservation_id == result.reservation_id
                        )
                        self.assertTrue(owner.end <= at < owner.occupied_end)
                    else:
                        self.assertIsNone(result.other_party)

    def test_pinned_batch_end_to_end(self):
        rs = [
            Reservation("P1", "A", "B", 0, 50, cooldown=10, pinned_relay="L9"),
            Reservation("P2", "A", "C", 20, 80),
            Reservation("P3", "D", "E", 0, 80),
        ]
        alloc = RelayAllocator(rs).allocate()
        self.assertEqual(alloc.relay_of("P1"), "L9")
        self.assertNotEqual(alloc.relay_of("P1"), alloc.relay_of("P2"))
        # P3 与 P1/P2 无共享号码，可复用 L9。
        self.assertEqual(alloc.relay_of("P3"), "L9")
        result = resolve_callback("E", "L9", 40, rs, alloc)
        self.assertEqual(result.status, CallStatus.CONNECTED)
        self.assertEqual(result.other_party, "D")


if __name__ == "__main__":
    unittest.main()
