"""Deterministic interleaved scenario used by crash tests and subprocesses."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .crash import CrashController
from .definitions import DefinitionRegistry
from .errors import BackfillConflict
from .store import ColumnMigrationStore


def run_crash_scenario(
    root: str | Path,
    crash: Optional[CrashController] = None,
    *,
    prune: bool = True,
) -> ColumnMigrationStore:
    """Exercise mixed reads, conflicts, upgrades, undo, and file reclamation.

    The scenario is deterministic apart from generated chunk filenames, which do
    not change the sequence of crash barriers.
    """

    registry = DefinitionRegistry.with_score_versions()
    store = ColumnMigrationStore.create(root, registry=registry, crash=crash)

    store.import_rows(
        "g0",
        [
            {"id": "a0", "x": 1},
            {"id": "a1", "x": 2},
            {"id": "a2", "x": 3},
        ],
    )
    store.import_rows(
        "g1",
        [
            {"id": "b0", "x": 10},
            {"id": "b1", "x": 11},
        ],
    )
    store.import_rows(
        "g2",
        [
            {"id": "c0", "x": 20},
            {"id": "c1", "x": 21},
        ],
    )

    v1_migration = store.add_computed_column(
        "score", 1, migration_id="score-v1"
    )

    # One healthy v1 backfill.
    job_g0 = store.prepare_backfill("g0")
    store.publish_backfill(job_g0)

    # Unrelated group movement while a g0 job is open should not affect its
    # row-version CAS; publish this after moving g1.
    unrelated_job = store.prepare_backfill("g0")
    store.update_rows({"b0": {"x": 100}})
    # g0 versions are unchanged, so this succeeds despite a newer manifest.
    store.publish_backfill(unrelated_job)

    # Late update after read: stale publication must be rejected.
    stale_g1 = store.prepare_backfill("g1")
    store.update_rows({"b1": {"x": 111}})
    try:
        store.publish_backfill(stale_g1)
    except BackfillConflict:
        pass
    else:  # pragma: no cover - scenario invariant
        raise AssertionError("late update did not cause BackfillConflict")

    # Whole-group rewrite after read: also rejected by row-version CAS.
    stale_g2 = store.prepare_backfill("g2")
    store.rewrite_group("g2")
    try:
        store.publish_backfill(stale_g2)
    except BackfillConflict:
        pass
    else:  # pragma: no cover - scenario invariant
        raise AssertionError("group rewrite did not cause BackfillConflict")

    # Fresh jobs publish at current row versions.
    fresh_g1 = store.prepare_backfill("g1")
    store.publish_backfill(fresh_g1)
    fresh_g2 = store.prepare_backfill("g2")
    store.publish_backfill(fresh_g2)

    # Definition upgrade.  Old snapshots are not touched.
    v2_migration = store.upgrade_definition(
        "score", 2, migration_id="score-v2"
    )

    v2_g0 = store.prepare_backfill("g0")
    store.publish_backfill(v2_g0)

    # Undo the active v2 migration on g1 before it has a v2 chunk.
    store.undo_group_migration("g1", migration_id=v2_migration)

    # Publish and then undo v2 on g2, exercising a materialized undo.
    v2_g2 = store.prepare_backfill("g2")
    store.publish_backfill(v2_g2)
    store.undo_group_migration("g2", migration_id=v2_migration)

    # Undo the original v1 group migration too; reads remain explainable.
    store.undo_group_migration("g0", migration_id=v1_migration)

    if prune:
        # Deliberately forget older ephemeral manifests in this crash scenario
        # so old source chunks become reclaimable.  Separate tests pin/retain
        # definition-upgrade snapshots and assert those are not broken.
        store.prune_snapshots(keep_latest=4)
        store.reclaim_unreferenced_files()

    return store
