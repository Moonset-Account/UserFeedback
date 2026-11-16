import json
import tempfile
import unittest
from pathlib import Path

from src.baseline import load_baseline, write_baseline
from src.scanner import Finding, ScanResult, SecretScanner


class V4CrossCategoryBaselineTests(unittest.TestCase):
    def finding(self, kind, rule, category):
        return Finding(
            type=kind, pattern_name=rule, severity="high", file="src/app.py", line=7,
            matched_text=f"stable-{category}", line_content="synthetic fixture",
            rule=rule, rule_id=rule, category=category, confidence="high",
            remediation="Review synthetic fixture.",
        )

    def test_fingerprints_suppress_secret_and_policy_categories(self):
        findings = [
            self.finding("secret", "CRT-SEC-101", "secret"),
            self.finding("policy", "CRT-CFG-201", "config_policy"),
            self.finding("policy", "CRT-POL-301", "ai_policy"),
        ]
        scanner = SecretScanner()
        scanner.baseline_fingerprints = {item.fingerprint for item in findings}
        result = ScanResult("scanner", "4", "now", "diff", "memory", findings=list(findings))
        filtered = scanner._apply_baseline(result)
        self.assertEqual([], filtered.findings)
        self.assertEqual(3, filtered.baseline_suppressed)
        self.assertEqual(3, filtered.baseline_matched)
        self.assertEqual(0, filtered.baseline_stale)

    def test_baseline_serializes_only_fingerprints_not_evidence(self):
        findings = [self.finding("secret", "CRT-SEC-101", "secret"), self.finding("policy", "CRT-CFG-201", "config_policy")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            write_baseline(str(path), [item.fingerprint for item in findings])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual({"schema", "version", "fingerprints"}, set(data))
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("stable-secret", text)
            self.assertNotIn("synthetic fixture", text)
            self.assertEqual({item.fingerprint for item in findings}, load_baseline(str(path)))


if __name__ == "__main__":
    unittest.main()
