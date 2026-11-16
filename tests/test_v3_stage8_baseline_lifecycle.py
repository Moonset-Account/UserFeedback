import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.baseline import write_baseline
from src.scanner import Finding, ScanResult, SecretScanner


class Stage8BaselineLifecycleTests(unittest.TestCase):
    def finding(self, rule_id="CRT-SEC-001", evidence="secret-value", line=1):
        return Finding("secret", "TEST", "high", "src/app.py", line, evidence, evidence,
                       "secret-pattern", rule_id)

    def test_writer_is_sorted_unique_deterministic_and_secret_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp, "first.json")
            second = Path(tmp, "second.json")
            a = self.finding().fingerprint
            b = self.finding(rule_id="CRT-SEC-002", evidence="other-secret").fingerprint
            write_baseline(str(first), [b, a, b])
            write_baseline(str(second), [a, b])
            self.assertEqual(first.read_bytes(), second.read_bytes())
            data = json.loads(first.read_text(encoding="utf-8"))
            self.assertEqual(data["fingerprints"], sorted({a, b}))
            text = first.read_text(encoding="utf-8")
            self.assertTrue(text.endswith("\n"))
            self.assertNotIn("secret-value", text)
            self.assertNotIn("other-secret", text)

    def test_writer_refuses_missing_parent_existing_file_directory_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fp = self.finding().fingerprint
            with self.assertRaises(FileNotFoundError):
                write_baseline(str(root / "missing" / "baseline.json"), [fp])
            existing = root / "existing.json"; existing.write_text("keep", encoding="utf-8")
            with self.assertRaises(FileExistsError): write_baseline(str(existing), [fp])
            self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
            directory = root / "directory"; directory.mkdir()
            with self.assertRaises((ValueError, FileExistsError)): write_baseline(str(directory), [fp], overwrite=True)
            target = root / "target"; target.write_text("keep", encoding="utf-8")
            link = root / "link.json"; link.symlink_to(target)
            with self.assertRaises(ValueError): write_baseline(str(link), [fp], overwrite=True)
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_force_replaces_regular_file_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp, "baseline.json")
            output.write_text("old", encoding="utf-8")
            fp = self.finding().fingerprint
            write_baseline(str(output), [fp], overwrite=True)
            self.assertEqual(json.loads(output.read_text())["fingerprints"], [fp])
            leftovers = [p for p in output.parent.iterdir() if p.name != output.name]
            self.assertEqual(leftovers, [])

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "src", *args],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_cli_creates_refuses_then_force_replaces_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, "app.py")
            source.write_text('password = "stage8_password_value_123"\n', encoding="utf-8")
            baseline = Path(tmp, "baseline.json")
            args = ("scan", "--dir", tmp, "--severity-threshold", "low",
                    "--write-baseline", str(baseline), "--format", "json")
            created = self.run_cli(*args)
            self.assertEqual(created.returncode, 1, created.stderr)
            original = baseline.read_bytes()
            refused = self.run_cli(*args)
            self.assertEqual(refused.returncode, 3)
            self.assertEqual(baseline.read_bytes(), original)
            forced = self.run_cli(*args, "--force-baseline")
            self.assertEqual(forced.returncode, 1, forced.stderr)
            self.assertEqual(baseline.read_bytes(), original)

    def test_cli_rejects_baseline_write_and_output_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, "app.py")
            source.write_text('password = "stage8_password_value_123"\n', encoding="utf-8")
            baseline = Path(tmp, "baseline.json")
            write_baseline(str(baseline), [])
            both = self.run_cli("scan", "--dir", tmp, "--baseline", str(baseline),
                                "--write-baseline", str(Path(tmp, "new.json")))
            self.assertEqual(both.returncode, 3)
            collision = Path(tmp, "collision.json")
            same = self.run_cli("scan", "--dir", tmp, "--output", str(collision),
                                "--write-baseline", str(collision))
            self.assertEqual(same.returncode, 3)
            self.assertFalse(collision.exists())
            force_only = self.run_cli("scan", "--dir", tmp, "--force-baseline")
            self.assertEqual(force_only.returncode, 3)

    def test_markdown_and_html_report_baseline_drift(self):
        result = ScanResult("secret-config-diff-scanner", "x", "t", "diff", "memory",
                            baseline_suppressed=4, baseline_total=3,
                            baseline_matched=2, baseline_stale=1)
        markdown = result.to_markdown()
        html = result.to_html()
        for label in ("Baseline total", "Baseline matched", "Baseline stale"):
            self.assertIn(label, markdown)
            self.assertIn(label, html)

    def test_drift_counts_unique_baseline_identities_and_suppressed_findings(self):
        one = self.finding()
        duplicate = self.finding(line=99)
        stale = self.finding(rule_id="CRT-SEC-099", evidence="stale").fingerprint
        result = ScanResult("secret-config-diff-scanner", "x", "t", "diff", "memory",
                            findings=[one, duplicate])
        scanner = SecretScanner()
        scanner.baseline_fingerprints = {one.fingerprint, stale}
        filtered = scanner._apply_baseline(result)
        self.assertEqual(filtered.findings, [])
        self.assertEqual(filtered.baseline_suppressed, 2)
        self.assertEqual(filtered.baseline_total, 2)
        self.assertEqual(filtered.baseline_matched, 1)
        self.assertEqual(filtered.baseline_stale, 1)
        self.assertEqual(filtered.summary["baseline_stale"], 1)


if __name__ == "__main__":
    unittest.main()
