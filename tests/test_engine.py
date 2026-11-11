"""端到端裁决测试：覆盖题目要求的全部场景与公告更新前后变化。"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from adjudicator.engine import Adjudicator, ChangeEntry, summarize
from adjudicator.loader import load_advisories, load_boms, load_records
from adjudicator.models import (
    DISPOSITION_AFFECTED,
    DISPOSITION_FIXED,
    DISPOSITION_NEEDS_VERIFICATION,
    DISPOSITION_NOT_AFFECTED,
)
from adjudicator.report import build_report, render_json, render_text

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


class EngineFixture:
    def __init__(self, advisory_file: str):
        self.boms = load_boms(DATA / "boms.json")
        self.advisories = load_advisories(DATA / advisory_file)
        self.records = load_records(DATA / "applicability-records.json")
        self.engine = Adjudicator(self.boms, self.advisories, self.records)
        self.findings = self.engine.adjudicate_all()

    def finding(self, bom_ref: str, vuln_id: str):
        matches = [
            f for f in self.findings
            if f.bom_ref == bom_ref and f.vuln_id == vuln_id
        ]
        assert len(matches) == 1, (
            f"{bom_ref}/{vuln_id} 应恰好有一条裁决，实际 {len(matches)} 条"
        )
        return matches[0]


class SameNameDifferentVariantTest(unittest.TestCase):
    """同名异生态 / 同包并存两个版本：证据不得串用。"""

    def setUp(self):
        self.current = EngineFixture("advisories-current.json")

    def test_same_name_different_ecosystem_are_separate(self):
        # npm:acme/widget 命中公告；pypi:acme/widget 无任何公告，不产漏洞条目
        widget = self.current.finding("A-WIDGET", "CVE-2026-1002")
        self.assertEqual(widget.disposition, DISPOSITION_AFFECTED)
        self.assertIn("ADV-2026-1002", widget.evidence)
        py_widget_entries = [
            f for f in self.current.findings if f.bom_ref == "B-WIDGET-PY"
        ]
        self.assertEqual(py_widget_entries, [])

    def test_two_versions_of_same_package_coexist(self):
        old = self.current.finding("A-LIBZ-OLD", "CVE-2026-1001")
        new = self.current.finding("A-LIBZ-NEW", "CVE-2026-1001")
        self.assertEqual(old.disposition, DISPOSITION_AFFECTED)
        self.assertEqual(new.disposition, DISPOSITION_AFFECTED)
        # 两条裁决各自独立，证据相同但组件 ref / 版本不同
        self.assertNotEqual(old.bom_ref, new.bom_ref)

    def test_headline_platform_mislabel_fixed_to_affected(self):
        """题目主场景：更新后区间扩大，beta 版 1.6.0 从“已修复”翻回“受影响”。"""
        baseline = EngineFixture("advisories-baseline.json")
        before = baseline.finding("B-LIBZ", "CVE-2026-1001")
        after = self.current.finding("B-LIBZ", "CVE-2026-1001")
        self.assertEqual(before.disposition, DISPOSITION_FIXED)
        self.assertEqual(after.disposition, DISPOSITION_AFFECTED)
        # B-LIBZ 没有任何适用性记录，结论完全由公告元数据决定
        self.assertEqual(
            [e for e in after.evidence if e.startswith("REC-")], []
        )


class IntervalEndpointTest(unittest.TestCase):
    def setUp(self):
        self.current = EngineFixture("advisories-current.json")

    def test_open_upper_endpoint_excludes_fix_version(self):
        # [1.2.0, 1.2.7)：1.2.0 受影响，1.2.7 落在区间外
        old = self.current.finding("A-UTIL-OLD", "CVE-2026-1003")
        new = self.current.finding("A-UTIL-NEW", "CVE-2026-1003")
        self.assertEqual(old.disposition, DISPOSITION_AFFECTED)
        self.assertEqual(new.disposition, DISPOSITION_FIXED)

    def test_closed_upper_endpoint_includes_boundary(self):
        # [1.2.7, 1.2.10]：闭区间上界 1.2.10 仍受影响
        included = self.current.finding("A-UTIL-NEW", "CVE-2026-1004")
        boundary = self.current.finding("B-UTIL", "CVE-2026-1004")
        self.assertEqual(included.disposition, DISPOSITION_AFFECTED)
        self.assertEqual(boundary.disposition, DISPOSITION_AFFECTED)

    def test_unbounded_lower_with_open_upper_and_prerelease(self):
        # (-inf, 2.0.0)：预发布版在界内受影响，正式版在界外已修复
        pre = self.current.finding("A-EDGE-PRE", "CVE-2026-1005")
        release = self.current.finding("A-EDGE-RC", "CVE-2026-1005")
        self.assertEqual(pre.disposition, DISPOSITION_AFFECTED)
        self.assertEqual(release.disposition, DISPOSITION_FIXED)
        beta = self.current.finding("B-EDGE", "CVE-2026-1005")
        self.assertEqual(beta.disposition, DISPOSITION_FIXED)

    def test_fully_unbounded_affects_everything(self):
        legacy = self.current.finding("A-LEGACY", "CVE-2026-1006")
        self.assertEqual(legacy.disposition, DISPOSITION_AFFECTED)


class StaleRecordTest(unittest.TestCase):
    def setUp(self):
        self.current = EngineFixture("advisories-current.json")

    def test_stale_not_affected_record_is_not_evidence(self):
        finding = self.current.finding("A-LIBZ-NEW", "CVE-2026-1001")
        self.assertEqual(finding.disposition, DISPOSITION_AFFECTED)
        self.assertNotIn("REC-3001", finding.evidence)
        self.assertTrue(
            any("record_stale:REC-3001" in r for r in finding.reasons)
        )

    def test_fresh_record_corroborates_advisory(self):
        finding = self.current.finding("A-SAFE", "CVE-2026-1011")
        self.assertEqual(finding.disposition, DISPOSITION_NOT_AFFECTED)
        self.assertIn("ADV-2026-1011", finding.evidence)
        self.assertIn("REC-3002", finding.evidence)


class WithdrawnReopenTest(unittest.TestCase):
    def test_withdrawn_then_reopened_advisory(self):
        baseline = EngineFixture("advisories-baseline.json")
        current = EngineFixture("advisories-current.json")

        for ref in ("A-GHOST", "B-GHOST"):
            base_entries = [f for f in baseline.findings if f.bom_ref == ref]
            self.assertEqual(len(base_entries), 1)
            base = base_entries[0]
            # 撤回状态下：无有效证据 => 待核实，且保留撤回原因
            self.assertEqual(base.disposition, DISPOSITION_NEEDS_VERIFICATION)
            self.assertTrue(
                any("advisory_withdrawn:ADV-2026-1009" in r
                    for r in base.reasons)
            )

            reopened = current.finding(ref, "CVE-2026-1009")
            self.assertEqual(reopened.disposition, DISPOSITION_AFFECTED)
            self.assertIn("ADV-2026-1009", reopened.evidence)

    def test_change_report_flags_reopen_and_revision(self):
        baseline = EngineFixture("advisories-baseline.json")
        current = EngineFixture("advisories-current.json")
        changes = current.engine.diff(
            baseline.findings, current.findings,
            baseline_advisories=baseline.advisories,
            current_advisories=current.advisories,
        )
        index = {(c.key[1], c.vuln_id): c for c in changes}

        ghost = index[("A-GHOST", "CVE-2026-1009")]
        self.assertEqual(ghost.before, DISPOSITION_NEEDS_VERIFICATION)
        self.assertEqual(ghost.after, DISPOSITION_AFFECTED)
        self.assertTrue(any("advisory_reopened:ADV-2026-1009" in r
                            for r in ghost.reasons))

        libz = index[("B-LIBZ", "CVE-2026-1001")]
        self.assertEqual(libz.before, DISPOSITION_FIXED)
        self.assertEqual(libz.after, DISPOSITION_AFFECTED)
        self.assertTrue(any("advisory_revised:ADV-2026-1001" in r
                            for r in libz.reasons))

    def test_change_report_is_deterministic_across_runs(self):
        def compute():
            baseline = EngineFixture("advisories-baseline.json")
            current = EngineFixture("advisories-current.json")
            return current.engine.diff(
                baseline.findings, current.findings,
                baseline_advisories=baseline.advisories,
                current_advisories=current.advisories,
            )
        first, second = compute(), compute()
        self.assertEqual([c.key for c in first], [c.key for c in second])
        self.assertEqual(
            [r for c in first for r in c.reasons],
            [r for c in second for r in c.reasons],
        )


class ConflictAndIncomparableTest(unittest.TestCase):
    def setUp(self):
        self.current = EngineFixture("advisories-current.json")

    def test_contradictory_advisories_keep_conflict_reason(self):
        finding = self.current.finding("A-CONTRA", "CVE-2026-1007")
        self.assertEqual(finding.disposition, DISPOSITION_NEEDS_VERIFICATION)
        self.assertIn("ADV-2026-1007A", finding.evidence)
        self.assertIn("ADV-2026-1007B", finding.evidence)
        self.assertTrue(
            any(r.startswith("conflicting_evidence:") for r in finding.reasons)
        )

    def test_unparseable_component_version_is_verification(self):
        finding = self.current.finding("A-WEIRD", "CVE-2026-1008")
        self.assertEqual(finding.disposition, DISPOSITION_NEEDS_VERIFICATION)
        reasons = " ".join(finding.reasons)
        self.assertIn("version_not_comparable", reasons)
        self.assertIn("unparseable_component_version:20261109-r1", reasons)
        # 畸形区间端点同样要留下原因
        self.assertTrue(
            any("unparseable_interval_endpoint" in r for r in finding.reasons)
        )


class ReportAndCliTest(unittest.TestCase):
    def test_summary_covers_four_dispositions(self):
        current = EngineFixture("advisories-current.json")
        counts = summarize(current.findings)
        for disposition in (DISPOSITION_AFFECTED, DISPOSITION_NOT_AFFECTED,
                            DISPOSITION_FIXED, DISPOSITION_NEEDS_VERIFICATION):
            self.assertGreaterEqual(counts[disposition], 1)
        self.assertEqual(sum(counts.values()), len(current.findings))

    def test_report_json_is_valid_and_serializable(self):
        current = EngineFixture("advisories-current.json")
        report = build_report(current.boms, current.findings)
        parsed = json.loads(render_json(report))
        self.assertEqual(parsed["report_version"], 1)
        self.assertIn("component_rollup", parsed)
        # 文本报告包含四种中文处置名
        text = render_text(report)
        for label in ("受影响", "不受影响", "已修复", "待核实"):
            self.assertIn(label, text)

    def test_cli_end_to_end_with_change_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_out = Path(tmp) / "report.json"
            text_out = Path(tmp) / "report.txt"
            cmd = [
                sys.executable, "-I", str(ROOT / "run_adjudicator.py"),
                "--boms", str(DATA / "boms.json"),
                "--advisories", str(DATA / "advisories-current.json"),
                "--records", str(DATA / "applicability-records.json"),
                "--baseline-advisories", str(DATA / "advisories-baseline.json"),
                "--json-out", str(json_out),
                "--text-out", str(text_out),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  cwd=tempfile.gettempdir(), check=True)
            report = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertIn("change_report", report)
            self.assertGreater(report["change_report"]["change_count"], 0)
            self.assertIn("公告更新前后变化报告", text_out.read_text(encoding="utf-8"))
            self.assertIn("裁决完成", proc.stdout)

    def test_rerun_produces_identical_output(self):
        current = EngineFixture("advisories-current.json")
        report1 = render_json(build_report(current.boms, current.findings))
        rerun = EngineFixture("advisories-current.json")
        report2 = render_json(build_report(rerun.boms, rerun.findings))
        self.assertEqual(report1, report2)

    def test_missing_input_file_exits_cleanly(self):
        cmd = [
            sys.executable, "-I", str(ROOT / "run_adjudicator.py"),
            "--boms", str(DATA / "does-not-exist.json"),
            "--advisories", str(DATA / "advisories-current.json"),
            "--records", str(DATA / "applicability-records.json"),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("裁决器输入错误", proc.stderr)


if __name__ == "__main__":
    unittest.main()
