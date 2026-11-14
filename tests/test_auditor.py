"""Unit tests for the SEC-20261114-045 load-segment W^X page auditor.

Every test method name carries the stable case_id that README.md and
tools/local_verify.py refer to.
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

import pageaudit
from pageaudit import audit_build, audit_document, parse_address

SAMPLES = os.path.join(ROOT, "samples")
APP = os.path.join(ROOT, "app.py")


def load_sample(name):
    with open(os.path.join(SAMPLES, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def make_build(**overrides):
    """Two segments that are disjoint as byte ranges but share page 1."""
    build = {
        "build_id": "t",
        "page_size": 0x1000,
        "merge_rule": "union",
        "segments": [
            {"name": "a", "start": "0x1000", "end": "0x1800", "perms": "r-x"},
            {"name": "b", "start": "0x1800", "end": "0x1c00", "perms": "rw-"},
        ],
    }
    build.update(overrides)
    return build


class MergeRuleTests(unittest.TestCase):
    def test_MERGE_UNION_001(self):
        # Disjoint segments aligned onto one page, union rule -> W+X page.
        report = audit_build(make_build())
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["affected_page_count"], 1)
        page = report["affected_pages"][0]
        self.assertEqual(page["page_index"], 1)
        self.assertEqual(page["effective_perms"], "rwx")
        self.assertEqual(page["contributing_segments"], ["a", "b"])
        self.assertEqual(page["applied_rule"], "union")

    def test_MERGE_LAST_001(self):
        # Same layout with last_loaded: only the last segment's perms count ("rw-")
        # remain, so the page is not W+X.
        report = audit_build(make_build(merge_rule="last_loaded"))
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["affected_page_count"], 0)
        self.assertEqual(report["affected_pages"], [])

    def test_MERGE_LAST_002(self):
        # Load order decides the outcome under last_loaded.
        segs = [
            {"name": "first", "start": "0x1000", "end": "0x1400", "perms": "rwx"},
            {"name": "second", "start": "0x1200", "end": "0x1800", "perms": "r-x"},
        ]
        report = audit_build(make_build(merge_rule="last_loaded", segments=segs))
        self.assertEqual(report["affected_page_count"], 0)  # "second" wins: r-x
        report = audit_build(make_build(merge_rule="last_loaded",
                                        segments=list(reversed(segs))))
        self.assertEqual(report["affected_page_count"], 1)  # "first" wins: rwx
        self.assertEqual(report["affected_pages"][0]["effective_perms"], "rwx")
        # Same two layouts under union are always W+X.
        report = audit_build(make_build(merge_rule="union", segments=segs))
        self.assertEqual(report["affected_page_count"], 1)


class AlignmentTests(unittest.TestCase):
    def test_ALIGN_001(self):
        # The two delivered sample mappings: original segments do not overlap
        # ([0x1000,0x1800) vs [0x1800,0x1c00)) but align onto the same page;
        # union flags the page, last_loaded does not.
        union_doc = load_sample("mapping_union.json")
        segs = union_doc["builds"][0]["segments"]
        self.assertLessEqual(int(segs[0]["end"], 16), int(segs[1]["start"], 16))
        union_report = audit_document(union_doc)["build_reports"][0]
        last_report = audit_document(
            load_sample("mapping_last_loaded.json"))["build_reports"][0]
        self.assertEqual(union_report["affected_page_count"], 1)
        self.assertEqual(union_report["affected_pages"][0]["page_index"], 1)
        self.assertEqual(last_report["affected_page_count"], 0)

    def test_ALIGN_002(self):
        # One segment spanning a page boundary covers both pages.
        segs = [{"name": "big", "start": "0x1800", "end": "0x2800",
                 "perms": "rwx"}]
        report = audit_build(make_build(segments=segs))
        self.assertEqual([p["page_index"] for p in report["affected_pages"]],
                         [1, 2])

    def test_ALIGN_003(self):
        # End address is exclusive: [0x1000,0x2000) covers page 1 only.
        segs = [{"name": "exact", "start": "0x1000", "end": "0x2000",
                 "perms": "rwx"}]
        report = audit_build(make_build(segments=segs))
        self.assertEqual([p["page_index"] for p in report["affected_pages"]],
                         [1])

    def test_ALIGN_004(self):
        # Segments on different pages are never merged, even under union.
        segs = [
            {"name": "a", "start": "0x1000", "end": "0x1800", "perms": "r-x"},
            {"name": "b", "start": "0x2800", "end": "0x2c00", "perms": "rw-"},
        ]
        report = audit_build(make_build(segments=segs))
        self.assertEqual(report["affected_page_count"], 0)


class ValidationTests(unittest.TestCase):
    def _error_type(self, build):
        report = audit_build(build)
        self.assertEqual(report["status"], "error")
        return report["error"]["type"]

    def test_ERR_ADDR_001(self):
        # 2^64 is not representable in the 64-bit address space.
        segs = [{"name": "a", "start": 1 << 64, "end": (1 << 64) + 0x1000,
                 "perms": "r-x"}]
        self.assertEqual(self._error_type(make_build(segments=segs)),
                         "address_unrepresentable")

    def test_ERR_ADDR_002(self):
        segs = [{"name": "a", "start": -1, "end": "0x1000", "perms": "r-x"}]
        self.assertEqual(self._error_type(make_build(segments=segs)),
                         "address_unrepresentable")

    def test_ERR_ADDR_003(self):
        segs = [{"name": "a", "start": "not-an-address", "end": "0x1000",
                 "perms": "r-x"}]
        self.assertEqual(self._error_type(make_build(segments=segs)),
                         "address_unrepresentable")

    def test_ERR_ADDR_004(self):
        # Decimal and hex strings are both accepted forms.
        self.assertEqual(parse_address("0x10", "t"), 16)
        self.assertEqual(parse_address("16", "t"), 16)
        self.assertEqual(parse_address(16, "t"), 16)

    def test_ERR_PAGE_001(self):
        self.assertEqual(self._error_type(make_build(page_size=1000)),
                         "invalid_page_size")

    def test_ERR_PAGE_002(self):
        self.assertEqual(self._error_type(make_build(page_size=0)),
                         "invalid_page_size")

    def test_ERR_PAGE_003(self):
        self.assertEqual(self._error_type(make_build(page_size="4096")),
                         "invalid_page_size")

    def test_ERR_MISSING_001(self):
        build = make_build()
        del build["segments"]
        self.assertEqual(self._error_type(build), "missing_mapping_records")

    def test_ERR_MISSING_002(self):
        self.assertEqual(self._error_type(make_build(segments=[])),
                         "missing_mapping_records")

    def test_ERR_SEGMENT_001(self):
        segs = [{"name": "a", "start": "0x1000", "end": "0x1000",
                 "perms": "r-x"}]
        self.assertEqual(self._error_type(make_build(segments=segs)),
                         "invalid_segment")

    def test_ERR_RULE_001(self):
        self.assertEqual(self._error_type(make_build(merge_rule="first")),
                         "invalid_merge_rule")

    def test_BOUNDARY_MAX_001(self):
        # Top of the 64-bit address space is representable and auditable.
        segs = [{"name": "top", "start": "0xfffffffffffff000",
                 "end": "0xffffffffffffffff", "perms": "rwx"}]
        report = audit_build(make_build(segments=segs))
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["affected_page_count"], 1)
        page = report["affected_pages"][0]
        self.assertEqual(page["page_index"], (1 << 64) // 4096 - 1)
        self.assertEqual(page["start"], "0xfffffffffffff000")
        self.assertEqual(page["end"], "0x10000000000000000")


class ReportShapeTests(unittest.TestCase):
    def test_REPORT_001(self):
        # Affected-page entries carry range, contributors and applied rule.
        report = audit_build(make_build())
        page = report["affected_pages"][0]
        self.assertEqual(
            set(page),
            {"page_index", "start", "end", "effective_perms",
             "contributing_segments", "applied_rule"})
        self.assertEqual(page["start"], "0x1000")
        self.assertEqual(page["end"], "0x2000")

    def test_SUMMARY_001(self):
        # Mixed sample: 1 ok build, 3 flagged builds, 1 affected page total.
        report = audit_document(load_sample("mixed_builds.json"))
        self.assertEqual(report["summary"],
                         {"total_builds": 4, "ok_builds": 1,
                          "error_builds": 3, "total_affected_pages": 1})
        error_types = {r["error"]["type"]
                       for r in report["build_reports"]
                       if r["status"] == "error"}
        self.assertEqual(error_types,
                         {"address_unrepresentable", "invalid_page_size",
                          "missing_mapping_records"})

    def test_DOC_001(self):
        # A document without any build records is flagged, not crashed on.
        report = audit_document({})
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["error"]["type"], "missing_mapping_records")


class CliTests(unittest.TestCase):
    def test_CLI_001(self):
        proc = subprocess.run(
            [sys.executable, APP, os.path.join(SAMPLES, "mixed_builds.json")],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["summary"]["error_builds"], 3)
        self.assertEqual(report["summary"]["total_affected_pages"], 1)

    def test_CLI_002(self):
        proc = subprocess.run(
            [sys.executable, APP, os.path.join(SAMPLES, "does-not-exist.json")],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 2)

    def test_CLI_003(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.json")
            proc = subprocess.run(
                [sys.executable, APP,
                 os.path.join(SAMPLES, "mapping_union.json"), "-o", out],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out, "r", encoding="utf-8") as fh:
                report = json.load(fh)
            self.assertEqual(report["summary"]["total_affected_pages"], 1)


if __name__ == "__main__":
    unittest.main()
