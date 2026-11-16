import json
import tempfile
import unittest
from pathlib import Path

from src.baseline import load_baseline
from src.scanner import Finding, SecretScanner


class Stage7FingerprintBaselineTests(unittest.TestCase):
    def finding(self, **overrides):
        values = dict(type="secret", pattern_name="TOKEN", severity="high", file="src\\.\\app.py", line=7,
                      matched_text="token = super-secret-value", line_content="token = super-secret-value",
                      rule="secret-pattern", rule_id="CRT-SEC-999")
        values.update(overrides)
        return Finding(**values)

    def test_fingerprint_is_sha256_stable_across_line_and_path_separator(self):
        first = self.finding()
        second = self.finding(file="src/app.py", line=999, severity="critical")
        self.assertRegex(first.fingerprint, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_fingerprint_changes_for_rule_path_or_evidence(self):
        base = self.finding().fingerprint
        self.assertNotEqual(base, self.finding(rule_id="CRT-SEC-998").fingerprint)
        self.assertNotEqual(base, self.finding(file="src/other.py").fingerprint)
        self.assertNotEqual(base, self.finding(matched_text="token = other-value").fingerprint)

    def write_baseline(self, data):
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(data, handle); handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return handle.name

    def test_loader_accepts_exact_schema_and_rejects_malformed(self):
        fp = self.finding().fingerprint
        path = self.write_baseline({"schema": "coderisktools.scanner.baseline", "version": 1, "fingerprints": [fp]})
        self.assertEqual(load_baseline(path), {fp})
        invalid = [
            {},
            {"schema": "wrong", "version": 1, "fingerprints": []},
            {"schema": "coderisktools.scanner.baseline", "version": 2, "fingerprints": []},
            {"schema": "coderisktools.scanner.baseline", "version": 1, "fingerprints": ["bad"]},
            {"schema": "coderisktools.scanner.baseline", "version": 1, "fingerprints": [fp, fp]},
            {"schema": "coderisktools.scanner.baseline", "version": 1, "fingerprints": [], "extra": True},
        ]
        for data in invalid:
            with self.subTest(data=data):
                with self.assertRaises(ValueError): load_baseline(self.write_baseline(data))

    def test_baseline_suppresses_exact_finding_but_not_config_change(self):
        content = 'password = "hardcoded_password_123"\n'
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "app.py"; source.write_text(content, encoding="utf-8")
            raw = SecretScanner(severity_threshold="low").scan_directory(tmp, recursive=True)
            self.assertTrue(raw.findings)
            path = self.write_baseline({"schema": "coderisktools.scanner.baseline", "version": 1,
                                        "fingerprints": [raw.findings[0].fingerprint]})
            filtered = SecretScanner(severity_threshold="low", baseline_path=path).scan_directory(tmp, recursive=True)
            self.assertEqual(filtered.findings, [])
            self.assertEqual(len(filtered.config_changes), len(raw.config_changes))

    def test_directory_fingerprint_is_portable_across_roots(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            for root in (first, second):
                Path(root, "nested").mkdir()
                Path(root, "nested", "app.py").write_text('password = "portable_password_123"\n', encoding="utf-8")
            one = SecretScanner(severity_threshold="low").scan_directory(first, recursive=True)
            two = SecretScanner(severity_threshold="low").scan_directory(second, recursive=True)
            self.assertTrue(one.findings and two.findings)
            self.assertEqual(one.findings[0].fingerprint, two.findings[0].fingerprint)

    def test_baseline_keeps_new_finding_and_failing_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "app.py"
            source.write_text('password = "hardcoded_password_123"\n', encoding="utf-8")
            raw = SecretScanner(severity_threshold="low").scan_directory(tmp, recursive=True)
            self.assertTrue(raw.findings)
            baseline = self.write_baseline({"schema": "coderisktools.scanner.baseline", "version": 1,
                                            "fingerprints": [raw.findings[0].fingerprint]})
            source.write_text('password = "hardcoded_password_123"\napi_key = "new-secret-value-456789"\n', encoding="utf-8")
            filtered = SecretScanner(severity_threshold="low", baseline_path=baseline).scan_directory(tmp, recursive=True)
            self.assertEqual(filtered.baseline_suppressed, 1)
            self.assertTrue(filtered.findings)
            self.assertEqual(filtered.exit_code, 1)
            self.assertEqual(filtered.summary["baseline_suppressed"], 1)

    def test_missing_baseline_fails_closed(self):
        with self.assertRaises(FileNotFoundError):
            SecretScanner(baseline_path="/definitely/missing/baseline.json")

    def test_json_and_sarif_include_fingerprint_without_evidence(self):
        finding = self.finding()
        from src.scanner import ScanResult
        result = ScanResult("secret-config-diff-scanner", "x", "t", "diff", "memory", [finding])
        json_text = result.to_json(); sarif_text = result.to_sarif()
        self.assertIn(finding.fingerprint, json_text)
        self.assertIn(finding.fingerprint, sarif_text)
        self.assertNotIn("super-secret-value", json_text + sarif_text)


if __name__ == "__main__":
    unittest.main()
