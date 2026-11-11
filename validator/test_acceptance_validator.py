#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""acceptance_validator 的单元测试与端到端 CLI 测试。

运行方式（仓库根目录）：
    python3 -m unittest discover -s validator -v
或直接：
    python3 validator/test_acceptance_validator.py -v
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import acceptance_validator as av  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
SCRIPT = ROOT / "validator" / "acceptance_validator.py"

SNAP = "snap-7f3a9c21"
RULE = "rules-v2.4.0"
OLD_SNAP = "snap-5b1d8e07"
OLD_RULE = "rules-v2.3.1"


def _manifest(snapshot_id=SNAP, rule_version=RULE, reqs=None):
    reqs = reqs or [
        ("RISK-001", "t_sql", "obj-a.py"),
        ("RISK-002", "t_auth", "obj-b.py"),
        ("RISK-003", "t_redact", "obj-c.py"),
        ("RISK-004", "t_pickle", "obj-d.py"),
        ("RISK-005", "t_replay", "obj-e.py"),
        ("RISK-006", "t_upload", "obj-f.py"),
        ("RISK-007", "t_same_name", "obj-g.py"),
    ]
    return {
        "manifest_id": "MF-TEST",
        "snapshot_id": snapshot_id,
        "rule_version": rule_version,
        "requirements": [
            {
                "risk_id": rid,
                "title": f"title-{rid}",
                "assertions": [{"test_name": tname, "object": obj}],
            }
            for rid, tname, obj in reqs
        ],
    }


def _rec(rid, tname, obj, status, snapshot_id=SNAP, rule_version=RULE):
    return {
        "result_id": rid,
        "run_id": "run-1",
        "test_name": tname,
        "object": obj,
        "snapshot_id": snapshot_id,
        "rule_version": rule_version,
        "status": status,
        "recorded_at": "2026-11-12T10:00:00Z",
    }


def _verdict_map(report):
    return {(it["test_name"], it["object"]): it["verdict"]
            for it in report["items"]}


class EvaluateStatusTests(unittest.TestCase):
    """五类异常状态 + 成功状态的判定。"""

    def test_all_five_unmet_states_plus_passed(self):
        manifest = _manifest()
        results = [
            _rec("R1", "t_sql", "obj-a.py", "passed"),
            _rec("R2", "t_auth", "obj-b.py", "skipped"),
            _rec("R3", "t_redact", "obj-c.py", "failed"),
            _rec("R4", "t_pickle", "obj-d.py", "not_run"),
            # t_replay / obj-e.py 完全无记录 -> NOT_EXECUTED
            # t_upload / obj-f.py 只有旧快照通过 -> STALE
            _rec("R6", "t_upload", "obj-f.py", "passed",
                 snapshot_id=OLD_SNAP, rule_version=OLD_RULE),
        ]
        report = av.evaluate(manifest, results)
        v = _verdict_map(report)
        self.assertEqual(v[("t_sql", "obj-a.py")], av.VERDICT_SATISFIED)
        self.assertEqual(v[("t_auth", "obj-b.py")], av.VERDICT_SKIPPED)
        self.assertEqual(v[("t_redact", "obj-c.py")], av.VERDICT_FAILED)
        self.assertEqual(v[("t_pickle", "obj-d.py")], av.VERDICT_NOT_EXECUTED)
        self.assertEqual(v[("t_replay", "obj-e.py")], av.VERDICT_NOT_EXECUTED)
        self.assertEqual(v[("t_upload", "obj-f.py")], av.VERDICT_STALE)
        self.assertFalse(report["summary"]["accepted"])
        self.assertEqual(report["summary"]["satisfied"], 1)
        self.assertEqual(report["summary"]["unmet"], 6)

    def test_skipped_is_never_treated_as_passed(self):
        """看板把 skipped 当通过；校验器必须拒绝。"""
        manifest = _manifest(reqs=[("R1", "t_auth", "obj-b.py")])
        results = [_rec("R1", "t_auth", "obj-b.py", "skipped")]
        report = av.evaluate(manifest, results)
        item = report["items"][0]
        self.assertEqual(item["verdict"], av.VERDICT_SKIPPED)
        self.assertIn("跳过", item["verdict_text"])
        # skipped 记录作为证据来源保留，但绝不计为满足
        self.assertEqual(len(item["exact_evidence"]), 1)
        self.assertEqual(item["exact_evidence"][0]["status"], "skipped")
        self.assertFalse(item["satisfied"])
        self.assertFalse(report["summary"]["accepted"])

    def test_stale_variants_snapshot_rule_both(self):
        manifest = _manifest(reqs=[
            ("R1", "t_pickle", "obj-d.py"),
            ("R2", "t_upload", "obj-f.py"),
            ("R3", "t_redact", "obj-c.py"),
        ])
        results = [
            # 仅快照过期
            _rec("R1", "t_pickle", "obj-d.py", "passed",
                 snapshot_id=OLD_SNAP),
            # 仅规则版本过期
            _rec("R2", "t_upload", "obj-f.py", "passed",
                 rule_version=OLD_RULE),
            # 两者都过期
            _rec("R3a", "t_redact", "obj-c.py", "passed",
                 snapshot_id=OLD_SNAP, rule_version=OLD_RULE),
            _rec("R3b", "t_redact", "obj-c.py", "failed",
                 snapshot_id=OLD_SNAP, rule_version=OLD_RULE),
        ]
        report = av.evaluate(manifest, results)
        v = _verdict_map(report)
        self.assertTrue(all(x == av.VERDICT_STALE for x in v.values()))
        stale_reasons = {
            (it["test_name"], it["object"]):
                [e["stale_reason"] for e in it["stale_evidence"]]
            for it in report["items"]
        }
        self.assertEqual(stale_reasons[("t_pickle", "obj-d.py")], ["snapshot"])
        self.assertEqual(stale_reasons[("t_upload", "obj-f.py")], ["rule"])
        self.assertCountEqual(
            stale_reasons[("t_redact", "obj-c.py")],
            ["snapshot+rule", "snapshot+rule"])
        # 旧快照的通过结果不得进入 exact_evidence
        for it in report["items"]:
            self.assertEqual(it["exact_evidence"], [])

    def test_conflicting_duplicates_detected(self):
        manifest = _manifest(reqs=[("R1", "t_replay", "obj-e.py")])
        results = [
            _rec("R1", "t_replay", "obj-e.py", "passed"),
            _rec("R2", "t_replay", "obj-e.py", "failed"),
        ]
        report = av.evaluate(manifest, results)
        self.assertEqual(report["items"][0]["verdict"],
                         av.VERDICT_CONFLICT)
        self.assertEqual(report["summary"]["conflicts"], 1)
        self.assertFalse(report["summary"]["accepted"])

    def test_identical_duplicate_passes_are_consistent(self):
        manifest = _manifest(reqs=[("R1", "t_sql", "obj-a.py")])
        results = [
            _rec("R1", "t_sql", "obj-a.py", "passed"),
            _rec("R2", "t_sql", "obj-a.py", "passed"),
        ]
        report = av.evaluate(manifest, results)
        self.assertEqual(report["items"][0]["verdict"], av.VERDICT_SATISFIED)
        self.assertEqual(len(report["items"][0]["exact_evidence"]), 2)
        self.assertTrue(report["summary"]["accepted"])

    def test_conflict_distinguishes_skipped_from_passed(self):
        manifest = _manifest(reqs=[("R1", "t_sql", "obj-a.py")])
        results = [
            _rec("R1", "t_sql", "obj-a.py", "passed"),
            _rec("R2", "t_sql", "obj-a.py", "skipped"),
        ]
        report = av.evaluate(manifest, results)
        self.assertEqual(report["items"][0]["verdict"],
                         av.VERDICT_CONFLICT)

    def test_stale_records_are_listed_but_never_accepted(self):
        """旧快照通过结果不会填补当前缺口（用户指定确认之二）。"""
        manifest = _manifest(reqs=[("R1", "t_upload", "obj-f.py")])
        results = [
            _rec("OLD", "t_upload", "obj-f.py", "passed",
                 snapshot_id=OLD_SNAP, rule_version=OLD_RULE),
        ]
        report = av.evaluate(manifest, results)
        it = report["items"][0]
        self.assertEqual(it["verdict"], av.VERDICT_STALE)
        self.assertEqual(len(it["stale_evidence"]), 1)
        self.assertEqual(it["stale_evidence"][0]["result_id"], "OLD")
        self.assertEqual(it["exact_evidence"], [])
        pending_ids = [p["test_name"] for p in report["pending"]]
        self.assertIn("t_upload", pending_ids)


class ObjectScopingTests(unittest.TestCase):
    """相同测试名覆盖不同对象时，证据按对象隔离（用户指定确认之一）。"""

    def test_same_test_name_different_objects_do_not_cross_fill(self):
        manifest = _manifest(reqs=[
            ("R1", "t_sql", "obj-a.py"),
            ("R2", "t_sql", "obj-b.py"),
        ])
        results = [
            _rec("R1", "t_sql", "obj-a.py", "passed"),
            # obj-b.py 同名测试只有 skipped
            _rec("R2", "t_sql", "obj-b.py", "skipped"),
        ]
        report = av.evaluate(manifest, results)
        v = _verdict_map(report)
        self.assertEqual(v[("t_sql", "obj-a.py")], av.VERDICT_SATISFIED)
        self.assertEqual(v[("t_sql", "obj-b.py")], av.VERDICT_SKIPPED)
        self.assertFalse(report["summary"]["accepted"])

    def test_passed_on_different_object_does_not_satisfy(self):
        manifest = _manifest(reqs=[("R1", "t_sql", "obj-a.py")])
        results = [
            _rec("R1", "t_sql", "obj-OTHER.py", "passed"),
        ]
        report = av.evaluate(manifest, results)
        self.assertEqual(report["items"][0]["verdict"],
                         av.VERDICT_NOT_EXECUTED)
        self.assertEqual(report["items"][0]["stale_evidence"], [])


class ExampleDataTests(unittest.TestCase):
    """对 examples/ 合成数据的端到端判定。"""

    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(
            (EXAMPLES / "fix_manifest.json").read_text(encoding="utf-8"))
        cls.gaps = json.loads(
            (EXAMPLES / "results_with_gaps.json").read_text(encoding="utf-8"))
        cls.complete = json.loads(
            (EXAMPLES / "results_complete.json").read_text(encoding="utf-8"))

    def test_gaps_file_covers_every_unmet_verdict(self):
        report = av.evaluate(self.manifest, self.gaps)
        v = _verdict_map(report)
        # RISK-001：当前通过
        self.assertEqual(
            v[("test_sql_injection_blocked", "svc-order/dao/order_query.py")],
            av.VERDICT_SATISFIED)
        # RISK-002：跳过
        self.assertEqual(
            v[("test_horizontal_privilege_denied",
               "svc-billing/api/invoice.py")],
            av.VERDICT_SKIPPED)
        # RISK-003：失败
        self.assertEqual(
            v[("test_secret_redaction",
               "lib-common/logging/redactor.py")],
            av.VERDICT_FAILED)
        # RISK-004：两条过期证据（snapshot / rule 各一）
        pickle = next(
            it for it in report["items"]
            if it["test_name"] == "test_pickle_rejected")
        self.assertEqual(pickle["verdict"], av.VERDICT_STALE)
        self.assertCountEqual(
            [e["stale_reason"] for e in pickle["stale_evidence"]],
            ["snapshot", "rule"])
        # RISK-005：重复结果冲突 + 另一条 not_run
        replay = next(
            it for it in report["items"]
            if it["test_name"] == "test_replay_callback_rejected")
        self.assertEqual(replay["verdict"], av.VERDICT_CONFLICT)
        idem = next(
            it for it in report["items"]
            if it["test_name"] == "test_idempotency_key_enforced")
        self.assertEqual(idem["verdict"], av.VERDICT_NOT_EXECUTED)
        # RISK-006：旧快照+旧规则通过，不能填补缺口
        upload = next(
            it for it in report["items"]
            if it["test_name"] == "test_upload_magic_byte_check")
        self.assertEqual(upload["verdict"], av.VERDICT_STALE)
        # 同名测试在别的对象上通过，不影响 RISK-002 的缺口
        self.assertEqual(report["summary"]["satisfied"], 1)
        self.assertEqual(report["summary"]["unmet"], 6)
        self.assertFalse(report["summary"]["accepted"])
        # 待补清单全部带证据来源
        for p in report["pending"]:
            self.assertIn("verdict", p)
            self.assertIsInstance(p["exact_evidence"], list)
            self.assertIsInstance(p["stale_evidence"], list)

    def test_complete_evidence_passes(self):
        """完整证据能够通过验收（用户指定确认之三）。"""
        report = av.evaluate(self.manifest, self.complete)
        self.assertTrue(report["summary"]["accepted"])
        self.assertEqual(report["summary"]["unmet"], 0)
        self.assertEqual(report["summary"]["satisfied"], 7)
        self.assertTrue(all(it["satisfied"] for it in report["items"]))
        # 旧记录仍被标记为过期证据，但不影响结论
        pickle = next(
            it for it in report["items"]
            if it["test_name"] == "test_pickle_rejected")
        self.assertEqual(len(pickle["stale_evidence"]), 1)
        self.assertEqual(pickle["exact_evidence"][0]["status"], "passed")
        # 重复的一致通过记录被完整采信
        sql = next(
            it for it in report["items"]
            if it["object"] == "svc-order/dao/order_query.py")
        self.assertEqual(len(sql["exact_evidence"]), 2)

    def test_rendered_text_report_contains_key_sections(self):
        report = av.evaluate(self.manifest, self.gaps)
        text = av.render_text(report)
        self.assertIn("修复验收报告（离线校验）", text)
        self.assertIn("待补测试", text)
        self.assertIn("证据来源", text)
        self.assertIn("被拒绝的过期证据", text)
        self.assertIn("验收结论: 不通过", text)
        text_ok = av.render_text(av.evaluate(self.manifest, self.complete))
        self.assertIn("验收结论: 通过", text_ok)


class InputValidationTests(unittest.TestCase):
    def test_unknown_status_rejected(self):
        with self.assertRaises(av.InputError):
            av.evaluate(_manifest(),
                        [_rec("R1", "t_sql", "obj-a.py", "PASS")])

    def test_duplicate_result_id_rejected(self):
        with self.assertRaises(av.InputError):
            av.evaluate(
                _manifest(reqs=[("R1", "t_sql", "obj-a.py")]),
                [_rec("DUP", "t_sql", "obj-a.py", "passed"),
                 _rec("DUP", "t_sql", "obj-a.py", "passed")])

    def test_missing_field_rejected(self):
        bad = {"result_id": "R1", "test_name": "t", "object": "o",
               "snapshot_id": SNAP, "rule_version": RULE}  # 缺 status
        with self.assertRaises(av.InputError):
            av.evaluate(_manifest(reqs=[("R1", "t", "o")]), [bad])

    def test_empty_requirements_rejected(self):
        with self.assertRaises(av.InputError):
            av.evaluate({"manifest_id": "m", "snapshot_id": SNAP,
                         "rule_version": RULE, "requirements": []}, [])

    def test_results_wrapper_object_accepted(self):
        report = av.evaluate(
            _manifest(reqs=[("R1", "t_sql", "obj-a.py")]),
            {"results": [_rec("R1", "t_sql", "obj-a.py", "passed")]})
        self.assertTrue(report["summary"]["accepted"])


class CliTests(unittest.TestCase):
    def _run(self, *extra_args):
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), *extra_args],
            capture_output=True, text=True, cwd=str(ROOT))

    def test_exit_1_for_gaps_and_report_content(self):
        proc = self._run(
            str(EXAMPLES / "fix_manifest.json"),
            str(EXAMPLES / "results_with_gaps.json"))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("待补测试", proc.stdout)
        self.assertIn("SKIPPED", proc.stdout)
        self.assertIn("CONFLICTING_DUPLICATES", proc.stdout)
        self.assertIn("STALE_EVIDENCE", proc.stdout)

    def test_exit_0_for_complete_evidence(self):
        proc = self._run(
            str(EXAMPLES / "fix_manifest.json"),
            str(EXAMPLES / "results_complete.json"),
            "--format", "json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["summary"]["accepted"])

    def test_exit_2_for_missing_file(self):
        proc = self._run("no-such-manifest.json",
                         str(EXAMPLES / "results_complete.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("输入错误", proc.stderr)

    def test_exit_2_for_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not valid json", encoding="utf-8")
            proc = self._run(str(bad),
                             str(EXAMPLES / "results_complete.json"))
            self.assertEqual(proc.returncode, 2)

    def test_report_file_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.txt"
            proc = self._run(
                str(EXAMPLES / "fix_manifest.json"),
                str(EXAMPLES / "results_complete.json"),
                "--report", str(out))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("验收结论: 通过", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
