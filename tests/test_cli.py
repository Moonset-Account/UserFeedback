"""Acceptance: the command line entry point performs the same work end to end."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from tests import support

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CliTest(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, PYTHONPATH=ROOT)
        return subprocess.run(
            [sys.executable, "-m", "docredact", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )

    def _write_input(self, directory: str) -> str:
        path = os.path.join(directory, "input.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(support.build_document(), handle, ensure_ascii=False)
        return path

    def test_redact_then_extract_round_trip(self) -> None:
        start, end = support.COPY_SPANS["alice@example.com"]
        with tempfile.TemporaryDirectory() as directory:
            source = self._write_input(directory)
            target = os.path.join(directory, "redacted.json")

            redact = self._run(
                "redact", "--input", source, "--output", target,
                "--page", "p1", "--path", "main.body",
                "--start", str(start), "--end", str(end), "--report",
            )
            self.assertEqual(redact.returncode, 0, redact.stderr)
            report = json.loads(redact.stdout)
            self.assertEqual(report["removed_text"], "alice@example.com")
            self.assertEqual([s["utterance"] for s in report["removed_speech"]], ["alice at example dot com"])

            with open(target, "r", encoding="utf-8") as handle:
                exported = json.load(handle)
            self.assertNotIn("txt_secret", exported["objects"])
            self.assertNotIn("txt_orphan", exported["objects"])

            extract = self._run("extract", "--input", target, "--page", "p1")
            self.assertEqual(extract.returncode, 0, extract.stderr)
            self.assertEqual(
                json.loads(extract.stdout),
                support.p1_text(copy=support.copy_without("alice@example.com")),
            )

            other = self._run("extract", "--input", target, "--page", "p2")
            self.assertEqual(json.loads(other.stdout), support.HEAD_TEXT + support.COPY_TEXT)

            speech = self._run("extract", "--input", target, "--page", "p1", "--what", "speech")
            utterances = [s["utterance"] for s in json.loads(speech.stdout)]
            self.assertNotIn("alice at example dot com", utterances)

    def test_multi_step_path_flags(self) -> None:
        start, end = support.COPY_SPANS["launches in Q3."]
        with tempfile.TemporaryDirectory() as directory:
            source = self._write_input(directory)
            target = os.path.join(directory, "out.json")
            redact = self._run(
                "redact", "--input", source, "--output", target,
                "--page", "p2", "--path", "main", "--path", "body",
                "--start", str(start), "--end", str(end),
            )
            self.assertEqual(redact.returncode, 0, redact.stderr)
            extract = self._run("extract", "--input", target, "--page", "p2")
            self.assertEqual(
                json.loads(extract.stdout),
                support.HEAD_TEXT + support.COPY_TEXT.replace("launches in Q3.", ""),
            )

    def test_invalid_range_exits_nonzero_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._write_input(directory)
            target = os.path.join(directory, "never.json")
            result = self._run(
                "redact", "--input", source, "--output", target,
                "--page", "p1", "--path", "main.body", "--start", "9", "--end", "3",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("docredact:", result.stderr)
            self.assertFalse(os.path.exists(target))

    def test_unknown_page_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._write_input(directory)
            target = os.path.join(directory, "never.json")
            result = self._run(
                "redact", "--input", source, "--output", target,
                "--page", "ghost", "--path", "main.body", "--start", "0", "--end", "1",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(os.path.exists(target) and os.path.getsize(target) > 0)


if __name__ == "__main__":
    unittest.main()
