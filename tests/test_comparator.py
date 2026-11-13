"""Tests for the offline scanner paired comparator.

All expected values below are hand-computed from fixtures/*.json; see
README.md for the full hand-calculation table.
"""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from comparator import compare, match_annotations  # noqa: E402

FIXTURES = os.path.join(ROOT, "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _fixture_report():
    cases = _load("cases.json")["cases"]
    annotations = _load("annotations.json")["annotations"]
    v1 = _load("results_v1.json")["results"]
    v2 = _load("results_v2.json")["results"]
    return compare(cases, annotations, v1, v2)


def _case(report, cid):
    for entry in report["cases"]:
        if entry["case_id"] == cid:
            return entry
    raise AssertionError("case %s not in report" % cid)


class MatchingRuleTests(unittest.TestCase):
    def test_annotation_consumed_at_most_once(self):
        anns = [{"ann_id": "A", "family": "sqli", "location": "f:1"}]
        alerts = [
            {"alert_id": "x1", "family": "sqli", "location": "f:1"},
            {"alert_id": "x2", "family": "sqli", "location": "f:1"},
        ]
        verdicts, fps = match_annotations(anns, alerts)
        self.assertEqual(len(verdicts), 1)
        self.assertIsNotNone(verdicts[0][1])
        # The duplicate alert has no annotation left: one false positive.
        self.assertEqual([a["alert_id"] for a in fps], ["x2"])

    def test_same_family_different_locations_not_merged(self):
        anns = [
            {"ann_id": "A", "family": "sqli", "location": "f:1"},
            {"ann_id": "B", "family": "sqli", "location": "f:2"},
        ]
        alerts = [{"alert_id": "x1", "family": "sqli", "location": "f:1"}]
        verdicts, fps = match_annotations(anns, alerts)
        self.assertIsNotNone(verdicts[0][1])
        self.assertIsNone(verdicts[1][1])  # second location still missed
        self.assertEqual(fps, [])

    def test_family_must_match(self):
        anns = [{"ann_id": "A", "family": "sqli", "location": "f:1"}]
        alerts = [{"alert_id": "x1", "family": "xss", "location": "f:1"}]
        verdicts, fps = match_annotations(anns, alerts)
        self.assertIsNone(verdicts[0][1])
        self.assertEqual(len(fps), 1)


class FixtureReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = _fixture_report()

    def test_case_level_paired_detail(self):
        c1 = _case(self.report, "C1")
        self.assertEqual(c1["status"], "evaluated")
        self.assertEqual(c1["v1"], {"hits": 1, "misses": 1, "false_positives": 0})
        self.assertEqual(c1["v2"], {"hits": 2, "misses": 0, "false_positives": 0})
        self.assertEqual(c1["new_hits"], ["A2"])
        self.assertEqual(c1["lost_hits"], [])
        verdicts = {a["ann_id"]: (a["v1"], a["v2"]) for a in c1["annotations"]}
        self.assertEqual(verdicts, {"A1": ("hit", "hit"), "A2": ("miss", "hit")})

    def test_explicit_clean_case_counts_false_positive(self):
        c2 = _case(self.report, "C2")
        self.assertEqual(c2["status"], "evaluated")
        self.assertEqual(c2["v1"], {"hits": 0, "misses": 0, "false_positives": 1})
        self.assertEqual(c2["v2"], {"hits": 0, "misses": 0, "false_positives": 0})

    def test_lost_hit_regression(self):
        c3 = _case(self.report, "C3")
        self.assertEqual(c3["v1"], {"hits": 1, "misses": 0, "false_positives": 0})
        self.assertEqual(c3["v2"], {"hits": 0, "misses": 1, "false_positives": 0})
        self.assertEqual(c3["new_hits"], [])
        self.assertEqual(c3["lost_hits"], ["A3"])

    def test_duplicate_alert_in_results(self):
        c6 = _case(self.report, "C6")
        # v1 emitted the same cmdi alert twice: one hit + one false positive.
        self.assertEqual(c6["v1"], {"hits": 1, "misses": 0, "false_positives": 1})
        self.assertEqual(c6["v2"], {"hits": 1, "misses": 0, "false_positives": 0})

    def test_missing_annotation_case_is_undecidable(self):
        c4 = _case(self.report, "C4")
        self.assertEqual(c4["status"], "undecidable")
        self.assertEqual(c4["reason"], "missing_annotation")

    def test_single_version_timeout_excluded_from_pairing(self):
        c5 = _case(self.report, "C5")
        self.assertEqual(c5["status"], "undecidable")
        self.assertEqual(c5["reason"], "v2_status:timeout")
        # v1 actually hit A4 on C5, but the case must not enter the paired
        # comparison because v2 did not complete.
        self.assertNotIn("A4", json.dumps(self.report["families"]))

    def test_family_summary(self):
        fams = self.report["families"]
        self.assertEqual(fams["sqli"], {
            "v1": {"hits": 1, "misses": 1, "false_positives": 1},
            "v2": {"hits": 2, "misses": 0, "false_positives": 0},
            "new_hits": 1,
            "lost_hits": 0,
        })
        self.assertEqual(fams["xss"], {
            "v1": {"hits": 1, "misses": 0, "false_positives": 0},
            "v2": {"hits": 0, "misses": 1, "false_positives": 0},
            "new_hits": 0,
            "lost_hits": 1,
        })
        self.assertEqual(fams["cmdi"], {
            "v1": {"hits": 1, "misses": 0, "false_positives": 1},
            "v2": {"hits": 1, "misses": 0, "false_positives": 0},
            "new_hits": 0,
            "lost_hits": 0,
        })

    def test_totals(self):
        totals = self.report["summary"]["totals"]
        self.assertEqual(totals["v1"], {"hits": 3, "misses": 1, "false_positives": 2})
        self.assertEqual(totals["v2"], {"hits": 3, "misses": 1, "false_positives": 0})

    def test_run_coverage_reflects_skipped_hard_case(self):
        summary = self.report["summary"]
        self.assertEqual(summary["total_cases"], 6)
        self.assertEqual(summary["annotated_cases"], 5)
        self.assertEqual(summary["paired_evaluable_cases"], 4)
        # v2 timed out on the hard case C5: coverage drops to 5/6.
        self.assertEqual(summary["run_coverage"]["v1"],
                         {"ok": 6, "total": 6, "coverage": 1.0})
        self.assertEqual(summary["run_coverage"]["v2"],
                         {"ok": 5, "total": 6, "coverage": round(5 / 6, 4)})
        self.assertLess(summary["run_coverage"]["v2"]["coverage"],
                        summary["run_coverage"]["v1"]["coverage"])
        self.assertEqual(summary["undecidable_cases"], [
            {"case_id": "C4", "reason": "missing_annotation"},
            {"case_id": "C5", "reason": "v2_status:timeout"},
        ])

    def test_sample_timing(self):
        timing = self.report["summary"]["timing"]
        self.assertEqual(timing["v1"], {
            "ok_runs": 6, "timeouts": 0, "total_ms": 600, "mean_ms": 100.0,
        })
        self.assertEqual(timing["v2"], {
            "ok_runs": 5, "timeouts": 1, "total_ms": 600, "mean_ms": 120.0,
        })


class CliTests(unittest.TestCase):
    def test_cli_end_to_end(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "app.py"),
             "--cases", os.path.join(FIXTURES, "cases.json"),
             "--annotations", os.path.join(FIXTURES, "annotations.json"),
             "--v1", os.path.join(FIXTURES, "results_v1.json"),
             "--v2", os.path.join(FIXTURES, "results_v2.json")],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["summary"]["paired_evaluable_cases"], 4)
        self.assertEqual(report["families"]["sqli"]["v2"]["hits"], 2)


if __name__ == "__main__":
    unittest.main()
