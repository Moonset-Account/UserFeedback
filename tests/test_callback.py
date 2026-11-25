"""resolve_callback 回拨解析测试。"""

import unittest

from anon_relay import (
    Allocation,
    AmbiguousRoutingError,
    CallStatus,
    RelayAllocator,
    Reservation,
    resolve_callback,
)


def res(rid, a, b, start, end, cooldown=0, pin=None):
    return Reservation(
        reservation_id=rid, caller=a, callee=b,
        start=start, end=end, cooldown=cooldown, pinned_relay=pin,
    )


class CallbackResolutionTests(unittest.TestCase):
    def setUp(self):
        # R1: A<->B [0,60) 冷却 30 -> R01
        # R2: A<->C [60,120) 冷却 30 -> R02（共享 A，占区间重叠 -> 异号）
        self.rs = [
            res("R1", "A", "B", 0, 60, cooldown=30),
            res("R2", "A", "C", 60, 120, cooldown=30),
        ]
        self.alloc = RelayAllocator(self.rs).allocate()
        self.relay1 = self.alloc.relay_of("R1")
        self.relay2 = self.alloc.relay_of("R2")

    def _call(self, caller, relay, at):
        return resolve_callback(caller, relay, at, self.rs, self.alloc)

    def test_active_call_connects_to_unique_other_party(self):
        result = self._call("A", self.relay1, 30)
        self.assertEqual(result.status, CallStatus.CONNECTED)
        self.assertEqual(result.other_party, "B")
        self.assertEqual(result.reservation_id, "R1")
        self.assertTrue(result.connected)
        # 反向拨打同样唯一。
        back = self._call("B", self.relay1, 30)
        self.assertEqual(back.status, CallStatus.CONNECTED)
        self.assertEqual(back.other_party, "A")

    def test_stranger_is_denied_without_identity(self):
        result = self._call("X", self.relay1, 30)
        self.assertEqual(result.status, CallStatus.DENIED)
        self.assertIsNone(result.other_party)
        self.assertIsNone(result.reservation_id)
        # 冷却时段陌生主叫同样被拒绝。
        result2 = self._call("X", self.relay1, 70)
        self.assertEqual(result2.status, CallStatus.DENIED)

    def test_cooldown_callback_returns_ended_and_hides_new_contact(self):
        # t=70：R1 已结束但在冷却期 [60,90)；R2 正在 R02 服务 A<->C。
        result = self._call("A", self.relay1, 70)
        self.assertEqual(result.status, CallStatus.ENDED)
        self.assertEqual(result.reservation_id, "R1")
        self.assertIsNone(result.other_party)  # 绝不能泄露新联系人 C
        # 旧被叫 B 回拨同样结束。
        self.assertEqual(self._call("B", self.relay1, 75).status, CallStatus.ENDED)

    def test_half_open_boundaries(self):
        # t=0 有效期起点（含）-> 接通。
        self.assertEqual(self._call("A", self.relay1, 0).status,
                         CallStatus.CONNECTED)
        # t=60 有效期终点（不含）-> R1 进入冷却 -> 已结束。
        self.assertEqual(self._call("A", self.relay1, 60).status,
                         CallStatus.ENDED)
        # t=90 冷却终点（不含）-> 冷却已过 -> 拒绝。
        self.assertEqual(self._call("A", self.relay1, 90).status,
                         CallStatus.DENIED)
        # t=60 R2 起点（含）-> 接通 C。
        r2 = self._call("A", self.relay2, 60)
        self.assertEqual(r2.status, CallStatus.CONNECTED)
        self.assertEqual(r2.other_party, "C")
        # t=120 R2 终点（不含）-> 冷却 -> 已结束。
        self.assertEqual(self._call("C", self.relay2, 120).status,
                         CallStatus.ENDED)

    def test_before_start_denied(self):
        # 预约尚未开始：不泄露身份。
        self.assertEqual(self._call("A", self.relay2, 10).status,
                         CallStatus.DENIED)

    def test_stranger_cannot_reach_any_relay(self):
        for relay in (self.relay1, self.relay2):
            for at in (10, 60, 70, 100):
                self.assertEqual(
                    self._call("STRANGER", relay, at).status,
                    CallStatus.DENIED,
                )

    def test_shared_relay_routes_correct_reservation(self):
        # 两条互不冲突的预约共用一个中继号，回拨仍能唯一定位。
        rs = [
            res("S1", "A", "B", 0, 10),
            res("S2", "C", "D", 20, 30),
        ]
        alloc = RelayAllocator(rs).allocate()
        self.assertEqual(alloc.relay_count, 1)
        relay = alloc.relay_of("S1")
        r1 = resolve_callback("A", relay, 5, rs, alloc)
        r2 = resolve_callback("D", relay, 25, rs, alloc)
        self.assertEqual(r1.other_party, "B")
        self.assertEqual(r1.reservation_id, "S1")
        self.assertEqual(r2.other_party, "C")
        self.assertEqual(r2.reservation_id, "S2")

    def test_ambiguous_routing_raises(self):
        # 手工构造违反冲突约束的分配：两条同时生效、共享主叫的预约
        # 被放到同一中继号，路由无法唯一确定 -> 报错而非猜测。
        rs = [
            res("T1", "A", "B", 0, 60),
            res("T2", "A", "C", 0, 60),
        ]
        bad_alloc = Allocation(
            mapping={"T1": "R01", "T2": "R01"}, relays=frozenset({"R01"})
        )
        with self.assertRaises(AmbiguousRoutingError):
            resolve_callback("A", "R01", 30, rs, bad_alloc)

    def test_result_carries_relay_and_caller(self):
        result = self._call("A", self.relay1, 30)
        self.assertEqual(result.relay, self.relay1)
        self.assertEqual(result.caller, "A")


if __name__ == "__main__":
    unittest.main()
