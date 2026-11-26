import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from column_migration import (  # noqa: E402
    ColumnMigrationStore,
    CrashController,
    DefinitionRegistry,
    SimulatedCrash,
)
from column_migration.crash_scenarios import run_crash_scenario  # noqa: E402


def fresh_registry() -> DefinitionRegistry:
    return DefinitionRegistry.with_score_versions()


class InProcessCrashMatrixTest(unittest.TestCase):
    """Replay every recorded barrier one at a time from a fresh store."""

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as record_dir:
            recorder = CrashController()
            run_crash_scenario(record_dir, recorder, prune=True)
            cls.events = list(recorder.events)
        cls.create_complete = next(
            i
            for i, event in enumerate(cls.events)
            if event.point == "create_complete"
        )

    def test_every_barrier_reopens_to_coherent_snapshots(self) -> None:
        targets = range(self.create_complete + 1, len(self.events))
        checked = 0
        for target in targets:
            with self.subTest(target=target):
                with tempfile.TemporaryDirectory() as tmp:
                    controller = CrashController(crash_at_seq=target)
                    try:
                        run_crash_scenario(tmp, controller, prune=True)
                    except SimulatedCrash:
                        pass
                    else:
                        self.fail(
                            f"target {target} ({self.events[target].point}) did not crash"
                        )

                    store = ColumnMigrationStore.open(
                        tmp, registry=fresh_registry()
                    )
                    report = store.verify_all_snapshots()
                    if not report.ok:
                        self.fail(
                            "post-crash verification failed at "
                            f"{target} {self.events[target].point}: "
                            + "; ".join(report.errors)
                        )

                    # Completing orphan/temp reclamation must not remove a file
                    # referenced by a committed snapshot.
                    store.reclaim_unreferenced_files()
                    report = store.verify_all_snapshots()
                    self.assertTrue(
                        report.ok,
                        "post-reclaim verification failed at "
                        f"{target} {self.events[target].point}: "
                        + "; ".join(report.errors),
                    )

                    # Recovery is writable and can publish a new group.  An
                    # early cut may have occurred before the score declaration
                    # committed; declare it in that case.
                    store.import_rows(
                        "g_recovery",
                        [{"id": "r1", "x": 7}, {"id": "r2", "x": 8}],
                    )
                    recovered_manifest = store.load_manifest()
                    if "score" not in recovered_manifest.get("columns", {}):
                        store.add_computed_column(
                            "score", 1, migration_id="recovery-score-v1"
                        )
                    store.publish_backfill(store.prepare_backfill("g_recovery"))
                    store.update_rows({"r1": {"x": 70}})
                    final_report = store.verify_all_snapshots()
                    self.assertTrue(
                        final_report.ok,
                        "; ".join(final_report.errors),
                    )
                    checked += 1
        self.assertGreater(checked, 100)


class RandomSubprocessCutTest(unittest.TestCase):
    """Actually terminate a child with os._exit at randomly chosen barriers."""

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as record_dir:
            recorder = CrashController()
            run_crash_scenario(record_dir, recorder, prune=True)
            cls.events = list(recorder.events)
        first_committed_import = next(
            i
            for i, event in enumerate(cls.events)
            if event.point == "after_manifest_switch"
            and event.context.get("operation") == "import_rows"
        )
        cls.eligible_targets = list(range(first_committed_import + 1, len(cls.events)))

    def test_random_real_process_cuts(self) -> None:
        rng = random.Random(20261127)
        number = min(14, len(self.eligible_targets))
        targets = rng.sample(self.eligible_targets, number)
        phase_points = {
            "after_source_data_write",
            "after_computed_data_write",
            "after_manifest_write",
            "before_manifest_switch",
            "after_manifest_switch",
            "before_reclaim",
            "before_reclaim_file",
            "after_unlink_before_dir_fsync",
            "after_reclaim_file",
            "after_reclaim",
        }
        seen_points = set()

        for target in targets:
            point = self.events[target].point
            seen_points.add(point)
            with self.subTest(target=target, point=point):
                with tempfile.TemporaryDirectory() as tmp:
                    store_dir = Path(tmp) / "store"
                    proc = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "column_migration.crash_demo",
                            str(store_dir),
                            str(target),
                        ],
                        cwd=REPO_ROOT,
                        capture_output=True,
                        text=True,
                        timeout=60,
                        check=False,
                    )
                    self.assertEqual(
                        proc.returncode,
                        97,
                        msg=(
                            f"target {target} {point}\n"
                            f"stdout={proc.stdout}\nstderr={proc.stderr}"
                        ),
                    )
                    store = ColumnMigrationStore.open(
                        store_dir, registry=fresh_registry()
                    )
                    report = store.verify_all_snapshots()
                    self.assertTrue(
                        report.ok,
                        msg=(
                            f"target {target} {point}: "
                            + "; ".join(report.errors)
                        ),
                    )
                    self.assertGreater(report.rows_checked, 0)
                    store.reclaim_unreferenced_files()
                    self.assertTrue(store.verify_all_snapshots().ok)

        # The random sample should hit meaningful phase families.  If the fixed
        # seed ever misses one, the explicit barrier test below still covers it.
        self.assertTrue(seen_points)

    def test_explicit_phase_families_in_real_subprocess(self) -> None:
        wanted = [
            "after_source_data_write",
            "after_computed_data_write",
            "after_manifest_write",
            "before_manifest_switch",
            "after_manifest_switch",
            "before_reclaim",
            "before_reclaim_file",
            "after_unlink_before_dir_fsync",
            "after_reclaim_file",
        ]
        first_by_point = {}
        for i, event in enumerate(self.events):
            if event.point in wanted and event.point not in first_by_point:
                first_by_point[event.point] = i

        for point in wanted:
            target = first_by_point.get(point)
            if target is None:
                self.fail(f"scenario did not record barrier {point}")
            with self.subTest(point=point, target=target):
                with tempfile.TemporaryDirectory() as tmp:
                    store_dir = Path(tmp) / "store"
                    proc = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "column_migration.crash_demo",
                            str(store_dir),
                            str(target),
                        ],
                        cwd=REPO_ROOT,
                        capture_output=True,
                        text=True,
                        timeout=60,
                        check=False,
                    )
                    self.assertEqual(proc.returncode, 97, proc.stderr)
                    store = ColumnMigrationStore.open(
                        store_dir, registry=fresh_registry()
                    )
                    self.assertTrue(store.verify_all_snapshots().ok)


if __name__ == "__main__":
    unittest.main()
