"""Meta-tests for tools/local_verify.py's refusal behaviour.

The wrapper is part of the delivered contract: it must not report success when
there is no real, non-skipped test evidence.  These tests run it against
temporary, deliberately broken projects and assert on its actual exit code and
the evidence it prints.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "tools" / "local_verify.py"
RUNNER_SHIM = textwrap.dedent(
    """
    import runpy, sys
    from pathlib import Path
    project = Path(sys.argv[1])
    sys.argv = [str(Path(project) / "tools" / "local_verify.py")]
    sys.path.insert(0, str(project))
    runpy.run_path(sys.argv[0], run_name="__main__")
    """
)


def make_project(tmp: Path, test_body: str) -> Path:
    """Create a minimal project that reuses the real verifier and app."""

    project = tmp / "project"
    (project / "tools").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "samples" / "prod").mkdir(parents=True)
    shutil.copy2(VERIFIER, project / "tools" / "local_verify.py")
    shutil.copy2(ROOT / "app.py", project / "app.py")
    shutil.copytree(ROOT / "secretrefs", project / "secretrefs",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("environment.json", "services.json", "catalog.json"):
        shutil.copy2(ROOT / "samples" / "prod" / name,
                     project / "samples" / "prod" / name)
    (project / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (project / "tests" / "test_generated.py").write_text(test_body, encoding="utf-8")
    return project


def run_verifier(project: Path) -> subprocess.CompletedProcess:
    shim = project.parent / "run_verifier.py"
    shim.write_text(RUNNER_SHIM, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(shim), str(project)],
        capture_output=True,
        text=True,
        timeout=120,
    )


class VerifierRefusalTests(unittest.TestCase):
    def test_zero_discovered_tests_is_failure(self) -> None:
        """VERIFY-ZERO: an empty test directory must not be reported as passing."""

        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw), "")
            result = run_verifier(project)

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("discovered zero tests", result.stdout)
        self.assertIn("RESULT: FAILED", result.stdout)

    def test_all_skipped_tests_is_failure(self) -> None:
        """VERIFY-SKIP: a fully skipped suite must not be reported as passing."""

        body = textwrap.dedent(
            """
            import unittest

            class Skipped(unittest.TestCase):
                @unittest.skip("synthetic total skip")
                def test_never_runs(self) -> None:
                    pass
            """
        )
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw), body)
            result = run_verifier(project)

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("every executed test was skipped", result.stdout)
        self.assertIn("RESULT: FAILED", result.stdout)

    def test_failing_test_is_failure(self) -> None:
        """VERIFY-FAIL: a real failing test must make the wrapper exit non-zero."""

        body = textwrap.dedent(
            """
            import unittest

            class Failing(unittest.TestCase):
                def test_expected_failure(self) -> None:
                    self.assertEqual(1, 2)
            """
        )
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw), body)
            result = run_verifier(project)

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("1 failing test(s)", result.stdout)
        self.assertIn("RESULT: FAILED", result.stdout)

    def test_passing_project_is_success(self) -> None:
        """VERIFY-PASS: a genuinely passing project exits zero."""

        body = textwrap.dedent(
            """
            import unittest

            class Passing(unittest.TestCase):
                def test_truth(self) -> None:
                    self.assertTrue(True)
            """
        )
        with tempfile.TemporaryDirectory() as raw:
            project = make_project(Path(raw), body)
            result = run_verifier(project)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT: PASSED", result.stdout)


if __name__ == "__main__":
    unittest.main()
