import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.git_history import collect_history_diffs
from src.scanner import SecretScanner
from tests.synthetic_values import assemble


class V4GitHistoryTests(unittest.TestCase):
    def git(self, repo, *args):
        return subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()

    def make_repo(self):
        temp = tempfile.TemporaryDirectory()
        repo = Path(temp.name)
        self.git(repo, "init", "-q")
        self.git(repo, "config", "user.email", "test@example.invalid")
        self.git(repo, "config", "user.name", "Synthetic Test")
        (repo / "app.py").write_text("print('clean')\n", encoding="utf-8")
        self.git(repo, "add", "app.py"); self.git(repo, "commit", "-qm", "clean")
        (repo / "app.py").write_text("token = '" + assemble("gh", "p_", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij") + "'\n", encoding="utf-8")
        self.git(repo, "commit", "-qam", "introduce synthetic token")
        introduced = self.git(repo, "rev-parse", "HEAD")
        (repo / "app.py").write_text("print('removed')\n", encoding="utf-8")
        self.git(repo, "commit", "-qam", "remove token")
        return temp, repo, introduced

    def test_finds_secret_introduced_then_deleted_and_deduplicates(self):
        temp, repo, introduced = self.make_repo()
        self.addCleanup(temp.cleanup)
        result = SecretScanner(config_check=False).scan_git_history(str(repo), max_commits=20)
        self.assertTrue(any(item.type == "secret" for item in result.findings))
        self.assertEqual(len({item.fingerprint for item in result.findings}), len(result.findings))
        self.assertEqual("git-history", result.input_type)
        self.assertNotIn(introduced, result.input_source)
        self.assertNotIn("ghp_", json.dumps(result.summary))

    def test_since_ref_excludes_older_introduction(self):
        temp, repo, introduced = self.make_repo(); self.addCleanup(temp.cleanup)
        result = SecretScanner(config_check=False).scan_git_history(str(repo), since_ref=introduced, max_commits=20)
        self.assertFalse(any(item.type == "secret" for item in result.findings))

    def test_bounds_and_invalid_repository_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):collect_history_diffs(tmp, max_commits=0)
            with self.assertRaises(ValueError):collect_history_diffs(tmp, max_commits=1001)
            with self.assertRaises(ValueError):collect_history_diffs(tmp, max_commits=True)
            with self.assertRaises(ValueError):collect_history_diffs(tmp, max_commits=5)

    def test_subprocess_uses_argument_arrays_without_shell(self):
        temp,repo,_=self.make_repo(); self.addCleanup(temp.cleanup)
        real_popen=subprocess.Popen
        with mock.patch("src.git_history.subprocess.Popen",wraps=real_popen) as popen:
            collect_history_diffs(str(repo),max_commits=1)
        self.assertGreater(len(popen.call_args_list),0)
        for call in popen.call_args_list:
            self.assertIsInstance(call.args[0],list)
            self.assertFalse(call.kwargs.get("shell",False))

    def test_cli_accepts_git_history_mode(self):
        temp, repo, _ = self.make_repo(); self.addCleanup(temp.cleanup)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        proc = subprocess.run(
            [sys.executable, "-m", "src", "scan", "--git-history", "--max-commits", "20", "--format", "json"],
            cwd=repo, env=env, text=True, capture_output=True,
        )
        self.assertIn(proc.returncode, (0, 1, 2))
        self.assertEqual("git-history", json.loads(proc.stdout)["input_type"])


if __name__ == "__main__":
    unittest.main()
