"""Unit tests for the process-to-session attribution tool.

All fixtures are synthetic and written into temporary directories.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import attribution  # noqa: E402

CAL = "host,boot_id,offset_seconds,error_seconds\nh1,b1,0,10\n"

SESS_HEADER = "source,event_id,event_type,host,boot_id,session_id,user,timestamp\n"


def sess(event_id, etype, session_id="s1", user="alice", ts="2026-11-14T10:00:00",
         host="h1", boot="b1", source="siem"):
    return "%s,%s,%s,%s,%s,%s,%s,%s\n" % (source, event_id, etype, host, boot,
                                          session_id, user, ts)


def proc(event_id, session_id="s1", pid=100, name="bash",
         ts="2026-11-14T10:30:00", host="h1", boot="b1", source="edr"):
    return json.dumps({"source": source, "event_id": event_id, "host": host,
                       "boot_id": boot, "session_id": session_id, "pid": pid,
                       "process": name, "timestamp": ts}) + "\n"


class AttributionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self._n = 0

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def build(self, session_texts, process_texts, cal_text=CAL):
        cal_path = self.write("cal.csv", cal_text)
        s_paths = [self.write("s%d.csv" % i, SESS_HEADER + t)
                   for i, t in enumerate(session_texts)]
        p_paths = [self.write("p%d.jsonl" % i, t)
                   for i, t in enumerate(process_texts)]
        return attribution.build_report(s_paths, p_paths, cal_path)

    def entry(self, report, event_id):
        for e in report["timeline"]:
            if e["event_id"] == event_id:
                return e
        self.fail("event %s not in timeline" % event_id)


class TestAttribution(AttributionTestCase):
    def test_confirmed_attribution(self):
        report = self.build(
            [sess("e1", "login", ts="2026-11-14T10:00:00")
             + sess("e2", "logout", ts="2026-11-14T11:00:00")],
            [proc("p1")])
        e = self.entry(report, "p1")
        self.assertEqual(e["attribution"]["status"], "confirmed")
        self.assertEqual(e["attribution"]["user"], "alice")
        # offset 0, err 10 -> interval [10:29:50, 10:30:10]
        self.assertEqual(e["utc_interval"],
                         ["2026-11-14T10:29:50", "2026-11-14T10:30:10"])
        # definite window [10:00:10, 10:59:50]
        self.assertEqual(e["attribution"]["session_definite_window_utc"],
                         ["2026-11-14T10:00:10", "2026-11-14T10:59:50"])

    def test_boundary_inclusive(self):
        # process interval [10:00:10, 10:00:30] starts exactly at the
        # definite window's lower bound -> still contained.
        report = self.build(
            [sess("e1", "login", ts="2026-11-14T10:00:00")
             + sess("e2", "logout", ts="2026-11-14T11:00:00")],
            [proc("p1", ts="2026-11-14T10:00:20")])
        self.assertEqual(self.entry(report, "p1")["attribution"]["status"],
                         "confirmed")

    def test_interval_crosses_boundary(self):
        # process interval [09:59:55, 10:00:15] crosses the definite lower
        # bound 10:00:10 -> cannot confirm, but stays on the timeline.
        report = self.build(
            [sess("e1", "login", ts="2026-11-14T10:00:00")
             + sess("e2", "logout", ts="2026-11-14T11:00:00")],
            [proc("p1", ts="2026-11-14T10:00:05")])
        e = self.entry(report, "p1")
        self.assertEqual(e["attribution"]["status"], "unattributed")
        self.assertEqual(e["attribution"]["reason"], "interval_outside_session")

    def test_session_not_closed(self):
        report = self.build(
            [sess("e1", "login", ts="2026-11-14T10:00:00")],
            [proc("p1")])
        e = self.entry(report, "p1")
        self.assertEqual(e["attribution"]["status"], "unattributed")
        self.assertEqual(e["attribution"]["reason"], "session_not_closed")

    def test_missing_calibration(self):
        report = self.build(
            [sess("e1", "login", ts="2026-11-14T10:00:00")
             + sess("e2", "logout", ts="2026-11-14T11:00:00")],
            [proc("p1", host="h2", boot="b9")])
        e = self.entry(report, "p1")
        self.assertEqual(e["attribution"]["status"], "unattributed")
        self.assertEqual(e["attribution"]["reason"], "missing_calibration")
        self.assertIsNone(e["utc_interval"])

    def test_no_matching_session(self):
        report = self.build(
            [sess("e1", "login", ts="2026-11-14T10:00:00")
             + sess("e2", "logout", ts="2026-11-14T11:00:00")],
            [proc("p1", session_id="s-unknown")])
        e = self.entry(report, "p1")
        self.assertEqual(e["attribution"]["reason"], "no_matching_session")

    def test_empty_definite_window(self):
        # login 10:00:00, logout 10:00:05, err 10 -> definite window
        # [10:00:10, 09:59:55] is empty, nothing can be confirmed.
        report = self.build(
            [sess("e1", "login", ts="2026-11-14T10:00:00")
             + sess("e2", "logout", ts="2026-11-14T10:00:05")],
            [proc("p1", ts="2026-11-14T10:00:02")])
        e = self.entry(report, "p1")
        self.assertEqual(e["attribution"]["reason"], "interval_outside_session")

    def test_offset_conversion(self):
        # offset 7200 means local + 7200s = UTC (local clock runs 2h behind
        # UTC), err 5.
        cal = "host,boot_id,offset_seconds,error_seconds\nh1,b1,7200,5\n"
        report = self.build(
            [sess("e1", "login", ts="2026-11-14T12:00:00")
             + sess("e2", "logout", ts="2026-11-14T13:00:00")],
            [proc("p1", ts="2026-11-14T12:30:00")],
            cal_text=cal)
        e = self.entry(report, "p1")
        self.assertEqual(e["attribution"]["status"], "confirmed")
        self.assertEqual(e["utc_interval"],
                         ["2026-11-14T14:29:55", "2026-11-14T14:30:05"])


class TestDedupAndConflicts(AttributionTestCase):
    def test_duplicate_exports_merge_and_keep_all_provenance(self):
        s_text = (sess("e1", "login", ts="2026-11-14T10:00:00")
                  + sess("e2", "logout", ts="2026-11-14T11:00:00"))
        report = self.build(
            [s_text, s_text],               # same session export twice
            [proc("p1"), proc("p1")])       # same process export twice
        self.assertEqual(len(report["timeline"]), 1)
        e = self.entry(report, "p1")
        self.assertEqual(e["attribution"]["status"], "confirmed")
        self.assertEqual(len(e["provenance"]), 2)
        files = sorted(p["file"] for p in e["provenance"])
        self.assertEqual(files, [os.path.join(self.dir, "p0.jsonl"),
                                 os.path.join(self.dir, "p1.jsonl")])
        self.assertEqual(len(report["sessions"]), 1)
        self.assertEqual(len(report["sessions"][0]["provenance"]), 4)
        self.assertEqual(report["conflicts"], [])

    def test_conflicting_session_event_is_listed_and_excluded(self):
        # same (source, event_id) but different timestamps -> conflict;
        # the session then has no usable login -> process unattributed.
        report = self.build(
            [sess("e1", "login", ts="2026-11-14T10:00:00")
             + sess("e1", "login", ts="2026-11-14T10:01:00")
             + sess("e2", "logout", ts="2026-11-14T11:00:00")],
            [proc("p1")])
        kinds = [(c["kind"], c["source"], c["event_id"]) for c in report["conflicts"]]
        self.assertEqual(kinds, [("session", "siem", "e1")])
        conflict = report["conflicts"][0]
        self.assertEqual(len(conflict["variants"]), 2)
        self.assertEqual(self.entry(report, "p1")["attribution"]["reason"],
                         "session_not_closed")

    def test_conflicting_process_event_is_listed_not_in_timeline(self):
        report = self.build(
            [sess("e1", "login", ts="2026-11-14T10:00:00")
             + sess("e2", "logout", ts="2026-11-14T11:00:00")],
            [proc("p1", pid=100) + proc("p1", pid=999)])
        kinds = [(c["kind"], c["source"], c["event_id"]) for c in report["conflicts"]]
        self.assertEqual(kinds, [("process", "edr", "p1")])
        self.assertEqual(report["timeline"], [])

    def test_provenance_line_numbers(self):
        # CSV: header is line 1, first data row line 2, second line 3.
        # JSONL: blank line skipped, line numbers stay physical.
        report = self.build(
            [sess("e1", "login", ts="2026-11-14T10:00:00")
             + sess("e2", "logout", ts="2026-11-14T11:00:00")],
            [proc("p1") + "\n" + proc("p2", ts="2026-11-14T10:40:00")])
        sess_prov = {p["line"] for p in report["sessions"][0]["provenance"]}
        self.assertEqual(sess_prov, {2, 3})
        self.assertEqual(self.entry(report, "p1")["provenance"][0]["line"], 1)
        self.assertEqual(self.entry(report, "p2")["provenance"][0]["line"], 3)


class TestDeterminism(AttributionTestCase):
    def _fixtures(self, subdir):
        d = os.path.join(self.dir, subdir)
        os.makedirs(d)
        cal = os.path.join(d, "cal.csv")
        with open(cal, "w", encoding="utf-8") as fh:
            fh.write(CAL)
        s1 = os.path.join(d, "s1.csv")
        with open(s1, "w", encoding="utf-8") as fh:
            fh.write(SESS_HEADER
                     + sess("e1", "login", ts="2026-11-14T10:00:00")
                     + sess("e2", "logout", ts="2026-11-14T11:00:00"))
        s2 = os.path.join(d, "s2.csv")
        with open(s2, "w", encoding="utf-8") as fh:
            fh.write(SESS_HEADER
                     + sess("e1", "login", ts="2026-11-14T10:00:00")
                     + sess("e3", "login", session_id="s2", user="bob",
                            ts="2026-11-14T12:00:00"))
        p1 = os.path.join(d, "p1.jsonl")
        with open(p1, "w", encoding="utf-8") as fh:
            fh.write(proc("p1") + proc("p2", session_id="s2",
                                       ts="2026-11-14T12:30:00"))
        p2 = os.path.join(d, "p2.jsonl")
        with open(p2, "w", encoding="utf-8") as fh:
            fh.write(proc("p1") + proc("p3", ts="2026-11-14T10:00:05"))
        return cal, s1, s2, p1, p2

    def test_input_file_order_swap_gives_identical_report(self):
        cal, s1, s2, p1, p2 = self._fixtures("a")
        r1 = attribution.build_report([s1, s2], [p1, p2], cal)
        r2 = attribution.build_report([s2, s1], [p2, p1], cal)
        self.assertEqual(json.dumps(r1, sort_keys=True),
                         json.dumps(r2, sort_keys=True))

    def test_record_order_shuffle_gives_identical_report(self):
        cal, s1, s2, p1, p2 = self._fixtures("b")
        r1 = attribution.build_report([s1, s2], [p1, p2], cal)
        # rewrite the same files with reversed record order
        for path, is_csv in ((s1, True), (s2, True), (p1, False), (p2, False)):
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
            if is_csv:
                lines = [lines[0]] + list(reversed(lines[1:]))
            else:
                lines = list(reversed(lines))
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(lines)
        r2 = attribution.build_report([s2, s1], [p2, p1], cal)
        # Line numbers in provenance legitimately change when lines move;
        # everything else in the report must be identical.
        self.assertEqual(_strip_provenance(r1), _strip_provenance(r2))


def _strip_provenance(obj):
    """Recursively remove provenance/line info for order-independence checks."""
    if isinstance(obj, dict):
        return {k: _strip_provenance(v) for k, v in obj.items()
                if k not in ("provenance", "session_provenance")}
    if isinstance(obj, list):
        return [_strip_provenance(v) for v in obj]
    return obj


class TestCli(AttributionTestCase):
    def test_cli_end_to_end(self):
        cal_path = self.write("cal.csv", CAL)
        s_path = self.write("s.csv", SESS_HEADER
                            + sess("e1", "login", ts="2026-11-14T10:00:00")
                            + sess("e2", "logout", ts="2026-11-14T11:00:00"))
        p_path = self.write("p.jsonl", proc("p1"))
        out_path = os.path.join(self.dir, "report.json")
        rc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "app.py"),
             "--sessions", s_path, "--processes", p_path,
             "--calibration", cal_path, "--output", out_path],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(rc.returncode, 0, rc.stderr)
        with open(out_path, encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertEqual(report["timeline"][0]["attribution"]["status"],
                         "confirmed")

    def test_cli_reports_input_errors(self):
        bad = self.write("bad.jsonl", "{not json}\n")
        cal_path = self.write("cal.csv", CAL)
        s_path = self.write("s.csv", SESS_HEADER
                            + sess("e1", "login", ts="2026-11-14T10:00:00"))
        rc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "app.py"),
             "--sessions", s_path, "--processes", bad,
             "--calibration", cal_path],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(rc.returncode, 2)
        self.assertIn("error", rc.stderr.lower())


if __name__ == "__main__":
    unittest.main()
