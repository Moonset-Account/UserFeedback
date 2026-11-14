import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import store
from scheduler import (
    QUEUE_DEFERRED,
    QUEUE_PENDING,
    REASON_CARRYOVER,
    REASON_GENERAL,
    REASON_RESERVED,
    plan_shift,
)


def rec(txn, risk, group, t):
    return {"txn_id": txn, "risk_level": risk, "merchant_group": group,
            "enqueued_at": t}


class TestReservedPhase(unittest.TestCase):
    def test_reserved_fills_highest_risk_by_enqueue_time(self):
        # 保留名额只给最高风险组，按入队时间先后填
        records = [
            rec("A2", 3, "g1", "2026-11-14T08:02:00"),
            rec("A1", 3, "g1", "2026-11-14T08:01:00"),
            rec("B1", 2, "g2", "2026-11-14T07:59:00"),  # 更早但风险低
        ]
        result = plan_shift(records, reserved_slots=2, general_slots=0)
        self.assertEqual([s["txn_id"] for s in result["selected"]], ["A1", "A2"])
        self.assertTrue(all(s["reason"] == REASON_RESERVED for s in result["selected"]))
        self.assertEqual([p["txn_id"] for p in result["pending"]], ["B1"])

    def test_cap_counts_across_reserved_and_general(self):
        # 同班上限对保留名额和普通名额阶段合并计数
        records = [
            rec("A1", 3, "g1", "2026-11-14T08:01:00"),
            rec("A2", 3, "g1", "2026-11-14T08:02:00"),
            rec("A3", 3, "g1", "2026-11-14T08:03:00"),
        ]
        result = plan_shift(records, reserved_slots=1, general_slots=2,
                            group_caps={"g1": 2})
        self.assertEqual([s["txn_id"] for s in result["selected"]], ["A1", "A2"])
        self.assertEqual(result["selected"][0]["reason"], REASON_RESERVED)
        self.assertEqual(result["selected"][1]["reason"], REASON_GENERAL)
        self.assertEqual([d["txn_id"] for d in result["deferred_cap"]], ["A3"])
        self.assertEqual(result["deferred_cap"][0]["queue"], QUEUE_DEFERRED)


class TestGeneralPhase(unittest.TestCase):
    def test_unused_reserved_merges_into_general(self):
        # 最高风险组只有 1 条，保留名额剩 1 个并入普通名额
        records = [
            rec("A1", 3, "g1", "2026-11-14T08:01:00"),
            rec("B1", 2, "g2", "2026-11-14T08:02:00"),
            rec("B2", 2, "g2", "2026-11-14T08:03:00"),
        ]
        result = plan_shift(records, reserved_slots=2, general_slots=1)
        self.assertEqual([s["txn_id"] for s in result["selected"]],
                         ["A1", "B1", "B2"])
        self.assertEqual(result["slots"]["reserved_unused_merged"], 1)

    def test_general_orders_by_risk_then_enqueue_time(self):
        records = [
            rec("L1", 1, "g1", "2026-11-14T07:00:00"),  # 最早但风险最低
            rec("H2", 3, "g1", "2026-11-14T09:02:00"),
            rec("H1", 3, "g1", "2026-11-14T09:01:00"),
            rec("M1", 2, "g1", "2026-11-14T08:00:00"),
        ]
        result = plan_shift(records, reserved_slots=0, general_slots=3)
        self.assertEqual([s["txn_id"] for s in result["selected"]],
                         ["H1", "H2", "M1"])
        self.assertEqual([p["txn_id"] for p in result["pending"]], ["L1"])

    def test_pending_when_slots_exhausted(self):
        records = [rec("T%d" % i, 1, "g1", "2026-11-14T08:0%d:00" % i)
                   for i in range(4)]
        result = plan_shift(records, reserved_slots=0, general_slots=2)
        self.assertEqual(len(result["selected"]), 2)
        self.assertEqual([p["txn_id"] for p in result["pending"]], ["T2", "T3"])
        self.assertTrue(all(p["queue"] == QUEUE_PENDING for p in result["pending"]))

    def test_group_cap_defers_excess_to_carryover_queue(self):
        records = [
            rec("A1", 3, "hot", "2026-11-14T08:01:00"),
            rec("A2", 3, "hot", "2026-11-14T08:02:00"),
            rec("A3", 3, "hot", "2026-11-14T08:03:00"),
            rec("B1", 1, "cold", "2026-11-14T08:04:00"),
        ]
        result = plan_shift(records, reserved_slots=0, general_slots=3,
                            group_caps={"hot": 2})
        self.assertEqual([s["txn_id"] for s in result["selected"]],
                         ["A1", "A2", "B1"])
        self.assertEqual([d["txn_id"] for d in result["deferred_cap"]], ["A3"])


class TestCarryoverAndClaimed(unittest.TestCase):
    def test_carryover_has_priority_over_new_arrivals(self):
        carryover = [rec("OLD", 1, "g1", "2026-11-13T08:00:00")]
        records = [rec("NEW", 3, "g2", "2026-11-14T08:00:00")]
        result = plan_shift(records, reserved_slots=0, general_slots=1,
                            carryover=carryover)
        self.assertEqual([s["txn_id"] for s in result["selected"]], ["OLD"])
        self.assertEqual(result["selected"][0]["reason"], REASON_CARRYOVER)
        self.assertEqual([p["txn_id"] for p in result["pending"]], ["NEW"])

    def test_carryover_still_capped_stays_deferred(self):
        carryover = [rec("OLD1", 2, "g1", "2026-11-13T08:00:00"),
                     rec("OLD2", 2, "g1", "2026-11-13T08:01:00")]
        result = plan_shift([], reserved_slots=0, general_slots=2,
                            group_caps={"g1": 1}, carryover=carryover)
        self.assertEqual([s["txn_id"] for s in result["selected"]], ["OLD1"])
        self.assertEqual([d["txn_id"] for d in result["deferred_cap"]], ["OLD2"])
        self.assertEqual(result["deferred_cap"][0]["queue"], QUEUE_DEFERRED)

    def test_claimed_txns_excluded_from_later_batches(self):
        records = [rec("T1", 3, "g1", "2026-11-14T08:01:00"),
                   rec("T2", 3, "g1", "2026-11-14T08:02:00")]
        result = plan_shift(records, reserved_slots=0, general_slots=2,
                            claimed={"T1"})
        self.assertEqual([s["txn_id"] for s in result["selected"]], ["T2"])
        self.assertEqual(result["excluded_claimed"], ["T1"])


class TestExecuteShiftRetry(unittest.TestCase):
    def test_retry_reuses_first_locked_list(self):
        # 事务重试：同一 shift_id 复用首次锁定名单，即使输入已变化
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            spec1 = {"shift_id": "S1", "reserved_slots": 0, "general_slots": 1,
                     "records": [rec("T1", 3, "g1", "2026-11-14T08:01:00")]}
            state = store.load_state(state_path)
            result1, state, reused1 = app.execute_shift(spec1, state)
            store.save_state(state_path, state)
            self.assertFalse(reused1)

            spec2 = dict(spec1)
            spec2["records"] = [rec("T9", 3, "g1", "2026-11-14T08:09:00")]
            state = store.load_state(state_path)
            result2, state, reused2 = app.execute_shift(spec2, state)
            self.assertTrue(reused2)
            self.assertEqual([s["txn_id"] for s in result2["selected"]], ["T1"])
            self.assertTrue(result2["reused_lock"])

    def test_execute_shift_updates_claimed_and_carryover(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            spec = {"shift_id": "S1", "reserved_slots": 0, "general_slots": 1,
                    "group_caps": {"hot": 1},
                    "records": [rec("A1", 3, "hot", "2026-11-14T08:01:00"),
                                rec("A2", 3, "hot", "2026-11-14T08:02:00")]}
            state = store.load_state(state_path)
            _, state, _ = app.execute_shift(spec, state)
            store.save_state(state_path, state)
            reloaded = store.load_state(state_path)
            self.assertEqual(reloaded["claimed"], ["A1"])
            self.assertEqual([c["txn_id"] for c in reloaded["carryover"]], ["A2"])
            self.assertIn("S1", reloaded["locks"])


class TestTwoShiftSample(unittest.TestCase):
    def test_low_freq_group_filled_in_second_shift(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            results = {}
            for name in ("shift_s1.json", "shift_s2.json"):
                spec = app.load_shift_spec(os.path.join(root, "samples", name))
                state = store.load_state(state_path)
                result, state, _ = app.execute_shift(spec, state)
                store.save_state(state_path, state)
                results[spec["shift_id"]] = result

        s1, s2 = results["S1"], results["S2"]
        # 第一班：保留名额给最高风险 bulkA 的 T1/T2，T3 触发组上限顺延，
        # 普通名额给 T4；低频组 rare 的 T5 名额耗尽留在 pending
        self.assertEqual([s["txn_id"] for s in s1["selected"]], ["T1", "T2", "T4"])
        self.assertEqual([d["txn_id"] for d in s1["deferred_cap"]], ["T3"])
        self.assertIn("T5", [p["txn_id"] for p in s1["pending"]])
        # 第二班：T1 已被领取被过滤；顺延的 T3 优先补位；
        # 低频组 rare 的 T5 在普通名额阶段被补进
        self.assertEqual(s2["excluded_claimed"], ["T1"])
        by_txn = {s["txn_id"]: s for s in s2["selected"]}
        self.assertEqual(by_txn["T3"]["reason"], REASON_CARRYOVER)
        self.assertEqual(by_txn["T5"]["merchant_group"], "rare")
        self.assertEqual(by_txn["T5"]["reason"], REASON_GENERAL)


class TestValidation(unittest.TestCase):
    def test_missing_field_rejected(self):
        with self.assertRaises(ValueError):
            plan_shift([{"txn_id": "X"}], 0, 1)

    def test_negative_slots_rejected(self):
        with self.assertRaises(ValueError):
            plan_shift([], -1, 0)


if __name__ == "__main__":
    unittest.main()
