import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.allowlist import MAX_ALLOWLIST_BYTES, load_allowlist
from src.config import MAX_CONFIG_BYTES, load_config
from src.safeio import write_private_atomic
from src.strict_diff import DiffParseError
from src.diff_parser import parse_diff
from src.scanner import MAX_DIFF_BYTES, MAX_LINE_CHARS, SecretScanner


class ScannerSecurityHardeningTests(unittest.TestCase):
    def test_traversal_and_absolute_paths_fail_closed(self):
        for path in ("../outside.py", "/tmp/outside.py"):
            diff = f"--- a/{path}\n+++ b/{path}\n@@ -0,0 +1,1 @@\n+x\n"
            with self.subTest(path=path), self.assertRaises(DiffParseError):
                parse_diff(diff)

    def test_malformed_nonempty_diff_fails_closed(self):
        with self.assertRaises(DiffParseError):
            parse_diff("+token = something\n")

    def test_oversized_diff_file_and_line_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "large.diff")
            path.write_bytes(b"x" * (MAX_DIFF_BYTES + 1))
            with self.assertRaises(ValueError):
                SecretScanner().scan_diff(str(path))
        line = "x" * (MAX_LINE_CHARS + 1)
        diff = f"--- a/a.txt\n+++ b/a.txt\n@@ -0,0 +1,1 @@\n+{line}\n"
        with self.assertRaises(DiffParseError):
            parse_diff(diff)

    def test_diff_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "real.diff"
            target.write_text("--- a/a\n+++ b/a\n@@ -0,0 +1,1 @@\n+x\n")
            link = root / "link.diff"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                SecretScanner().scan_diff(str(link))

    def test_directory_symlink_does_not_escape(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            outside_file = Path(outside, "external.py")
            value = "ghp" + "_" + "A" * 36
            key_name = "TO" + "KEN"
            outside_file.write_text(key_name + ' = "' + value + '"\n')
            (root / "linked.py").symlink_to(outside_file)
            result = SecretScanner().scan_directory(str(root), recursive=True)
            self.assertEqual(result.findings, [])
            root_link = Path(directory + "-link")
            try:
                root_link.symlink_to(root, target_is_directory=True)
                with self.assertRaises(ValueError):
                    SecretScanner().scan_directory(str(root_link), recursive=True)
            finally:
                root_link.unlink(missing_ok=True)

    def test_config_is_bounded_symlink_safe_and_rejects_unsafe_regex(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(json.dumps({"custom_patterns": [{"name": "BAD", "regex": "(a+)+"}]}))
            with self.assertRaises(ValueError):
                load_config(str(config))
            config.write_bytes(b" " * (MAX_CONFIG_BYTES + 1))
            with self.assertRaises(ValueError):
                load_config(str(config))
            real = root / "real.json"
            real.write_text("{}")
            link = root / "link.json"
            link.symlink_to(real)
            with self.assertRaises(ValueError):
                load_config(str(link))

    def test_allowlist_is_bounded_and_symlink_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "allowlist"
            path.write_bytes(b"#" * (MAX_ALLOWLIST_BYTES + 1))
            with self.assertRaises(ValueError):
                load_allowlist(str(path))
            real = root / "real-allowlist"
            real.write_text("path:tests/**\n")
            link = root / "link-allowlist"
            link.symlink_to(real)
            with self.assertRaises(ValueError):
                load_allowlist(str(link))

    def test_private_atomic_report_writer_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.json"
            write_private_atomic(report, b"{}", "scan report")
            self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
            target = root / "outside.json"
            target.write_text("unchanged")
            link = root / "linked-report.json"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                write_private_atomic(link, b"changed", "scan report")
            self.assertEqual(target.read_text(), "unchanged")
            diff = root / "clean.diff"
            diff.write_text("--- a/a.txt\n+++ b/a.txt\n@@ -0,0 +1,1 @@\n+hello\n")
            result = subprocess.run(
                [os.sys.executable, "-m", "src", "scan", "--diff", str(diff), "--output", str(link)],
                cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 3)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(target.read_text(), "unchanged")

    @staticmethod
    def _git_repo(directory: str):
        root = Path(directory)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        target = root / "sample.txt"
        target.write_text("before\n")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
        return root, target

    def test_staged_scan_never_executes_external_diff_or_textconv(self):
        with tempfile.TemporaryDirectory() as directory:
            root, target = self._git_repo(directory)
            marker = root / "executed"
            helper = root / "helper.sh"
            helper.write_text(f"#!/bin/sh\nprintf executed > {marker}\ncat \"$1\"\n")
            helper.chmod(0o755)
            subprocess.run(["git", "config", "diff.external", str(helper)], cwd=root, check=True)
            subprocess.run(["git", "config", "diff.evil.textconv", str(helper)], cwd=root, check=True)
            (root / ".gitattributes").write_text("*.txt diff=evil\n")
            target.write_text("after\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            previous = Path.cwd()
            try:
                os.chdir(root)
                result = SecretScanner(config_check=False).scan_staged()
            finally:
                os.chdir(previous)
            self.assertFalse(marker.exists())
            self.assertEqual(result.input_type, "staged")

    def test_action_collector_blocks_helpers_and_oversized_diff(self):
        collector = Path(__file__).resolve().parents[1] / "scripts/collect-diff.py"
        with tempfile.TemporaryDirectory() as directory:
            root, target = self._git_repo(directory)
            marker = root / "collector-executed"
            helper = root / "collector-helper.sh"
            helper.write_text(f"#!/bin/sh\nprintf executed > {marker}\ncat \"$1\"\n")
            helper.chmod(0o755)
            subprocess.run(["git", "config", "diff.external", str(helper)], cwd=root, check=True)
            target.write_text("after\n")
            subprocess.run(["git", "add", "sample.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "small"], cwd=root, check=True)
            output = root / "small.diff"
            result = subprocess.run(
                [os.sys.executable, str(collector), "--output", str(output)],
                cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())
            self.assertTrue(output.is_file())

            target.write_text("x" * (MAX_DIFF_BYTES + 4096))
            subprocess.run(["git", "add", "sample.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "large"], cwd=root, check=True)
            oversized = root / "large.diff"
            result = subprocess.run(
                [os.sys.executable, str(collector), "--output", str(oversized)],
                cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 3)
            self.assertFalse(oversized.exists())
            self.assertFalse(marker.exists())

    def test_staged_scan_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root, target = self._git_repo(directory)
            target.write_text("x" * (MAX_DIFF_BYTES + 4096))
            subprocess.run(["git", "add", "sample.txt"], cwd=root, check=True)
            previous = Path.cwd()
            try:
                os.chdir(root)
                with self.assertRaises(ValueError):
                    SecretScanner(config_check=False).scan_staged()
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
