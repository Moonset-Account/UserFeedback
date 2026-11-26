"""Durable column-partition migration store."""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from .crash import CrashController
from .definitions import DefinitionRegistry
from .errors import (
    BackfillConflict,
    DuplicateKeyError,
    InvalidMigrationStateError,
    RowNotFoundError,
    SnapshotNotFoundError,
    StoreNotFoundError,
)
from .storage import (
    atomic_write_json,
    read_json,
    safe_group_id,
    verify_ref,
)

MANIFEST_FORMAT = "column-migration-manifest"
CURRENT_FORMAT = "column-migration-current"
SOURCE_FORMAT = "column-migration-source-chunk"
COMPUTED_FORMAT = "column-migration-computed-chunk"


@dataclass
class BackfillJob:
    """A read-only backfill attempt and its optimistic row-version basis."""

    column: str
    group_id: str
    migration_id: str
    base_snapshot_id: int
    definition_name: str
    definition_version: int
    read_versions: Dict[str, int]
    computed_ref: Dict[str, Any]
    values: Dict[str, Any] = field(default_factory=dict)

    def to_token(self) -> Dict[str, Any]:
        """JSON-serializable token for a background worker restart."""

        return {
            "column": self.column,
            "group_id": self.group_id,
            "migration_id": self.migration_id,
            "base_snapshot_id": self.base_snapshot_id,
            "definition_name": self.definition_name,
            "definition_version": self.definition_version,
            "read_versions": dict(self.read_versions),
            "computed_ref": dict(self.computed_ref),
            "values": dict(self.values),
        }

    @classmethod
    def from_token(cls, token: Mapping[str, Any]) -> "BackfillJob":
        return cls(
            column=str(token["column"]),
            group_id=str(token["group_id"]),
            migration_id=str(token["migration_id"]),
            base_snapshot_id=int(token["base_snapshot_id"]),
            definition_name=str(token["definition_name"]),
            definition_version=int(token["definition_version"]),
            read_versions={str(k): int(v) for k, v in token["read_versions"].items()},
            computed_ref=dict(token["computed_ref"]),
            values=dict(token.get("values", {})),
        )


@dataclass
class SnapshotRead:
    snapshot_id: int
    column: str
    migration_id: str
    definition_name: str
    definition_version: int
    rows: List[Dict[str, Any]]
    materialized_groups: List[str]
    lazy_groups: List[str]

    @property
    def definition_ref(self) -> str:
        return f"{self.definition_name}@v{self.definition_version}"

    def by_key(self) -> Dict[str, Dict[str, Any]]:
        return {str(row["key"]): row for row in self.rows}

    def get(self, key: str) -> Dict[str, Any]:
        return self.by_key()[key]

    def values(self) -> Dict[str, Any]:
        return {str(row["key"]): row["value"] for row in self.rows}


class ColumnMigrationStore:
    """A small file-backed snapshot store for column/group migrations."""

    def __init__(
        self,
        root: str | Path,
        *,
        registry: Optional[DefinitionRegistry] = None,
        crash: Optional[CrashController] = None,
    ) -> None:
        self.root = Path(root)
        self.registry = registry or DefinitionRegistry.with_score_versions()
        self.crash = crash

    # ------------------------------------------------------------------
    # Construction and manifest plumbing
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        registry: Optional[DefinitionRegistry] = None,
        crash: Optional[CrashController] = None,
    ) -> "ColumnMigrationStore":
        root_path = Path(root)
        (root_path / "data").mkdir(parents=True, exist_ok=True)
        (root_path / "manifests").mkdir(parents=True, exist_ok=True)
        store = cls(root_path, registry=registry, crash=crash)
        pointer_path = root_path / "current.json"
        if pointer_path.exists():
            raise FileExistsError(f"store already exists: {root_path}")

        manifest: Dict[str, Any] = {
            "format": MANIFEST_FORMAT,
            "format_version": 1,
            "snapshot_id": 1,
            "parent_id": None,
            "last_operation": "create",
            "columns": {},
            "migrations": {},
            "groups": {},
        }
        atomic_write_json(
            store._manifest_path(1),
            manifest,
            crash=crash,
            barrier_prefix="manifest",
        )
        pointer = {
            "format": CURRENT_FORMAT,
            "current_snapshot_id": 1,
            "manifest": store._manifest_rel(1),
        }
        atomic_write_json(pointer_path, pointer, crash=crash, barrier_prefix="current")
        if crash is not None:
            crash.check("create_complete", snapshot_id=1)
        return store

    @classmethod
    def open(
        cls,
        root: str | Path,
        *,
        registry: Optional[DefinitionRegistry] = None,
        crash: Optional[CrashController] = None,
    ) -> "ColumnMigrationStore":
        root_path = Path(root)
        pointer_path = root_path / "current.json"
        if not pointer_path.exists():
            raise StoreNotFoundError(f"no committed current.json in {root_path}")
        pointer = read_json(pointer_path)
        if pointer.get("format") != CURRENT_FORMAT:
            raise StoreNotFoundError(f"unexpected current pointer format in {root_path}")
        store = cls(root_path, registry=registry, crash=crash)
        # Ensure the committed manifest is readable before returning.
        store.load_manifest()
        return store

    def _manifest_rel(self, snapshot_id: int) -> str:
        return f"manifests/snapshot-{snapshot_id:06d}.json"

    def _manifest_path(self, snapshot_id: int) -> Path:
        return self.root / self._manifest_rel(snapshot_id)

    def _read_pointer(self) -> Dict[str, Any]:
        pointer = read_json(self.root / "current.json")
        if pointer.get("format") != CURRENT_FORMAT:
            raise StoreNotFoundError("current.json has an unsupported format")
        return pointer

    @property
    def current_snapshot_id(self) -> int:
        return int(self._read_pointer()["current_snapshot_id"])

    def load_manifest(self, snapshot_id: Optional[int] = None) -> Dict[str, Any]:
        pointer = self._read_pointer()
        current_id = int(pointer["current_snapshot_id"])
        if snapshot_id is None:
            snapshot_id = current_id
        if snapshot_id > current_id or snapshot_id < 1:
            raise SnapshotNotFoundError(
                f"snapshot {snapshot_id} is not committed; current={current_id}"
            )
        path = self._manifest_path(snapshot_id)
        if not path.exists():
            raise SnapshotNotFoundError(
                f"snapshot {snapshot_id} manifest was pruned or is missing"
            )
        manifest = read_json(path)
        if int(manifest.get("snapshot_id", -1)) != snapshot_id:
            from .errors import CorruptChunkError

            raise CorruptChunkError(f"manifest id mismatch at {path}")
        return manifest

    def _commit(
        self, manifest: Dict[str, Any], *, operation: str
    ) -> Dict[str, Any]:
        pointer = self._read_pointer()
        parent_id = int(pointer["current_snapshot_id"])
        new_id = parent_id + 1
        manifest = copy.deepcopy(manifest)
        manifest["snapshot_id"] = new_id
        manifest["parent_id"] = parent_id
        manifest["last_operation"] = operation

        rel = self._manifest_rel(new_id)
        atomic_write_json(
            self.root / rel,
            manifest,
            crash=self.crash,
            barrier_prefix="manifest",
        )
        if self.crash is not None:
            self.crash.check(
                "after_manifest_write",
                snapshot_id=new_id,
                operation=operation,
            )

        new_pointer = {
            "format": CURRENT_FORMAT,
            "current_snapshot_id": new_id,
            "manifest": rel,
            "parent_snapshot_id": parent_id,
        }
        if self.crash is not None:
            self.crash.check(
                "before_manifest_switch",
                snapshot_id=new_id,
                operation=operation,
            )
        atomic_write_json(
            self.root / "current.json",
            new_pointer,
            crash=self.crash,
            barrier_prefix="current",
        )
        if self.crash is not None:
            self.crash.check(
                "after_manifest_switch",
                snapshot_id=new_id,
                operation=operation,
            )
        return manifest

    def _write_chunk(
        self,
        *,
        kind: str,
        group_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        safe_id = safe_group_id(group_id)
        rel = f"data/{kind}-{safe_id}-{uuid.uuid4().hex}.json"
        digest, size = atomic_write_json(
            self.root / rel,
            payload,
            crash=self.crash,
            barrier_prefix=f"{kind}_data",
        )
        return {"path": rel, "sha256": digest, "size": size}

    # ------------------------------------------------------------------
    # Source imports, late updates, and whole-group rewrites
    # ------------------------------------------------------------------

    @staticmethod
    def _row_key(raw: Mapping[str, Any]) -> str:
        if "id" not in raw:
            raise ValueError("every row must contain an 'id' field")
        return str(raw["id"])

    def _existing_source_rows(
        self, group_state: Optional[Mapping[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        if not group_state or not group_state.get("source"):
            return {}
        payload = verify_ref(self.root, group_state["source"])
        return {str(row["key"]): dict(row) for row in payload.get("rows", [])}

    def import_rows(self, group_id: str, rows: Sequence[Mapping[str, Any]]) -> int:
        """Insert a batch of new keys into a group.

        New rows start at source version 1.  Importing an existing key is an
        error; late changes use :meth:`update_rows`.
        """

        safe_group_id(group_id)
        manifest = self.load_manifest()
        groups = manifest.setdefault("groups", {})
        group = groups.setdefault(
            group_id,
            {"source": None, "source_versions": {}, "backfills": {}},
        )
        stored = self._existing_source_rows(group)

        for raw in rows:
            key = self._row_key(raw)
            if key in stored:
                raise DuplicateKeyError(f"key {key!r} already exists in {group_id}")
            values = {k: v for k, v in dict(raw).items() if k != "id"}
            stored[key] = {
                "key": key,
                "values": values,
                "source_version": 1,
            }

        ordered_rows = [stored[key] for key in sorted(stored)]
        payload = {
            "format": SOURCE_FORMAT,
            "format_version": 1,
            "group_id": group_id,
            "rows": ordered_rows,
        }
        ref = self._write_chunk(kind="source", group_id=group_id, payload=payload)
        if self.crash is not None:
            self.crash.check(
                "after_source_data_write",
                group_id=group_id,
                rows=len(ordered_rows),
                path=ref["path"],
            )

        group["source"] = ref
        group["source_versions"] = {
            str(row["key"]): int(row["source_version"]) for row in ordered_rows
        }
        snapshot = self._commit(manifest, operation="import_rows")
        return int(snapshot["snapshot_id"])

    @staticmethod
    def _normalize_updates(
        updates: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    ) -> List[Tuple[str, Dict[str, Any]]]:
        normalized: List[Tuple[str, Dict[str, Any]]] = []
        if isinstance(updates, Mapping):
            for key, patch in updates.items():
                normalized.append((str(key), dict(patch)))
            return normalized
        for raw in updates:
            key = str(raw["id"])
            patch = {k: v for k, v in dict(raw).items() if k != "id"}
            normalized.append((key, patch))
        return normalized

    def update_rows(
        self,
        updates: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
        *,
        group_id: Optional[str] = None,
    ) -> int:
        """Apply late partial updates and increment changed row versions."""

        normalized = self._normalize_updates(updates)
        manifest = self.load_manifest()
        groups = manifest.setdefault("groups", {})

        key_to_group: Dict[str, str] = {}
        for gid, group in groups.items():
            for key in group.get("source_versions", {}):
                key_to_group[str(key)] = gid

        by_group: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
        for key, patch in normalized:
            gid = group_id or key_to_group.get(key)
            if gid is None:
                raise RowNotFoundError(f"cannot locate existing row {key!r}")
            if group_id is not None and key not in groups.get(group_id, {}).get(
                "source_versions", {}
            ):
                raise RowNotFoundError(
                    f"row {key!r} is not in requested group {group_id!r}"
                )
            by_group.setdefault(gid, []).append((key, patch))

        for gid in sorted(by_group):
            group = groups[gid]
            stored = self._existing_source_rows(group)
            for key, patch in by_group[gid]:
                if key not in stored:
                    raise RowNotFoundError(f"unknown row {key!r}")
                row = stored[key]
                old_values = dict(row["values"])
                new_values = dict(old_values)
                new_values.update(patch)
                changed = new_values != old_values
                row["values"] = new_values
                if changed:
                    row["source_version"] = int(row["source_version"]) + 1

            ordered_rows = [stored[key] for key in sorted(stored)]
            payload = {
                "format": SOURCE_FORMAT,
                "format_version": 1,
                "group_id": gid,
                "rows": ordered_rows,
            }
            ref = self._write_chunk(kind="source", group_id=gid, payload=payload)
            if self.crash is not None:
                self.crash.check(
                    "after_source_data_write",
                    group_id=gid,
                    updated_keys=len(by_group[gid]),
                    path=ref["path"],
                )
            group["source"] = ref
            group["source_versions"] = {
                str(row["key"]): int(row["source_version"]) for row in ordered_rows
            }

        snapshot = self._commit(manifest, operation="update_rows")
        return int(snapshot["snapshot_id"])

    def rewrite_group(
        self,
        group_id: str,
        rows: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> int:
        """Rewrite an entire group.

        A physical rewrite assigns a fresh source version to every retained or
        replacement row, so any backfill that read the old row-version map
        loses its compare-and-publish attempt.
        """

        safe_group_id(group_id)
        manifest = self.load_manifest()
        groups = manifest.setdefault("groups", {})
        if group_id not in groups:
            raise RowNotFoundError(f"group {group_id!r} does not exist")
        group = groups[group_id]
        old_versions = {
            str(k): int(v) for k, v in group.get("source_versions", {}).items()
        }

        new_stored: Dict[str, Dict[str, Any]] = {}
        if rows is None:
            old_rows = self._existing_source_rows(group)
            for key, row in old_rows.items():
                new_stored[key] = {
                    "key": key,
                    "values": dict(row["values"]),
                    "source_version": int(row["source_version"]) + 1,
                }
        else:
            for raw in rows:
                key = self._row_key(raw)
                values = {k: v for k, v in dict(raw).items() if k != "id"}
                new_stored[key] = {
                    "key": key,
                    "values": values,
                    # Explicit whole-group rewrite is itself a new source
                    # generation, even if values happen to be identical.
                    "source_version": old_versions.get(key, 0) + 1,
                }

        ordered_rows = [new_stored[key] for key in sorted(new_stored)]
        payload = {
            "format": SOURCE_FORMAT,
            "format_version": 1,
            "group_id": group_id,
            "whole_group_rewrite": True,
            "rows": ordered_rows,
        }
        ref = self._write_chunk(kind="source", group_id=group_id, payload=payload)
        if self.crash is not None:
            self.crash.check(
                "after_source_data_write",
                group_id=group_id,
                whole_group_rewrite=True,
                path=ref["path"],
            )
        group["source"] = ref
        group["source_versions"] = {
            str(row["key"]): int(row["source_version"]) for row in ordered_rows
        }
        snapshot = self._commit(manifest, operation="rewrite_group")
        return int(snapshot["snapshot_id"])

    # ------------------------------------------------------------------
    # Computed-column declarations, upgrades, undo
    # ------------------------------------------------------------------

    def _resolve_column(
        self, manifest: Mapping[str, Any], column: Optional[str]
    ) -> Tuple[str, str, Dict[str, Any]]:
        columns = manifest.get("columns", {})
        if column is None:
            if len(columns) != 1:
                raise InvalidMigrationStateError(
                    "column name is required when zero or multiple columns exist"
                )
            column = next(iter(columns))
        if column not in columns:
            raise InvalidMigrationStateError(f"no migration declared for column {column!r}")
        migration_id = str(columns[column]["active_migration_id"])
        migration = manifest["migrations"][migration_id]
        return column, migration_id, dict(migration)

    def add_computed_column(
        self,
        column: str,
        definition_version: int,
        *,
        definition_name: Optional[str] = None,
        migration_id: Optional[str] = None,
    ) -> str:
        """Declare a new computed column and its fixed definition version."""

        manifest = self.load_manifest()
        if column in manifest.get("columns", {}):
            raise InvalidMigrationStateError(
                f"column {column!r} already exists; use upgrade_definition()"
            )
        def_name = definition_name or column
        # Fail fast if this process cannot compute the declared version.
        self.registry.get(def_name, int(definition_version))
        new_id = self.current_snapshot_id + 1
        mid = migration_id or f"migration-{column}-v{definition_version}-s{new_id}"
        if mid in manifest.get("migrations", {}):
            raise InvalidMigrationStateError(f"migration id {mid!r} already exists")

        manifest.setdefault("columns", {})[column] = {
            "active_migration_id": mid,
        }
        manifest.setdefault("migrations", {})[mid] = {
            "migration_id": mid,
            "column": column,
            "definition_name": def_name,
            "definition_version": int(definition_version),
            "state": "active",
            "created_in_snapshot": new_id,
        }
        committed = self._commit(manifest, operation="add_computed_column")
        return str(committed["columns"][column]["active_migration_id"])

    def upgrade_definition(
        self,
        column: str,
        new_version: int,
        *,
        definition_name: Optional[str] = None,
        migration_id: Optional[str] = None,
    ) -> str:
        """Declare a new active definition version without rewriting history.

        Old manifests continue to name their old migration/version.  In the new
        snapshot, old materialized chunks are treated as stale-definition data
        and readers lazily use the new declared version until v2 backfill is
        published.
        """

        manifest = self.load_manifest()
        if column not in manifest.get("columns", {}):
            raise InvalidMigrationStateError(f"column {column!r} does not exist")
        old_mid = manifest["columns"][column]["active_migration_id"]
        old_migration = manifest["migrations"][old_mid]
        def_name = definition_name or str(old_migration["definition_name"])
        self.registry.get(def_name, int(new_version))
        new_id = self.current_snapshot_id + 1
        mid = migration_id or f"migration-{column}-v{new_version}-s{new_id}"
        if mid in manifest["migrations"]:
            raise InvalidMigrationStateError(f"migration id {mid!r} already exists")
        if (
            def_name == old_migration["definition_name"]
            and int(new_version) == int(old_migration["definition_version"])
        ):
            raise InvalidMigrationStateError(
                "refusing to upgrade to the same active definition version"
            )

        manifest["migrations"][mid] = {
            "migration_id": mid,
            "column": column,
            "definition_name": def_name,
            "definition_version": int(new_version),
            "state": "active",
            "upgraded_from_migration_id": old_mid,
            "created_in_snapshot": new_id,
        }
        manifest["columns"][column]["active_migration_id"] = mid
        self._commit(manifest, operation="upgrade_definition")
        return mid

    def undo_group_migration(
        self,
        group_id: str,
        *,
        column: Optional[str] = None,
        migration_id: Optional[str] = None,
    ) -> int:
        """Mark one group's migration undone in a new snapshot.

        The new manifest does not use the published chunk.  The prior manifest
        and its file remain available to old snapshots until an explicit
        snapshot prune/reference GC removes that history.
        """

        manifest = self.load_manifest()
        if migration_id is None:
            column, migration_id, migration = self._resolve_column(manifest, column)
        else:
            migration = dict(manifest["migrations"][migration_id])
            column = str(migration["column"])
        if group_id not in manifest.get("groups", {}):
            raise RowNotFoundError(f"group {group_id!r} does not exist")

        group = manifest["groups"][group_id]
        backfills = group.setdefault("backfills", {})
        backfills[migration_id] = {
            "state": "undone",
            "definition_name": migration["definition_name"],
            "definition_version": int(migration["definition_version"]),
            "undone": True,
        }
        committed = self._commit(manifest, operation="undo_group_migration")
        return int(committed["snapshot_id"])

    # ------------------------------------------------------------------
    # Background backfill preparation/publication
    # ------------------------------------------------------------------

    def prepare_backfill(
        self,
        group_id: str,
        *,
        column: Optional[str] = None,
        snapshot_id: Optional[int] = None,
    ) -> BackfillJob:
        """Read one snapshot/group and durably write its computed chunk.

        The returned job remembers exactly the row versions read.  No manifest
        is switched by preparation.
        """

        manifest = self.load_manifest(snapshot_id)
        column, migration_id, migration = self._resolve_column(manifest, column)
        if group_id not in manifest.get("groups", {}):
            raise RowNotFoundError(f"group {group_id!r} does not exist")
        group = manifest["groups"][group_id]
        source_payload = verify_ref(self.root, group["source"])
        read_versions = {
            str(key): int(version)
            for key, version in group.get("source_versions", {}).items()
        }
        definition = self.registry.get(
            str(migration["definition_name"]),
            int(migration["definition_version"]),
        )

        values: Dict[str, Any] = {}
        for row in source_payload.get("rows", []):
            key = str(row["key"])
            payload_version = int(row["source_version"])
            if payload_version != read_versions.get(key):
                from .errors import CorruptChunkError

                raise CorruptChunkError(
                    f"source chunk/manifest version disagreement for {key}"
                )
            values[key] = definition.compute(row["values"])

        payload = {
            "format": COMPUTED_FORMAT,
            "format_version": 1,
            "column": column,
            "group_id": group_id,
            "migration_id": migration_id,
            "definition_name": migration["definition_name"],
            "definition_version": int(migration["definition_version"]),
            "base_snapshot_id": int(manifest["snapshot_id"]),
            "source_versions": read_versions,
            "values": values,
        }
        ref = self._write_chunk(kind="computed", group_id=group_id, payload=payload)
        if self.crash is not None:
            self.crash.check(
                "after_computed_data_write",
                group_id=group_id,
                column=column,
                migration_id=migration_id,
                path=ref["path"],
                rows=len(values),
            )
        return BackfillJob(
            column=column,
            group_id=group_id,
            migration_id=migration_id,
            base_snapshot_id=int(manifest["snapshot_id"]),
            definition_name=str(migration["definition_name"]),
            definition_version=int(migration["definition_version"]),
            read_versions=read_versions,
            computed_ref=ref,
            values=values,
        )

    def publish_backfill(self, job: BackfillJob | Mapping[str, Any]) -> int:
        """Conditionally publish if and only if read row versions still match."""

        if isinstance(job, Mapping):
            job = BackfillJob.from_token(job)

        manifest = self.load_manifest()
        current_sid = int(manifest["snapshot_id"])
        if self.crash is not None:
            self.crash.check(
                "before_publish_cas",
                group_id=job.group_id,
                column=job.column,
                base_snapshot_id=job.base_snapshot_id,
                current_snapshot_id=current_sid,
            )

        group = manifest.get("groups", {}).get(job.group_id)
        current_versions = (
            {str(k): int(v) for k, v in group.get("source_versions", {}).items()}
            if group is not None
            else {}
        )
        if current_versions != dict(job.read_versions):
            raise BackfillConflict(
                group_id=job.group_id,
                column=job.column,
                read_versions=job.read_versions,
                current_versions=current_versions,
                current_snapshot_id=current_sid,
            )
        if job.migration_id not in manifest.get("migrations", {}):
            raise InvalidMigrationStateError(
                f"migration {job.migration_id!r} is absent from current manifest"
            )

        group = manifest["groups"][job.group_id]
        group.setdefault("backfills", {})[job.migration_id] = {
            "state": "materialized",
            "file": dict(job.computed_ref),
            "definition_name": job.definition_name,
            "definition_version": int(job.definition_version),
            "source_versions": dict(job.read_versions),
            "base_snapshot_id": job.base_snapshot_id,
        }
        committed = self._commit(manifest, operation="publish_backfill")
        return int(committed["snapshot_id"])

    # ------------------------------------------------------------------
    # Snapshot reads
    # ------------------------------------------------------------------

    def read_snapshot(
        self,
        snapshot_id: Optional[int] = None,
        *,
        column: Optional[str] = None,
    ) -> SnapshotRead:
        manifest = self.load_manifest(snapshot_id)
        snapshot_id = int(manifest["snapshot_id"])
        column, migration_id, migration = self._resolve_column(manifest, column)
        definition_name = str(migration["definition_name"])
        definition_version = int(migration["definition_version"])
        definition = self.registry.get(definition_name, definition_version)

        rows: List[Dict[str, Any]] = []
        materialized_groups: List[str] = []
        lazy_groups: List[str] = []

        for group_id in sorted(manifest.get("groups", {})):
            group = manifest["groups"][group_id]
            source_payload = verify_ref(self.root, group["source"])
            source_rows = {
                str(row["key"]): row for row in source_payload.get("rows", [])
            }
            manifest_versions = {
                str(k): int(v)
                for k, v in group.get("source_versions", {}).items()
            }
            bf = group.get("backfills", {}).get(migration_id)
            usable = False
            fallback_reason = "pending"
            computed_values: Dict[str, Any] = {}

            if bf is None:
                fallback_reason = "pending"
            elif bf.get("state") == "undone":
                fallback_reason = "undone"
            elif bf.get("state") != "materialized":
                fallback_reason = str(bf.get("state", "not_materialized"))
            elif str(bf.get("definition_name")) != definition_name or int(
                bf.get("definition_version", -1)
            ) != definition_version:
                fallback_reason = "stale_definition_version"
            elif {
                str(k): int(v)
                for k, v in bf.get("source_versions", {}).items()
            } != manifest_versions:
                fallback_reason = "stale_source_version"
            else:
                computed_payload = verify_ref(self.root, bf["file"])
                if computed_payload.get("format") != COMPUTED_FORMAT:
                    from .errors import CorruptChunkError

                    raise CorruptChunkError(
                        f"bad computed chunk format in group {group_id}"
                    )
                if {
                    str(k): int(v)
                    for k, v in computed_payload.get("source_versions", {}).items()
                } != manifest_versions:
                    fallback_reason = "stale_source_version"
                elif int(computed_payload.get("definition_version", -1)) != definition_version:
                    fallback_reason = "stale_definition_version"
                else:
                    usable = True
                    computed_values = dict(computed_payload.get("values", {}))

            if usable:
                materialized_groups.append(group_id)
            else:
                lazy_groups.append(group_id)

            for key in sorted(source_rows):
                source_row = source_rows[key]
                source_version = int(source_row["source_version"])
                if source_version != manifest_versions.get(key):
                    from .errors import CorruptChunkError

                    raise CorruptChunkError(
                        f"source version mismatch for {key} in snapshot {snapshot_id}"
                    )
                if usable:
                    if key not in computed_values:
                        from .errors import CorruptChunkError

                        raise CorruptChunkError(
                            f"materialized chunk lacks key {key!r} for {group_id}"
                        )
                    value = computed_values[key]
                else:
                    value = definition.compute(source_row["values"])

                rows.append(
                    {
                        "key": key,
                        "group_id": group_id,
                        "column": column,
                        "value": value,
                        "snapshot_id": snapshot_id,
                        "migration_id": migration_id,
                        "definition_name": definition_name,
                        "definition_version": definition_version,
                        "definition_ref": f"{definition_name}@v{definition_version}",
                        "source_version": source_version,
                        "materialized": usable,
                        "read_mode": "materialized" if usable else "declared_definition",
                        "fallback_reason": None if usable else fallback_reason,
                    }
                )

        return SnapshotRead(
            snapshot_id=snapshot_id,
            column=column,
            migration_id=migration_id,
            definition_name=definition_name,
            definition_version=definition_version,
            rows=rows,
            materialized_groups=materialized_groups,
            lazy_groups=lazy_groups,
        )

    def explain_row(
        self,
        key: str,
        *,
        snapshot_id: Optional[int] = None,
        column: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.read_snapshot(snapshot_id, column=column).get(key)

    # ------------------------------------------------------------------
    # Retention and old-file reclamation
    # ------------------------------------------------------------------

    def pin_snapshot(self, snapshot_id: int) -> List[int]:
        self.load_manifest(snapshot_id)
        pins_path = self.root / "pins.json"
        pins = {"pinned": []}
        if pins_path.exists():
            pins = read_json(pins_path)
        pinned = {int(x) for x in pins.get("pinned", [])}
        pinned.add(int(snapshot_id))
        value = {"pinned": sorted(pinned)}
        atomic_write_json(pins_path, value, crash=self.crash, barrier_prefix="pins")
        return value["pinned"]

    def unpin_snapshot(self, snapshot_id: int) -> List[int]:
        pins_path = self.root / "pins.json"
        pinned: Set[int] = set()
        if pins_path.exists():
            pins = read_json(pins_path)
            pinned = {int(x) for x in pins.get("pinned", [])}
        pinned.discard(int(snapshot_id))
        value = {"pinned": sorted(pinned)}
        atomic_write_json(pins_path, value, crash=self.crash, barrier_prefix="pins")
        return value["pinned"]

    def _pinned_snapshot_ids(self) -> Set[int]:
        pins_path = self.root / "pins.json"
        if not pins_path.exists():
            return set()
        pins = read_json(pins_path)
        return {int(x) for x in pins.get("pinned", [])}

    def prune_snapshots(
        self,
        *,
        keep_latest: int = 1,
        retain_ids: Optional[Iterable[int]] = None,
    ) -> List[str]:
        """Delete old manifest files not protected by pins or arguments.

        This never changes ``current.json``.  Data files are not unlinked here;
        call :meth:`reclaim_unreferenced_files` after pruning.
        """

        current_id = self.current_snapshot_id
        retain: Set[int] = set(self._pinned_snapshot_ids())
        if retain_ids:
            retain.update(int(x) for x in retain_ids)
        keep_latest = max(1, int(keep_latest))
        retain.update(
            range(max(1, current_id - keep_latest + 1), current_id + 1)
        )

        deleted: List[str] = []
        manifests_dir = self.root / "manifests"
        for path in sorted(manifests_dir.glob("snapshot-*.json")):
            try:
                sid = int(path.stem.split("-")[-1])
            except ValueError:
                continue
            if sid == current_id or sid in retain:
                continue
            path.unlink()
            deleted.append(path.name)
        if deleted:
            from .storage import fsync_directory

            fsync_directory(manifests_dir)
        return deleted

    def _committed_manifest_files_for_gc(self) -> List[Path]:
        current_id = self.current_snapshot_id
        result: List[Path] = []
        for path in sorted((self.root / "manifests").glob("snapshot-*.json")):
            try:
                sid = int(path.stem.split("-")[-1])
            except ValueError:
                continue
            # An id above current pointer is an uncommitted/orphan manifest.
            if sid <= current_id:
                result.append(path)
        return result

    @staticmethod
    def _collect_data_paths(value: Any, found: Set[str]) -> None:
        if isinstance(value, Mapping):
            path_value = value.get("path")
            if isinstance(path_value, str) and path_value.startswith("data/"):
                found.add(path_value)
            for child in value.values():
                ColumnMigrationStore._collect_data_paths(child, found)
        elif isinstance(value, list):
            for child in value:
                ColumnMigrationStore._collect_data_paths(child, found)

    def referenced_data_files(self) -> Set[str]:
        referenced: Set[str] = set()
        for manifest_path in self._committed_manifest_files_for_gc():
            manifest = read_json(manifest_path)
            self._collect_data_paths(manifest, referenced)
        return referenced

    def reclaim_unreferenced_files(self) -> List[str]:
        """Unlink temp/orphan data files not referenced by committed manifests.

        Files needed by any retained committed snapshot are protected.  Thus
        old source/computed chunks survive while old snapshots survive.
        """

        referenced = self.referenced_data_files()
        data_dir = self.root / "data"
        candidates: List[Path] = []
        if data_dir.exists():
            for path in sorted(data_dir.iterdir(), key=lambda p: p.name):
                if not path.is_file():
                    continue
                rel = f"data/{path.name}"
                is_temp = path.name.startswith(".") or ".tmp." in path.name
                if is_temp or rel not in referenced:
                    candidates.append(path)

        if self.crash is not None:
            self.crash.check(
                "before_reclaim",
                candidate_count=len(candidates),
                referenced_count=len(referenced),
            )

        removed: List[str] = []
        for path in candidates:
            rel = f"data/{path.name}"
            if self.crash is not None:
                self.crash.check("before_reclaim_file", path=rel)
            path.unlink()
            if self.crash is not None:
                self.crash.check("after_unlink_before_dir_fsync", path=rel)
            from .storage import fsync_directory

            fsync_directory(data_dir)
            removed.append(rel)
            if self.crash is not None:
                self.crash.check("after_reclaim_file", path=rel)

        if self.crash is not None:
            self.crash.check("after_reclaim", removed_count=len(removed))
        return removed

    # ------------------------------------------------------------------
    # Verification convenience
    # ------------------------------------------------------------------

    def verify(self, snapshot_id: Optional[int] = None, *, column: Optional[str] = None):
        from .verification import verify_snapshot

        return verify_snapshot(self, snapshot_id, column=column)

    def verify_all_snapshots(self, *, column: Optional[str] = None):
        from .verification import verify_all_snapshots

        return verify_all_snapshots(self, column=column)

    def finish_and_verify_after_crash(self) -> Any:
        """Convenience recovery routine used by crash tests."""

        removed = self.reclaim_unreferenced_files()
        report = self.verify_all_snapshots()
        report.reclaimed = removed  # type: ignore[attr-defined]
        return report
