"""Engine-level tests for per-build blocking and cross-build symbol handling."""

from __future__ import annotations

from triage.engine import (
    REASON_DEBUG_ID_MISMATCH,
    REASON_MISSING_FILE,
    REASON_SYMBOL_CONFLICT,
    REASON_UNSAFE_PATH,
)

from tests._helpers import TriageFixture


class EngineHappyPathTests(TriageFixture):
    def test_case_001_public_plan_excludes_symbols_and_internal_plan_merges(
        self,
    ) -> None:
        """case_id TC-001: same debug id + identical bytes => merged symbol."""

        debug_id = "11112222-AAAA-BBBB-CCCC-000000000001"
        symbol_bytes = b"DWARF debug data shared by both builds"
        for build_id in ("B100", "B101"):
            self.make_build(
                build_id,
                "arm64",
                [
                    {"role": "main_program", "path": "bin/app",
                     "debug_id": debug_id},
                    {"role": "runtime_library", "path": "lib/libfoo.dylib"},
                    {"role": "runtime_resource", "path": "share/cfg.json"},
                    {"role": "debug_symbol", "path": "symbols/app.dbg",
                     "debug_id": debug_id},
                ],
                files={
                    "bin/app": f"binary {build_id}".encode(),
                    "lib/libfoo.dylib": b"runtime library",
                    "share/cfg.json": b"{}",
                    "symbols/app.dbg": symbol_bytes,
                },
            )

        result = self.run_triage()

        self.assertEqual(result.blocked_batches, [])
        public_paths = [
            f["path"]
            for f in result.public_plan["batches"][0]["files"]
        ]
        self.assertEqual(
            public_paths, ["bin/app", "lib/libfoo.dylib", "share/cfg.json"]
        )
        for batch in result.public_plan["batches"]:
            self.assertTrue(
                all(
                    entry["role"] != "debug_symbol"
                    for entry in batch["files"]
                )
            )
        arm64 = result.internal_plan["architectures"][0]
        self.assertEqual(arm64["architecture"], "arm64")
        entry = arm64["debug_ids"][debug_id]
        self.assertTrue(entry["merged"])
        self.assertEqual(entry["reference_count"], 2)
        self.assertEqual(
            entry["references"], ["B100:arm64", "B101:arm64"]
        )
        self.assertEqual(
            sorted(s["build_id"] for s in entry["sources"]),
            ["B100", "B101"],
        )
        self.assertEqual(
            result.internal_plan["summary"]["merged_entry_count"], 1
        )
        self.assertEqual(
            result.block_manifest["summary"]["blocked_batch_count"], 0
        )

    def test_case_002_two_architectures_are_organized_separately(self) -> None:
        """case_id TC-002: arch partitions never collide with each other."""

        for architecture, debug_id in (
            ("arm64", "A" * 32),
            ("x86_64", "B" * 32),
        ):
            self.make_build(
                "B200",
                architecture,
                [
                    {"role": "main_program", "path": "bin/app",
                     "debug_id": debug_id},
                    {"role": "debug_symbol", "path": "symbols/app.dbg",
                     "debug_id": debug_id},
                ],
                files={
                    "bin/app": f"bin-{architecture}".encode(),
                    "symbols/app.dbg": f"dbg-{architecture}".encode(),
                },
            )

        result = self.run_triage()

        self.assertEqual(result.blocked_batches, [])
        arches = [
            section["architecture"]
            for section in result.internal_plan["architectures"]
        ]
        self.assertEqual(arches, ["arm64", "x86_64"])
        self.assertEqual(
            result.public_plan["summary"]["batch_count"], 2
        )


class BlockingTests(TriageFixture):
    def test_case_003_debug_id_mismatch_blocks_only_affected_batch(
        self,
    ) -> None:
        """case_id TC-003: one bad batch is held, the other still publishes."""

        good_id = "C0FFEE01"
        bad_main_id = "DEAD0001"
        bad_symbol_id = "DEAD0002"

        self.make_build(
            "B300-GOOD", "arm64",
            [
                {"role": "main_program", "path": "bin/app",
                 "debug_id": good_id},
                {"role": "debug_symbol", "path": "symbols/app.dbg",
                 "debug_id": good_id},
            ],
            files={"bin/app": b"good-bin", "symbols/app.dbg": b"good-dbg"},
        )
        self.make_build(
            "B301-BAD", "arm64",
            [
                {"role": "main_program", "path": "bin/app",
                 "debug_id": bad_main_id},
                {"role": "debug_symbol", "path": "symbols/app.dbg",
                 "debug_id": bad_symbol_id},
            ],
            files={"bin/app": b"bad-bin", "symbols/app.dbg": b"bad-dbg"},
        )

        result = self.run_triage()

        blocked = result.blocked_batches
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].build.build_id, "B301-BAD")
        codes = {reason["code"] for reason in blocked[0].reasons}
        self.assertIn(REASON_DEBUG_ID_MISMATCH, codes)
        reason = next(r for r in blocked[0].reasons
                      if r["code"] == REASON_DEBUG_ID_MISMATCH)
        self.assertEqual(reason["main_program_debug_ids"], [bad_main_id])
        self.assertEqual(reason["symbol_debug_ids"], [bad_symbol_id])

        manifest = result.block_manifest
        self.assertEqual(manifest["summary"]["blocked_batch_count"], 1)
        self.assertEqual(manifest["summary"]["publishable_batch_count"], 1)
        publishable = manifest["publishable"]["batches"]
        self.assertEqual(
            [b["build_id"] for b in publishable], ["B300-GOOD"]
        )
        self.assertEqual(
            [f["path"] for f in publishable[0]["public_files"]],
            ["bin/app"],
        )
        # The blocked batch must not leak into either archive plan.
        published_ids = [
            b["build_id"] for b in result.public_plan["batches"]
        ]
        self.assertEqual(published_ids, ["B300-GOOD"])
        self.assertEqual(
            result.internal_plan["architectures"][0]["debug_ids"].keys(),
            {good_id},
        )

    def test_case_004_missing_artifact_blocks_that_batch_only(self) -> None:
        """case_id TC-004: a declared file that does not exist holds batch."""

        self.make_build(
            "B400", "arm64",
            [
                {"role": "main_program", "path": "bin/app",
                 "debug_id": "ID-400"},
                {"role": "debug_symbol", "path": "symbols/app.dbg",
                 "debug_id": "ID-400"},
            ],
            files={"bin/app": b"app"},  # symbols/app.dbg intentionally absent
        )
        self.make_build(
            "B401", "arm64",
            [
                {"role": "main_program", "path": "bin/app",
                 "debug_id": "ID-401"},
                {"role": "debug_symbol", "path": "symbols/app.dbg",
                 "debug_id": "ID-401"},
            ],
            files={"bin/app": b"app2", "symbols/app.dbg": b"dbg2"},
        )

        result = self.run_triage()

        blocked = result.blocked_batches
        self.assertEqual([b.build.build_id for b in blocked], ["B400"])
        self.assertIn(
            REASON_MISSING_FILE,
            {r["code"] for r in blocked[0].reasons},
        )
        self.assertEqual(
            [b["build_id"] for b in result.public_plan["batches"]],
            ["B401"],
        )

    def test_case_005_same_debug_id_different_content_is_conflict(
        self,
    ) -> None:
        """case_id TC-005: shared debug id with different bytes blocks both."""

        debug_id = "CONFLICT-ID-500"
        self.make_build(
            "B500", "arm64",
            [
                {"role": "main_program", "path": "bin/app",
                 "debug_id": debug_id},
                {"role": "runtime_library", "path": "lib/runtime.dylib"},
                {"role": "debug_symbol", "path": "symbols/a.dbg",
                 "debug_id": debug_id},
            ],
            files={
                "bin/app": b"bin-500",
                "lib/runtime.dylib": b"runtime-500",
                "symbols/a.dbg": b"symbol payload version one",
            },
        )
        self.make_build(
            "B501", "arm64",
            [
                {"role": "main_program", "path": "bin/app",
                 "debug_id": debug_id},
                {"role": "runtime_library", "path": "lib/runtime.dylib"},
                {"role": "debug_symbol", "path": "symbols/a.dbg",
                 "debug_id": debug_id},
            ],
            files={
                "bin/app": b"bin-501",
                "lib/runtime.dylib": b"runtime-501",
                "symbols/a.dbg": b"symbol payload version TWO -- different",
            },
        )

        result = self.run_triage()

        self.assertEqual(
            sorted(b.build.build_id for b in result.blocked_batches),
            ["B500", "B501"],
        )
        for blocked in result.blocked_batches:
            self.assertIn(
                REASON_SYMBOL_CONFLICT,
                {r["code"] for r in blocked.reasons},
            )
        conflicts = result.block_manifest["symbol_conflicts"]
        self.assertEqual(
            conflicts,
            [{"architecture": "arm64", "debug_id": debug_id}],
        )
        # Conflicted symbols are never stored, even as a merged entry.
        self.assertEqual(
            result.internal_plan["summary"]["symbol_entry_count"], 0
        )
        self.assertEqual(result.public_plan["batches"], [])
        observed = {
            h
            for blocked in result.blocked_batches
            for reason in blocked.reasons
            if reason["code"] == REASON_SYMBOL_CONFLICT
            for h in reason["observed_sha256"]
        }
        self.assertEqual(len(observed), 2)

    def test_case_006_conflict_is_scoped_by_architecture(self) -> None:
        """case_id TC-006: same debug id on different arches is not a clash."""

        debug_id = "SAME-ID-DIFFERENT-ARCH"
        self.make_build(
            "B600", "arm64",
            [
                {"role": "main_program", "path": "bin/app",
                 "debug_id": debug_id},
                {"role": "debug_symbol", "path": "symbols/a.dbg",
                 "debug_id": debug_id},
            ],
            files={"bin/app": b"arm-bin", "symbols/a.dbg": b"arm-dbg"},
        )
        self.make_build(
            "B601", "x86_64",
            [
                {"role": "main_program", "path": "bin/app",
                 "debug_id": debug_id},
                {"role": "debug_symbol", "path": "symbols/a.dbg",
                 "debug_id": debug_id},
            ],
            files={"bin/app": b"x86-bin", "symbols/a.dbg": b"x86-dbg"},
        )

        result = self.run_triage()

        self.assertEqual(result.blocked_batches, [])
        arches = {
            section["architecture"]: section
            for section in result.internal_plan["architectures"]
        }
        self.assertEqual(set(arches), {"arm64", "x86_64"})
        for section in arches.values():
            self.assertIn(debug_id, section["debug_ids"])
        self.assertEqual(
            result.internal_plan["summary"]["conflict_count"], 0
        )


class SecurityBoundaryTests(TriageFixture):
    def test_case_007_parent_traversal_is_rejected_and_batch_blocked(
        self,
    ) -> None:
        """case_id TC-007: ../ paths cannot read outside the artifact root."""

        secret = self.workspace / "secret.txt"
        secret.write_bytes(b"top secret")
        self.make_build(
            "B700", "arm64",
            [
                {"role": "main_program", "path": "../secret.txt",
                 "debug_id": "ID-700"},
                {"role": "debug_symbol", "path": "symbols/a.dbg",
                 "debug_id": "ID-700"},
            ],
            files={"symbols/a.dbg": b"dbg"},
        )

        result = self.run_triage()

        self.assertEqual(len(result.blocked_batches), 1)
        reason = result.blocked_batches[0].reasons[0]
        self.assertEqual(reason["code"], REASON_UNSAFE_PATH)
        self.assertEqual(reason["path"], "../secret.txt")
        self.assertEqual(result.public_plan["batches"], [])

    def test_case_008_symlink_escape_is_rejected(self) -> None:
        """case_id TC-008: a symlink leaving the root blocks the batch."""

        outside = self.workspace / "outside"
        outside.mkdir()
        (outside / "evil.dbg").write_bytes(b"not part of the build")
        root, _ = self.make_build(
            "B800", "arm64",
            [
                {"role": "main_program", "path": "bin/app",
                 "debug_id": "ID-800"},
                {"role": "debug_symbol", "path": "symbols/evil.dbg",
                 "debug_id": "ID-800"},
            ],
            files={"bin/app": b"app"},
        )
        self.symlink(root, "symbols/evil.dbg", outside / "evil.dbg")

        result = self.run_triage()

        self.assertEqual(len(result.blocked_batches), 1)
        codes = {r["code"] for r in result.blocked_batches[0].reasons}
        self.assertIn(REASON_UNSAFE_PATH, codes)
        self.assertEqual(result.internal_plan["summary"]["symbol_entry_count"], 0)

    def test_case_009_absolute_path_is_rejected(self) -> None:
        """case_id TC-009: absolute artifact paths are refused outright."""

        self.make_build(
            "B900", "arm64",
            [
                {"role": "main_program", "path": "/etc/hosts",
                 "debug_id": "ID-900"},
            ],
            files={},
        )

        result = self.run_triage()

        blocked = result.blocked_batches[0]
        self.assertEqual(blocked.reasons[0]["code"], REASON_UNSAFE_PATH)
        self.assertEqual(blocked.reasons[0]["path"], "/etc/hosts")
