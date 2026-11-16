import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryContractTests(unittest.TestCase):
    def test_proprietary_and_private_paths_are_absent(self):
        tracked = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
        forbidden_parts = {
            "agency", "commercial", "buyer", "operator", "private", "mcpwatch",
            "changeguard", "changeguard_core", "changefirewall", "firewall",
        }
        for path in tracked:
            lowered = path.lower()
            parts = set(Path(lowered).parts)
            self.assertFalse(parts.intersection(forbidden_parts), path)
            self.assertFalse(lowered.endswith((".zip", ".pem", ".key", ".p12")), path)
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
        self.assertNotIn("firewall", pyproject)
        self.assertNotIn("mcpwatch", pyproject)

    def test_workflow_actions_are_full_sha_pinned(self):
        text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        uses = re.findall(r"uses:\s*([^\s#]+)", text)
        self.assertGreaterEqual(len(uses), 3)
        for value in uses:
            self.assertRegex(value, r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")

    def test_action_does_not_execute_target_project(self):
        wrapper = (ROOT / "scripts/run-action.sh").read_text(encoding="utf-8")
        collector = (ROOT / "scripts/collect-diff.py").read_text(encoding="utf-8")
        gitdiff = (ROOT / "src/gitdiff.py").read_text(encoding="utf-8")
        text = wrapper + "\n" + collector + "\n" + gitdiff
        for forbidden in (
            "npm install", "npm test", "pytest", "tox", "make ", "eval ",
            "source ", "bash -c", "pip install",
        ):
            self.assertNotIn(forbidden, text)
        self.assertIn("core.pager=cat", gitdiff)
        self.assertIn("--no-ext-diff", gitdiff)
        self.assertIn("--no-textconv", gitdiff)
        self.assertIn("python -m src scan --diff", wrapper)
        self.assertIn("MAX_DIFF_BYTES", gitdiff)
        self.assertIn("selectors.DefaultSelector", gitdiff)

    def test_precommit_contract(self):
        text = (ROOT / ".pre-commit-hooks.yaml").read_text(encoding="utf-8")
        self.assertIn("secret-scanner scan --staged", text)
        self.assertIn("pass_filenames: false", text)
        self.assertIn("language: python", text)

    def _repo(self, root, unsafe=False):
        repo = Path(root) / ("unsafe" if unsafe else "safe")
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        target = repo / "app.py"
        target.write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
        if unsafe:
            value = "ghp" + "_" + "A" * 36
            key_name = "TO" + "KEN"
            target.write_text(key_name + ' = "' + value + '"\n', encoding="utf-8")
        else:
            target.write_text("VALUE = 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "change"], cwd=repo, check=True)
        return repo

    def _run_action(self, repo, temp):
        env = dict(os.environ)
        env.update({
            "CRT_ACTION_PATH": str(ROOT),
            "CRT_PROFILE": "secrets-only",
            "CRT_BASE_SHA": "",
            "CRT_HEAD_SHA": "",
            "RUNNER_TEMP": str(Path(temp) / "runner"),
            "GITHUB_OUTPUT": str(Path(temp) / "output.txt"),
            "PATH": str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", ""),
        })
        Path(env["RUNNER_TEMP"]).mkdir()
        return subprocess.run(
            ["bash", str(ROOT / "scripts/run-action.sh")], cwd=repo, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=120, check=False,
        )

    def test_action_allows_clean_diff_and_blocks_secret_like_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            safe = self._repo(directory, unsafe=False)
            safe_temp = Path(directory) / "safe-temp"
            safe_temp.mkdir()
            allowed = self._run_action(safe, safe_temp)
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            report = json.loads((safe_temp / "runner/coderisktools-scan.json").read_text())
            self.assertEqual(report["findings"], [])

            unsafe = self._repo(directory, unsafe=True)
            unsafe_temp = Path(directory) / "unsafe-temp"
            unsafe_temp.mkdir()
            blocked = self._run_action(unsafe, unsafe_temp)
            self.assertEqual(blocked.returncode, 1, blocked.stderr)
            combined = blocked.stdout + blocked.stderr
            self.assertNotIn("ghp_", combined)
            report = json.loads((unsafe_temp / "runner/coderisktools-scan.json").read_text())
            self.assertGreater(len(report["findings"]), 0)
            self.assertEqual(report["findings"][0]["matched_text"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
