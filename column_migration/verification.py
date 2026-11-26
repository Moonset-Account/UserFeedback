"""Semantic and on-disk invariant verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .storage import verify_ref


@dataclass
class SnapshotVerificationReport:
    snapshot_id: int
    column: Optional[str]
    ok: bool = True
    row_count: int = 0
    group_count: int = 0
    materialized_groups: List[str] = field(default_factory=list)
    lazy_groups: List[str] = field(default_factory=list)
    definition_ref: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.ok = False
        self.errors.append(message)


@dataclass
class AllSnapshotsReport:
    ok: bool
    snapshot_count: int
    rows_checked: int
    reports: List[SnapshotVerificationReport]
    errors: List[str] = field(default_factory=list)
    reclaimed: List[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"snapshots={self.snapshot_count}",
            f"rows={self.rows_checked}",
            f"ok={self.ok}",
        ]
        if self.reclaimed:
            parts.append(f"reclaimed={len(self.reclaimed)}")
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return ", ".join(parts)


def verify_snapshot(
    store: Any,
    snapshot_id: Optional[int] = None,
    *,
    column: Optional[str] = None,
) -> SnapshotVerificationReport:
    """Verify one committed snapshot and independently recompute every value."""

    try:
        manifest = store.load_manifest(snapshot_id)
    except Exception as exc:  # verification reports rather than asserting
        sid = snapshot_id if snapshot_id is not None else -1
        report = SnapshotVerificationReport(sid, column)
        report.fail(f"cannot load manifest: {type(exc).__name__}: {exc}")
        return report

    sid = int(manifest["snapshot_id"])
    report = SnapshotVerificationReport(sid, column)

    # Verify source chunks first.  This is meaningful even for early snapshots
    # before any computed column has been declared.
    source_values_by_key: Dict[str, Any] = {}
    source_row_count = 0
    for group_id, group in manifest.get("groups", {}).items():
        try:
            source_payload = verify_ref(store.root, group["source"])
        except Exception as exc:
            report.fail(
                f"group {group_id}: source reference verification failed: {exc}"
            )
            continue
        payload_versions = {
            str(row["key"]): int(row["source_version"])
            for row in source_payload.get("rows", [])
        }
        manifest_versions = {
            str(key): int(version)
            for key, version in group.get("source_versions", {}).items()
        }
        if payload_versions != manifest_versions:
            report.fail(
                f"group {group_id}: source chunk versions {payload_versions} "
                f"!= manifest versions {manifest_versions}"
            )
        for row in source_payload.get("rows", []):
            source_values_by_key[str(row["key"])] = (group_id, row)
            source_row_count += 1

    declared_columns = manifest.get("columns", {})
    if column is None and not declared_columns:
        report.row_count = source_row_count
        report.group_count = len(manifest.get("groups", {}))
        report.ok = not report.errors
        return report

    try:
        read = store.read_snapshot(sid, column=column)
    except Exception as exc:
        report.fail(f"read_snapshot failed: {type(exc).__name__}: {exc}")
        report.row_count = source_row_count
        report.group_count = len(manifest.get("groups", {}))
        report.ok = False
        return report

    report.column = read.column
    report.row_count = len(read.rows)
    report.group_count = len(manifest.get("groups", {}))
    report.materialized_groups = list(read.materialized_groups)
    report.lazy_groups = list(read.lazy_groups)
    report.definition_ref = read.definition_ref

    migration = manifest["migrations"][read.migration_id]
    definition = store.registry.get(
        migration["definition_name"], int(migration["definition_version"])
    )

    seen_keys = set()
    for row in read.rows:
        key = str(row["key"])
        seen_keys.add(key)
        prefix = f"row {key!r}"

        if int(row["snapshot_id"]) != sid:
            report.fail(f"{prefix}: row snapshot {row['snapshot_id']} != {sid}")
        if row["definition_name"] != read.definition_name:
            report.fail(f"{prefix}: wrong definition name")
        if int(row["definition_version"]) != read.definition_version:
            report.fail(
                f"{prefix}: definition version {row['definition_version']} "
                f"!= declared {read.definition_version}"
            )
        if row["definition_ref"] != read.definition_ref:
            report.fail(f"{prefix}: inconsistent definition_ref")

        if key not in source_values_by_key:
            report.fail(f"{prefix}: missing from verified source chunks")
            continue
        group_id, source_row = source_values_by_key[key]
        if row["group_id"] != group_id:
            report.fail(f"{prefix}: group mismatch")
        expected_source_version = int(source_row["source_version"])
        if int(row["source_version"]) != expected_source_version:
            report.fail(
                f"{prefix}: source version {row['source_version']} != "
                f"source chunk {expected_source_version}"
            )

        try:
            expected_value = definition.compute(source_row["values"])
        except Exception as exc:
            report.fail(f"{prefix}: definition computation failed: {exc}")
            continue
        if row["value"] != expected_value:
            report.fail(
                f"{prefix}: value {row['value']!r} does not equal declared "
                f"{read.definition_ref} result {expected_value!r}"
            )

        if row["materialized"]:
            group = manifest["groups"][row["group_id"]]
            bf = group.get("backfills", {}).get(read.migration_id)
            if not bf or bf.get("state") != "materialized":
                report.fail(f"{prefix}: marked materialized without manifest chunk")
                continue
            bf_versions = {
                str(k): int(v)
                for k, v in bf.get("source_versions", {}).items()
            }
            if key not in bf_versions:
                report.fail(f"{prefix}: backfill metadata lacks current version")
            elif bf_versions[key] != expected_source_version:
                report.fail(
                    f"{prefix}: materialized source version "
                    f"{bf_versions[key]} != {expected_source_version}"
                )
            try:
                computed_payload = verify_ref(store.root, bf["file"])
                if computed_payload["values"].get(key) != expected_value:
                    report.fail(f"{prefix}: materialized chunk has wrong value")
            except Exception as exc:
                report.fail(f"{prefix}: computed chunk verification failed: {exc}")

    if seen_keys != set(source_values_by_key):
        missing = sorted(set(source_values_by_key) - seen_keys)
        extra = sorted(seen_keys - set(source_values_by_key))
        report.fail(f"row coverage mismatch missing={missing} extra={extra}")

    # Every row in a snapshot must share the same declared definition version,
    # while source versions may differ and are carried per row.
    refs = {row["definition_ref"] for row in read.rows}
    if read.rows and refs != {read.definition_ref}:
        report.fail(f"mixed definition refs inside one snapshot: {sorted(refs)}")

    report.ok = not report.errors
    return report


def verify_all_snapshots(
    store: Any, *, column: Optional[str] = None
) -> AllSnapshotsReport:
    pointer = store._read_pointer()
    current_id = int(pointer["current_snapshot_id"])
    manifest_dir = store.root / "manifests"
    reports: List[SnapshotVerificationReport] = []
    errors: List[str] = []
    total_rows = 0

    paths = sorted(manifest_dir.glob("snapshot-*.json"))
    for path in paths:
        try:
            sid = int(path.stem.split("-")[-1])
        except ValueError:
            continue
        if sid > current_id:
            # Final but never pointed-to manifest after a simulated cut.
            continue
        report = verify_snapshot(store, sid, column=column)
        reports.append(report)
        total_rows += report.row_count
        if not report.ok:
            errors.extend(f"snapshot {sid}: {err}" for err in report.errors)

    if not reports:
        errors.append("no committed snapshots found")
    ok = not errors
    return AllSnapshotsReport(
        ok=ok,
        snapshot_count=len(reports),
        rows_checked=total_rows,
        reports=reports,
        errors=errors,
    )
