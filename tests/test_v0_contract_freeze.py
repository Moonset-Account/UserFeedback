"""Stage V0 characterization tests for contracts that vulnerability work must preserve."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from src.baseline import load_baseline, write_baseline
from src.scanner import ConfigChange, Finding, ScanResult


class TestV0ContractFreeze(unittest.TestCase):
    def make_finding(self, **overrides):
        values = {
            "type": "secret",
            "pattern_name": "FIXTURE_SECRET",
            "severity": "high",
            "file": "src/app.py",
            "line": 7,
            "matched_text": "token=synthetic-value",
            "line_content": "token=synthetic-value",
            "rule": "synthetic fixture",
            "rule_id": "CRT-SEC-FIXTURE",
            "category": "secret",
            "confidence": "high",
            "remediation": "rotate the credential",
        }
        values.update(overrides)
        return Finding(**values)

    def make_result(self, findings=None, config_changes=None):
        return ScanResult(
            scanner="secret-config-diff-scanner",
            version="3.0.1",
            timestamp="2026-01-01T00:00:00+00:00",
            input_type="diff",
            input_source="fixture.diff",
            findings=list(findings or []),
            config_changes=list(config_changes or []),
        )

    def test_finding_fingerprint_namespace_and_formula_are_frozen(self):
        finding = self.make_finding()
        expected_payload = "\0".join(
            (
                "coderisktools-finding-v1",
                "CRT-SEC-FIXTURE",
                "src/app.py",
                "token=synthetic-value",
            )
        )
        expected = "sha256:" + hashlib.sha256(expected_payload.encode("utf-8")).hexdigest()
        self.assertEqual(finding.fingerprint, expected)
        self.assertEqual(finding.fingerprint, self.make_finding().fingerprint)

    def test_json_redacts_secret_evidence(self):
        raw = self.make_result([self.make_finding()]).to_json()
        data = json.loads(raw)
        item = data["findings"][0]
        self.assertEqual(item["matched_text"], "[REDACTED]")
        self.assertEqual(item["line_content"], "[REDACTED]")
        self.assertNotIn("synthetic-value", raw)

    def test_scan_result_exit_codes_remain_separate(self):
        self.assertEqual(self.make_result().exit_code, 0)
        self.assertEqual(self.make_result([self.make_finding()]).exit_code, 1)
        config = ConfigChange("config", "config.yml", "medium", "modified", "fixture")
        self.assertEqual(self.make_result(config_changes=[config]).exit_code, 2)

    def test_baseline_schema_and_round_trip_remain_versioned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.json"
            fingerprint = self.make_finding().fingerprint
            write_baseline(str(path), [fingerprint])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(data), {"schema", "version", "fingerprints"})
            self.assertEqual(data["schema"], "coderisktools.scanner.baseline")
            self.assertEqual(data["version"], 1)
            self.assertEqual(load_baseline(str(path)), {fingerprint})

    def test_sarif_version_and_secret_safe_message_remain_stable(self):
        raw = self.make_result([self.make_finding()]).to_sarif()
        data = json.loads(raw)
        self.assertEqual(data["version"], "2.1.0")
        self.assertNotIn("synthetic-value", raw)
        result = data["runs"][0]["results"][0]
        self.assertEqual(result["properties"]["fingerprint"], self.make_finding().fingerprint)


if __name__ == "__main__":
    unittest.main()
