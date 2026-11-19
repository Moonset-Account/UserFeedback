"""The documented CLI entry points must actually work end to end."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from password_screening.__main__ import main
from password_screening.digest import password_digest
from tests.fixtures import digest_of


class CliScreenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.digest_file = Path(self.tmp.name) / "digests.txt"
        self.digests = [password_digest("hunter2"), password_digest("swordfish")]
        self.digest_file.write_text(
            "# comment line\n\n" + "\n".join(self.digests) + "\n", encoding="utf-8"
        )

    def _run(self, argv: list[str]) -> tuple[int, list[dict], str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        return code, lines, err.getvalue()

    def test_screen_reports_hit_and_miss(self) -> None:
        code, lines, _ = self._run(
            [
                "screen",
                "--digests-file",
                str(self.digest_file),
                "--password",
                "hunter2",
                "--password",
                "not-listed",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual([line["matched"] for line in lines], [True, False])
        self.assertEqual({line["version"] for line in lines}, {lines[0]["version"]})
        self.assertEqual({line["pages_read"] for line in lines}, {1})

    def test_fail_on_match_exit_code_is_non_zero(self) -> None:
        code, _, _ = self._run(
            [
                "screen",
                "--digests-file",
                str(self.digest_file),
                "--password",
                "hunter2",
                "--fail-on-match",
            ]
        )
        self.assertEqual(code, 1)

    def test_case_and_whitespace_in_digest_file_are_normalised(self) -> None:
        lower = Path(self.tmp.name) / "lower.txt"
        lower.write_text("  " + self.digests[0].lower() + "  \n", encoding="utf-8")
        code, lines, _ = self._run(
            ["screen", "--digests-file", str(lower), "--password", "hunter2"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(lines[0]["matched"])

    def test_invalid_digest_file_is_rejected(self) -> None:
        bad = Path(self.tmp.name) / "bad.txt"
        bad.write_text("NOT-A-DIGEST\n", encoding="utf-8")
        with self.assertRaises(Exception):
            self._run(["screen", "--digests-file", str(bad), "--password", "x"])

    def test_missing_file_is_reported(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self._run(
                [
                    "screen",
                    "--digests-file",
                    os.path.join(self.tmp.name, "absent.txt"),
                    "--password",
                    "x",
                ]
            )


class CliDigestNormalisationTests(unittest.TestCase):
    def test_digest_file_matches_client_digest_function(self) -> None:
        # The CLI uses the same SHA-256 derivation as the client.
        self.assertEqual(password_digest("hunter2"), digest_of("hunter2"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
