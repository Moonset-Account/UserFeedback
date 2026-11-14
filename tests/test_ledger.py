"""电子证物交接事件台账 —— 单元测试(unittest,仅标准库)。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger_core import Location, build_report, load_records, process_evidence


def ev(evidence_id, seq, etype, **kw):
    d = {"evidence_id": evidence_id, "seq": seq, "type": etype}
    d.update(kw)
    return d


def report_for(events, eid="EV-T"):
    """events: [event_dict,...] -> 该证物的台账条目"""
    records = [(e, Location("mem.jsonl", i + 1)) for i, e in enumerate(events)]
    report = build_report(records, [])
    for item in report["evidence"]:
        if item["evidence_id"] == eid:
            return item
    raise AssertionError("evidence %s not in report" % eid)


class CustodyFlowTests(unittest.TestCase):
    def test_intake_sets_custodian(self):
        item = report_for([ev("EV-T", 1, "intake", custodian="alice")])
        self.assertEqual(item["current_custodian"], "alice")
        self.assertEqual(item["status"], "in_custody")

    def test_unconfirmed_transfer_stays_in_transfer(self):
        # 借出后接收方未确认:仍显示交接中,保管人不变
        item = report_for([
            ev("EV-T", 1, "intake", custodian="alice"),
            ev("EV-T", 2, "transfer_init", transfer_id="T1",
               **{"from": "alice", "to": "bob"}),
        ])
        self.assertEqual(item["status"], "in_transfer")
        self.assertEqual(item["current_custodian"], "alice")
        self.assertEqual(len(item["pending_transfers"]), 1)
        self.assertEqual(item["pending_transfers"][0]["to"], "bob")

    def test_confirm_changes_custodian(self):
        item = report_for([
            ev("EV-T", 1, "intake", custodian="alice"),
            ev("EV-T", 2, "transfer_init", transfer_id="T1",
               **{"from": "alice", "to": "bob"}),
            ev("EV-T", 3, "receive_confirm", transfer_id="T1", by="bob"),
        ])
        self.assertEqual(item["current_custodian"], "bob")
        self.assertEqual(item["status"], "in_custody")
        self.assertEqual(item["pending_transfers"], [])

    def test_transfer_by_non_custodian_rejected(self):
        item = report_for([
            ev("EV-T", 1, "intake", custodian="alice"),
            ev("EV-T", 2, "transfer_init", transfer_id="T1",
               **{"from": "carol", "to": "bob"}),
        ])
        self.assertEqual(item["current_custodian"], "alice")
        self.assertEqual(item["pending_transfers"], [])
        self.assertEqual(item["rejected_events"][0]["reason"],
                         "initiator_not_current_custodian")

    def test_confirm_wrong_transfer_rejected(self):
        item = report_for([
            ev("EV-T", 1, "intake", custodian="alice"),
            ev("EV-T", 2, "transfer_init", transfer_id="T1",
               **{"from": "alice", "to": "bob"}),
            ev("EV-T", 3, "receive_confirm", transfer_id="T-OTHER", by="bob"),
        ])
        self.assertEqual(item["current_custodian"], "alice")
        self.assertEqual(item["rejected_events"][0]["reason"],
                         "no_matching_pending_transfer")

    def test_confirm_by_wrong_receiver_rejected(self):
        item = report_for([
            ev("EV-T", 1, "intake", custodian="alice"),
            ev("EV-T", 2, "transfer_init", transfer_id="T1",
               **{"from": "alice", "to": "bob"}),
            ev("EV-T", 3, "receive_confirm", transfer_id="T1", by="carol"),
        ])
        self.assertEqual(item["current_custodian"], "alice")
        self.assertEqual(item["rejected_events"][0]["reason"],
                         "confirmer_not_designated_receiver")

    def test_revoke_cancels_pending_transfer(self):
        item = report_for([
            ev("EV-T", 1, "intake", custodian="alice"),
            ev("EV-T", 2, "transfer_init", transfer_id="T1",
               **{"from": "alice", "to": "bob"}),
            ev("EV-T", 3, "revoke", transfer_id="T1", by="alice"),
        ])
        self.assertEqual(item["status"], "in_custody")
        self.assertEqual(item["pending_transfers"], [])
        self.assertEqual(item["current_custodian"], "alice")

    def test_revoke_by_non_initiator_rejected(self):
        item = report_for([
            ev("EV-T", 1, "intake", custodian="alice"),
            ev("EV-T", 2, "transfer_init", transfer_id="T1",
               **{"from": "alice", "to": "bob"}),
            ev("EV-T", 3, "revoke", transfer_id="T1", by="bob"),
        ])
        self.assertEqual(item["status"], "in_transfer")
        self.assertEqual(item["rejected_events"][0]["reason"],
                         "revoker_not_initiator")

    def test_review_does_not_change_custody(self):
        item = report_for([
            ev("EV-T", 1, "intake", custodian="alice"),
            ev("EV-T", 2, "review", reviewer="dave", note="ok"),
        ])
        self.assertEqual(item["current_custodian"], "alice")
        self.assertEqual(len(item["reviews"]), 1)


class DedupConflictTests(unittest.TestCase):
    def test_identical_duplicate_applied_once(self):
        e = ev("EV-T", 2, "transfer_init", transfer_id="T1",
               **{"from": "alice", "to": "bob"})
        item = report_for([
            ev("EV-T", 1, "intake", custodian="alice"),
            dict(e), dict(e),  # 同一事件重复上报
        ])
        applied_seq2 = [a for a in item["applied_events"] if a["seq"] == 2]
        self.assertEqual(len(applied_seq2), 1)
        # 两个来源位置都保留
        self.assertEqual(len(applied_seq2[0]["locations"]), 2)
        self.assertEqual(item["conflicts"], [])

    def test_same_seq_different_content_conflict(self):
        item = report_for([
            ev("EV-T", 1, "intake", custodian="alice"),
            ev("EV-T", 2, "transfer_init", transfer_id="T1",
               **{"from": "alice", "to": "bob"}),
            ev("EV-T", 2, "transfer_init", transfer_id="T2",
               **{"from": "alice", "to": "carol"}),
        ])
        self.assertEqual(len(item["conflicts"]), 1)
        self.assertEqual(item["conflicts"][0]["seq"], 2)
        # 两种内容都保留
        self.assertEqual(len(item["conflicts"][0]["variants"]), 2)
        # 冲突即中断:只确认到 seq 1,保管人不变
        self.assertEqual(item["confirmed_through_seq"], 1)
        self.assertEqual(item["current_custodian"], "alice")
        self.assertTrue(item["interrupted"])

    def test_out_of_order_events_sorted_by_seq(self):
        item = report_for([
            ev("EV-T", 3, "receive_confirm", transfer_id="T1", by="bob"),
            ev("EV-T", 1, "intake", custodian="alice"),
            ev("EV-T", 2, "transfer_init", transfer_id="T1",
               **{"from": "alice", "to": "bob"}),
        ])
        self.assertEqual(item["current_custodian"], "bob")
        self.assertEqual(item["confirmed_through_seq"], 3)


class InterruptionTests(unittest.TestCase):
    def test_sequence_gap_stops_confirmation(self):
        item = report_for([
            ev("EV-T", 1, "intake", custodian="alice"),
            ev("EV-T", 2, "transfer_init", transfer_id="T1",
               **{"from": "alice", "to": "bob"}),
            # seq 3 缺失
            ev("EV-T", 4, "receive_confirm", transfer_id="T1", by="bob"),
        ])
        self.assertEqual(item["confirmed_through_seq"], 2)
        self.assertTrue(item["interrupted"])
        self.assertEqual(item["interruption"]["reason"], "sequence_gap")
        # 断档后的确认不应用:保管人不变,移交仍待确认
        self.assertEqual(item["current_custodian"], "alice")
        self.assertEqual(item["status"], "in_transfer")
        # 后续记录进入人工复核,并保留原始位置
        self.assertEqual([r["seq"] for r in item["needs_review"]], [4])
        loc = item["needs_review"][0]["locations"][0]
        self.assertIn("file", loc)
        self.assertIn("line", loc)

    def test_missing_first_seq_interrupts_immediately(self):
        item = report_for([
            ev("EV-T", 2, "transfer_init", transfer_id="T1",
               **{"from": "alice", "to": "bob"}),
        ])
        self.assertEqual(item["confirmed_through_seq"], 0)
        self.assertTrue(item["interrupted"])
        self.assertEqual(item["status"], "unknown")

    def test_malformed_event_interrupts(self):
        item = report_for([
            ev("EV-T", 1, "intake", custodian="alice"),
            ev("EV-T", 2, "transfer_init", transfer_id="T1",
               **{"from": "alice"}),  # 缺 to
            ev("EV-T", 3, "review", reviewer="dave"),
        ])
        self.assertEqual(item["confirmed_through_seq"], 1)
        self.assertTrue(item["interrupted"])
        self.assertIn("malformed_event", item["interruption"]["reason"])
        self.assertEqual([r["seq"] for r in item["needs_review"]], [2, 3])


class LoadRecordsTests(unittest.TestCase):
    def _write(self, dirpath, name, lines):
        path = os.path.join(dirpath, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def test_load_batches_and_locations(self):
        with tempfile.TemporaryDirectory() as d:
            p1 = self._write(d, "b1.jsonl", [
                json.dumps(ev("EV-1", 1, "intake", custodian="alice")),
                "",
                json.dumps(ev("EV-1", 2, "review", reviewer="dave")),
            ])
            p2 = self._write(d, "b2.jsonl", [
                json.dumps(ev("EV-2", 1, "intake", custodian="bob")),
            ])
            events, invalid = load_records([p2, p1])
            self.assertEqual(invalid, [])
            self.assertEqual(len(events), 3)
            locs = {(loc.path, loc.line) for _, loc in events}
            self.assertIn((p1, 1), locs)
            self.assertIn((p1, 3), locs)
            self.assertIn((p2, 1), locs)

    def test_bad_lines_become_invalid_records(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "bad.jsonl", [
                "{not json",
                json.dumps({"seq": 1, "type": "intake"}),  # 缺 evidence_id
                json.dumps(ev("EV-1", 1, "intake", custodian="alice")),
                json.dumps(ev("EV-1", 2, "teleport")),     # 未知类型
            ])
            events, invalid = load_records([p])
            self.assertEqual(len(events), 1)
            self.assertEqual(len(invalid), 3)
            self.assertTrue(all("location" in r for r in invalid))


class ReportShapeTests(unittest.TestCase):
    def test_multi_evidence_grouped_and_sorted(self):
        records = [
            (ev("EV-B", 1, "intake", custodian="bob"), Location("f", 1)),
            (ev("EV-A", 1, "intake", custodian="alice"), Location("f", 2)),
        ]
        report = build_report(records, [])
        self.assertEqual([i["evidence_id"] for i in report["evidence"]],
                         ["EV-A", "EV-B"])
        self.assertEqual(report["evidence_count"], 2)

    def test_process_evidence_directly(self):
        item = process_evidence("EV-D", [
            (ev("EV-D", 1, "intake", custodian="alice"), Location("f", 1)),
        ])
        self.assertEqual(item["evidence_id"], "EV-D")
        self.assertFalse(item["interrupted"])


if __name__ == "__main__":
    unittest.main()
