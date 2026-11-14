"""End-to-end tests exercising the app.py command line interface."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from app import (
    EXIT_BLOCKED,
    EXIT_FATAL,
    EXIT_OK,
    build_parser,
    main,
)

from tests._helpers import TriageFixture


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class CliTests(TriageFixture):
    def _outputs(self, name: str) -> dict[str, Path]:
        out_dir = self.workspace / "out"
        return {
            "public": out_dir / f"{name}-public.json",
            "internal": out_dir / f"{name}-internal.json",
            "manifest": out_dir / f"{name}-manifest.json",
            "summary": out_dir / f"{name}-summary.json",
        }

    def _happy_build(self, build_id: str, debug_id: str, symbol: bytes) -> None:
        self.make_build(
            build_id, "arm64",
            [
                {"role": "main_program", "path": "bin/app",
                 "debug_id": debug_id},
                {"role": "runtime_library", "path": "lib/libfoo.dylib"},
                {"role": "debug_symbol", "path": "symbols/app.dbg",
                 "debug_id": debug_id},
            ],
            files={
                "bin/app": f"bin-{build_id}".encode(),
                "lib/libfoo.dylib": b"runtime",
                "symbols/app.dbg": symbol,
            },
        )

    def test_case_010_cli_emits_three_plans_and_lists_archive(
        self,
    ) -> None:
        """case_id TC-010: triage exit 0; list subcommand reads plans back."""

        debug_id = "CLI-HAPPY-010"
        self._happy_build("B010", debug_id, b"shared symbol bytes")
        out = self._outputs("tc010")
        argv = [
            "triage",
            "--index", str(self.indexes[0]),
            "--root", str(self.roots[0]),
            "--public-plan", str(out["public"]),
            "--internal-plan", str(out["internal"]),
            "--manifest", str(out["manifest"]),
            "--json-summary", str(out["summary"]),
        ]

        code, stdout, stderr = _run(argv)

        self.assertEqual(code, EXIT_OK, msg=stderr)
        for path in out.values():
            self.assertTrue(path.is_file())
        public = json.loads(out["public"].read_text())
        internal = json.loads(out["internal"].read_text())
        manifest = json.loads(out["manifest"].read_text())
        self.assertEqual(public["archive"], "public")
        self.assertEqual(public["summary"]["file_count"], 2)
        self.assertEqual(
            internal["architectures"][0]["debug_ids"][debug_id]["merged"],
            False,
        )
        self.assertEqual(manifest["summary"]["total_batches"], 1)
        self.assertIn("PUBLISH B010", stdout)

        code, listing, stderr = _run(["list", str(out["public"])])
        self.assertEqual(code, EXIT_OK, msg=stderr)
        self.assertIn("bin/app", listing)
        self.assertIn("lib/libfoo.dylib", listing)
        self.assertNotIn("symbols/app.dbg", listing)

        code, internal_listing, _ = _run(
            ["list", str(out["internal"]), "--format", "json"]
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(
            json.loads(internal_listing)["archive"], "internal"
        )

    def test_case_011_blocked_batch_returns_exit_code_3(self) -> None:
        """case_id TC-011: blocked run still writes every plan, exits 3."""

        self.make_build(
            "B011", "arm64",
            [
                {"role": "main_program", "path": "bin/app",
                 "debug_id": "MAIN-011"},
                {"role": "debug_symbol", "path": "symbols/app.dbg",
                 "debug_id": "SYMBOL-011"},
            ],
            files={"bin/app": b"x", "symbols/app.dbg": b"y"},
        )
        out = self._outputs("tc011")

        code, stdout, stderr = _run([
            "triage",
            "--index", str(self.indexes[0]),
            "--root", str(self.roots[0]),
            "--public-plan", str(out["public"]),
            "--internal-plan", str(out["internal"]),
            "--manifest", str(out["manifest"]),
        ])

        self.assertEqual(code, EXIT_BLOCKED, msg=stderr)
        self.assertIn("BLOCKED B011", stdout)
        manifest = json.loads(out["manifest"].read_text())
        self.assertEqual(manifest["summary"]["blocked_batch_count"], 1)
        self.assertTrue(out["public"].is_file())
        self.assertTrue(out["internal"].is_file())

    def test_case_012_malformed_index_is_fatal_exit_2(self) -> None:
        """case_id TC-012: unparseable JSON never produces partial plans."""

        root = self.workspace / "root-bad"
        root.mkdir()
        index = self.write_raw_index("bad.json", "{not valid json")
        out = self._outputs("tc012")

        code, _stdout, stderr = _run([
            "triage",
            "--index", str(index),
            "--root", str(root),
            "--public-plan", str(out["public"]),
            "--internal-plan", str(out["internal"]),
            "--manifest", str(out["manifest"]),
        ])

        self.assertEqual(code, EXIT_FATAL)
        self.assertIn("invalid JSON", stderr)
        self.assertFalse(out["public"].exists())

    def test_case_013_root_index_count_mismatch_is_fatal(self) -> None:
        """case_id TC-013: two indexes need one shared root or two roots."""

        self._happy_build("B013A", "ID-013A", b"a")
        self._happy_build("B013B", "ID-013B", b"b")
        out = self._outputs("tc013")
        argv = [
            "triage",
            "--index", str(self.indexes[0]),
            "--index", str(self.indexes[1]),
            "--root", str(self.roots[0]),
            "--root", str(self.roots[1]),
            "--root", str(self.roots[0]),
            "--public-plan", str(out["public"]),
            "--internal-plan", str(out["internal"]),
            "--manifest", str(out["manifest"]),
        ]

        code, _stdout, stderr = _run(argv)
        self.assertEqual(code, EXIT_FATAL)
        self.assertIn("--root", stderr)

    def test_case_014_manifest_lists_what_other_batches_can_publish(
        self,
    ) -> None:
        """case_id TC-014: blocked batch held, sibling's files enumerated."""

        self._happy_build("B014-OK", "ID-014-OK", b"ok symbols")
        self.make_build(
            "B014-BAD", "arm64",
            [
                {"role": "main_program", "path": "bin/app",
                 "debug_id": "ID-014-MAIN"},
                {"role": "debug_symbol", "path": "symbols/app.dbg",
                 "debug_id": "ID-014-SYM"},
            ],
            files={"bin/app": b"bad", "symbols/app.dbg": b"bad-dbg"},
        )
        out = self._outputs("tc014")

        code, _stdout, stderr = _run([
            "triage",
            "--index", str(self.indexes[0]),
            "--index", str(self.indexes[1]),
            "--root", str(self.roots[0]),
            "--root", str(self.roots[1]),
            "--public-plan", str(out["public"]),
            "--internal-plan", str(out["internal"]),
            "--manifest", str(out["manifest"]),
        ])
        self.assertEqual(code, EXIT_BLOCKED, msg=stderr)

        manifest = json.loads(out["manifest"].read_text())
        self.assertEqual(
            [b["build_id"] for b in manifest["blocked_batches"]],
            ["B014-BAD"],
        )
        publishable = manifest["publishable"]
        self.assertEqual(
            [b["build_id"] for b in publishable["batches"]],
            ["B014-OK"],
        )
        public_paths = {f["path"] for f in publishable["all_public_files"]}
        self.assertEqual(public_paths, {"bin/app", "lib/libfoo.dylib"})
        self.assertEqual(
            [f["path"] for f in publishable["all_symbol_files"]],
            ["symbols/app.dbg"],
        )
        # The blocked batch's symbol must not appear in the publishable
        # section; the blocked reason itself may still quote the debug id.
        self.assertNotIn(
            "ID-014-SYM",
            json.dumps(publishable),
        )
        self.assertNotIn(
            "B014-BAD",
            json.dumps(publishable),
        )

    def test_case_015_invalid_debug_identifier_blocks_batch(self) -> None:
        """case_id TC-015: identifiers with path separators are rejected."""

        self.make_build(
            "B015", "arm64",
            [
                {"role": "main_program", "path": "bin/app",
                 "debug_id": "id/with/slash"},
                {"role": "debug_symbol", "path": "symbols/app.dbg",
                 "debug_id": "id/with/slash"},
            ],
            files={"bin/app": b"a", "symbols/app.dbg": b"b"},
        )
        out = self._outputs("tc015")

        code, _stdout, _stderr = _run([
            "triage",
            "--index", str(self.indexes[0]),
            "--root", str(self.roots[0]),
            "--public-plan", str(out["public"]),
            "--internal-plan", str(out["internal"]),
            "--manifest", str(out["manifest"]),
        ])
        self.assertEqual(code, EXIT_BLOCKED)
        manifest = json.loads(out["manifest"].read_text())
        codes = {
            reason["code"]
            for batch in manifest["blocked_batches"]
            for reason in batch["reasons"]
        }
        self.assertIn("invalid_artifact_entry", codes)


class ParserTests(TriageFixture):
    def test_case_016_parser_requires_a_subcommand(self) -> None:
        """case_id TC-016: running without arguments fails instead of silently
        succeeding."""

        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args([])
