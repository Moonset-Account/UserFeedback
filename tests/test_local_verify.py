"""Meta-tests: the verification wrapper must fail loudly, never silently pass.

These tests do not trust the wrapper's happy path. They drive its discovery and
counting logic against synthetic suites (zero tests, all skipped, one failure)
inside a throwaway package, so a regression in the wrapper itself is caught.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import local_verify  # noqa: E402


class CountingResultTests(unittest.TestCase):
    def test_skipped_tests_are_not_counted_as_executed(self) -> None:
        import io

        class SkippedCase(unittest.TestCase):
            @unittest.skip("synthetic skip")
            def test_skipped(self) -> None:
                pass

        suite = unittest.TestSuite([SkippedCase("test_skipped")])
        runner = unittest.TextTestRunner(
            stream=io.StringIO(), verbosity=0, resultclass=local_verify.RecordingResult
        )
        result = runner.run(suite)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.executed_not_skipped, 0)

    def test_passing_test_is_counted(self) -> None:
        import io

        class PassingCase(unittest.TestCase):
            def test_passes(self) -> None:
                self.assertTrue(True)

        suite = unittest.TestSuite([PassingCase("test_passes")])
        runner = unittest.TextTestRunner(
            stream=io.StringIO(), verbosity=0, resultclass=local_verify.RecordingResult
        )
        result = runner.run(suite)
        self.assertEqual(result.executed_not_skipped, 1)


class WrapperFailureModeTests(unittest.TestCase):
    """Run the wrapper as a subprocess against throwaway suites."""

    def _wrapper_in(self, tmp: str, body: str) -> subprocess.CompletedProcess:
        """Create a minimal project with app.py-free dummy files and run the wrapper.

        The wrapper needs tests/, fixtures/, and app.py because it also replays
        CLI cases; we only care about the unit-suite section, so we run it in a
        copy of the real project where tests/ is replaced.
        """

        import shutil

        for name in ("quota", "tools", "fixtures"):
            source = os.path.join(ROOT, name)
            target = os.path.join(tmp, name)
            if os.path.isdir(source):
                shutil.copytree(source, target)
        for name in ("app.py",):
            shutil.copy2(os.path.join(ROOT, name), os.path.join(tmp, name))
        tests_dir = os.path.join(tmp, "tests")
        os.makedirs(tests_dir, exist_ok=True)
        with open(os.path.join(tests_dir, "__init__.py"), "w", encoding="utf-8") as handle:
            handle.write("")
        with open(os.path.join(tests_dir, "test_synthetic.py"), "w", encoding="utf-8") as handle:
            handle.write(textwrap.dedent(body))

        return subprocess.run(
            [sys.executable, os.path.join(tmp, "tools", "local_verify.py")],
            cwd=tmp,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_zero_discovered_tests_fails_the_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            completed = self._wrapper_in(tmp, "# no tests here\n")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn('"discovered_test_count": 0', completed.stdout)
        self.assertIn("refusing to report success", completed.stdout)

    def test_all_skipped_fails_the_wrapper(self) -> None:
        body = """
        import unittest

        class Skipped(unittest.TestCase):
            @unittest.skip("synthetic skip")
            def test_skipped(self):
                pass
        """
        with tempfile.TemporaryDirectory() as tmp:
            completed = self._wrapper_in(tmp, body)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("every test was skipped", completed.stdout)
        self.assertIn('"passed": false', completed.stdout)

    def test_one_failure_fails_the_wrapper(self) -> None:
        body = """
        import unittest

        class Failing(unittest.TestCase):
            def test_fails(self):
                self.assertEqual(1, 2)
        """
        with tempfile.TemporaryDirectory() as tmp:
            completed = self._wrapper_in(tmp, body)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn('"failures": 1', completed.stdout)

    def test_one_passing_test_succeeds(self) -> None:
        body = """
        import unittest

        class Passing(unittest.TestCase):
            def test_passes(self):
                self.assertTrue(True)
        """
        with tempfile.TemporaryDirectory() as tmp:
            completed = self._wrapper_in(tmp, body)
        self.assertEqual(completed.returncode, 0, completed.stdout[-2000:] + completed.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
