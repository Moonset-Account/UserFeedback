"""告警事件生命周期的单元测试与样例回归测试。

每个测试方法 docstring 标注稳定 case_id, 与 README 的需求映射表对应。
"""

import json
import os
import subprocess
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from alert_lifecycle import (  # noqa: E402
    AlertManager, CLOSED, EXPIRED, OPEN, parse_time,
)

T = "2026-11-14T09:00:00Z"


def hit(mgr, rid, occurred, arrived=None, tenant="acme", endpoint="ws-001",
        detection="trojan.x"):
    return mgr.handle_hit(rid, tenant, endpoint, detection, occurred,
                          arrived or occurred)


class FirstHitAndAggregationTest(unittest.TestCase):
    def setUp(self):
        self.mgr = AlertManager()

    def test_case_001_first_hit_notifies_immediately(self):
        """CASE-001: 首次命中立即开启事件并通知一次。"""
        r = hit(self.mgr, "r1", T)
        self.assertEqual("new_event", r["action"])
        self.assertTrue(r["notified"])
        self.assertEqual(1, r["count"])
        self.assertEqual(1, len(self.mgr.notifications))
        self.assertEqual(OPEN, self.mgr.events[r["event_id"]].state)

    def test_case_002_duplicates_within_window_aggregate(self):
        """CASE-002: 十分钟窗口内重复命中汇总到同一事件, 计数保留, 不再通知。"""
        hit(self.mgr, "r1", T)
        r2 = hit(self.mgr, "r2", "2026-11-14T09:05:00Z")
        r3 = hit(self.mgr, "r3", "2026-11-14T09:09:59Z")
        self.assertEqual("aggregated", r2["action"])
        self.assertEqual("aggregated", r3["action"])
        self.assertEqual("EVT-0001", r2["event_id"])
        self.assertEqual(3, r3["count"])
        self.assertEqual(1, len(self.mgr.notifications))

    def test_case_003_duplicate_report_id_does_not_count(self):
        """CASE-003: 重复事件 ID 不增加计数。"""
        hit(self.mgr, "r1", T)
        hit(self.mgr, "r2", "2026-11-14T09:01:00Z")
        dup = hit(self.mgr, "r2", "2026-11-14T09:01:00Z",
                  arrived="2026-11-14T09:02:00Z")
        self.assertEqual("duplicate_ignored", dup["action"])
        self.assertEqual(2, dup["count"])
        self.assertEqual(2, self.mgr.events["EVT-0001"].count)
        self.assertEqual(1, len(self.mgr.notifications))

    def test_case_016_window_end_boundary_rolls_new_event(self):
        """CASE-016: 边界——发生时间恰好等于窗口终点(09:10:00)不属于旧窗口。"""
        hit(self.mgr, "r1", T)
        r = hit(self.mgr, "r2", "2026-11-14T09:10:00Z")
        self.assertEqual("new_event", r["action"])
        self.assertEqual("EVT-0002", r["event_id"])
        self.assertEqual(EXPIRED, self.mgr.events["EVT-0001"].state)
        self.assertEqual(2, len(self.mgr.notifications))


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self.mgr = AlertManager()
        hit(self.mgr, "r1", T)

    def test_case_004_confirmed_receipt_closes_event(self):
        """CASE-004: 收到已确认处置回执即关闭事件。"""
        r = self.mgr.handle_receipt("rc1", "EVT-0001", "confirmed",
                                    "2026-11-14T09:05:00Z")
        self.assertEqual("closed", r["action"])
        self.assertEqual(CLOSED, self.mgr.events["EVT-0001"].state)
        self.assertEqual("2026-11-14T09:05:00Z", r["closed_at"])

    def test_case_005_unconfirmed_receipt_keeps_open(self):
        """CASE-005: 未确认处置的回执不关闭事件。"""
        r = self.mgr.handle_receipt("rc1", "EVT-0001", "investigating",
                                    "2026-11-14T09:05:00Z")
        self.assertEqual("receipt_not_confirmed", r["action"])
        self.assertEqual(OPEN, self.mgr.events["EVT-0001"].state)

    def test_case_011_receipt_for_unknown_event(self):
        """CASE-011: 未知事件的回执被记录为 unknown, 不影响现有事件。"""
        r = self.mgr.handle_receipt("rc9", "EVT-9999", "confirmed",
                                    "2026-11-14T09:05:00Z")
        self.assertEqual("receipt_unknown_event", r["action"])
        self.assertEqual(OPEN, self.mgr.events["EVT-0001"].state)

    def test_case_012_duplicate_receipt_ignored(self):
        """CASE-012: 重复回执 ID 被忽略, 不重复处理。"""
        self.mgr.handle_receipt("rc1", "EVT-0001", "confirmed",
                                "2026-11-14T09:05:00Z")
        r = self.mgr.handle_receipt("rc1", "EVT-0001", "confirmed",
                                    "2026-11-14T09:06:00Z")
        self.assertEqual("duplicate_receipt_ignored", r["action"])
        self.assertEqual("2026-11-14T09:05:00Z",
                         self.mgr.events["EVT-0001"].closed_at and
                         self.mgr.events["EVT-0001"].closed_at.isoformat()
                         .replace("+00:00", "Z"))


class LateAndReopenTest(unittest.TestCase):
    def setUp(self):
        self.mgr = AlertManager()
        hit(self.mgr, "r1", T)
        self.mgr.handle_receipt("rc1", "EVT-0001", "confirmed",
                                "2026-11-14T09:06:00Z")

    def test_case_006_late_report_before_close_returns_to_old_event(self):
        """CASE-006: 关闭前发生、关闭后到达的迟到上报归回旧事件, 不重开不通知。"""
        r = hit(self.mgr, "r2", "2026-11-14T09:05:30Z",
                arrived="2026-11-14T09:07:00Z")
        self.assertEqual("late_attributed", r["action"])
        self.assertEqual("EVT-0001", r["event_id"])
        self.assertEqual(1, r["late_count"])
        self.assertEqual(1, r["count"])  # 打开期计数不变
        self.assertEqual(CLOSED, self.mgr.events["EVT-0001"].state)
        self.assertEqual(1, len(self.mgr.notifications))

    def test_case_007_new_report_after_close_opens_new_event(self):
        """CASE-007: 关闭后发生的新上报开启新事件并重新通知。"""
        r = hit(self.mgr, "r2", "2026-11-14T09:07:00Z")
        self.assertEqual("new_event", r["action"])
        self.assertEqual("EVT-0002", r["event_id"])
        self.assertTrue(r["notified"])
        self.assertEqual(2, len(self.mgr.notifications))

    def test_case_017_close_time_boundary_belongs_to_old_event(self):
        """CASE-017: 边界——发生时间恰好等于关闭时间的上报归回旧事件。"""
        r = hit(self.mgr, "r2", "2026-11-14T09:06:00Z",
                arrived="2026-11-14T09:08:00Z")
        self.assertEqual("late_attributed", r["action"])
        self.assertEqual("EVT-0001", r["event_id"])


class TimeAdvanceTest(unittest.TestCase):
    def test_case_008_advance_expires_open_event(self):
        """CASE-008: 显式推进时间使窗口结束的打开事件到期。"""
        mgr = AlertManager()
        mgr.advance_to(T)
        hit(mgr, "r1", T)
        r = mgr.advance_to("2026-11-14T09:10:00Z")
        self.assertEqual(["EVT-0001"], r["expired"])
        self.assertEqual(EXPIRED, mgr.events["EVT-0001"].state)

    def test_case_009_late_report_after_expiry_attributed_to_old(self):
        """CASE-009: 到期事件窗口内发生、到期后到达的迟到上报归回旧事件。"""
        mgr = AlertManager()
        mgr.advance_to(T)
        hit(mgr, "r1", T)
        mgr.advance_to("2026-11-14T09:20:00Z")
        r = hit(mgr, "r2", "2026-11-14T09:09:00Z",
                arrived="2026-11-14T09:20:05Z")
        self.assertEqual("late_attributed", r["action"])
        self.assertEqual("EVT-0001", r["event_id"])
        self.assertEqual(1, r["late_count"])
        self.assertEqual(1, len(mgr.notifications))

    def test_case_014_hit_beyond_window_while_open_rolls_new_event(self):
        """CASE-014: 打开期间发生时间越过窗口的命中使旧事件到期并开启新事件。"""
        mgr = AlertManager()
        hit(mgr, "r1", T)
        r = hit(mgr, "r2", "2026-11-14T09:15:00Z")
        self.assertEqual("new_event", r["action"])
        self.assertEqual("EVT-0001", r["superseded_event_id"])
        self.assertEqual(EXPIRED, mgr.events["EVT-0001"].state)
        self.assertEqual(OPEN, mgr.events["EVT-0002"].state)

    def test_case_018_advance_rejects_backwards_time(self):
        """CASE-018: 显式推进时间不允许回退。"""
        mgr = AlertManager()
        mgr.advance_to("2026-11-14T09:10:00Z")
        with self.assertRaises(ValueError):
            mgr.advance_to(T)


class KeyIsolationTest(unittest.TestCase):
    def test_case_010_keys_are_isolated(self):
        """CASE-010: 不同 (租户, 终端, 检测项) 各自独立聚合与通知。"""
        mgr = AlertManager()
        a = hit(mgr, "r1", T, tenant="acme", endpoint="ws-001")
        b = hit(mgr, "r2", T, tenant="acme", endpoint="ws-002")
        c = hit(mgr, "r3", T, tenant="globex", endpoint="ws-001")
        d = hit(mgr, "r4", T, tenant="acme", endpoint="ws-001",
                detection="ransom.y")
        self.assertEqual(4, len({a["event_id"], b["event_id"],
                                 c["event_id"], d["event_id"]}))
        self.assertEqual(4, len(mgr.notifications))
        again = hit(mgr, "r5", "2026-11-14T09:01:00Z",
                    tenant="acme", endpoint="ws-001")
        self.assertEqual("aggregated", again["action"])
        self.assertEqual(a["event_id"], again["event_id"])


class NotificationTest(unittest.TestCase):
    def test_case_013_one_notification_per_event(self):
        """CASE-013: 同一事件无论聚合多少上报只通知一次; 新事件才再通知。"""
        mgr = AlertManager()
        hit(mgr, "r1", T)
        for i in range(2, 8):
            hit(mgr, "r%d" % i, "2026-11-14T09:0%d:00Z" % min(i, 5))
        self.assertEqual(1, len(mgr.notifications))
        mgr.handle_receipt("rc1", "EVT-0001", "confirmed",
                           "2026-11-14T09:08:00Z")
        hit(mgr, "r9", "2026-11-14T09:09:00Z")
        self.assertEqual(2, len(mgr.notifications))
        self.assertEqual("EVT-0002", mgr.notifications[1]["event_id"])


class CliDemoSampleTest(unittest.TestCase):
    def test_case_015_cli_demo_matches_expected_output(self):
        """CASE-015: 端到端样例——延迟到达、重新开启、事件归属与期望输出一致。"""
        demo = os.path.join(PROJECT_ROOT, "samples", "demo.jsonl")
        expected = os.path.join(PROJECT_ROOT, "samples", "demo_expected.jsonl")
        proc = subprocess.run(
            [sys.executable, os.path.join(PROJECT_ROOT, "app.py"), demo],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(0, proc.returncode, proc.stderr)
        actual = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        with open(expected, "r", encoding="utf-8") as fh:
            want = [json.loads(l) for l in fh if l.strip()]
        self.assertEqual(len(want), len(actual),
                         "output line count mismatch")
        for idx, (w, a) in enumerate(zip(want, actual), start=1):
            self.assertEqual(w, a, "line %d mismatch" % idx)


if __name__ == "__main__":
    unittest.main()
