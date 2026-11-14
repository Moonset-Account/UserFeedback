"""Evaluation engine turning parsed builds into archive plans.

Two kinds of decisions are made here:

1. Per-build validation.  A failed check blocks that build batch only; other
   batches keep publishing.  Examples: main program / symbol debug identifiers
   disagree, a declared file is missing, an index contains unsafe paths.
2. Cross-build symbol comparison.  Symbol files sharing an architecture and a
   debug identifier are merged into one internal archive entry when their
   content hashes agree, and declared a conflict (blocking every batch that
   contributed a different hash) when they do not.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .errors import UnsafePathError
from .models import (
    PUBLIC_ROLES,
    SYMBOL_ROLE,
    Artifact,
    Build,
    KNOWN_ROLES,
)
from .safety import resolve_artifact

SCHEMA_VERSION = "1.0"

# Reason codes used inside the block manifest.  Keep these stable: downstream
# tooling filters on them.
REASON_ENTRY_ERROR = "invalid_artifact_entry"
REASON_UNKNOWN_ROLE = "unknown_role"
REASON_UNSAFE_PATH = "unsafe_path"
REASON_MISSING_FILE = "missing_file"
REASON_DUPLICATE_PATH = "duplicate_path"
REASON_MAIN_DEBUG_ID = "main_program_debug_id_required"
REASON_SYMBOL_DEBUG_ID = "debug_symbol_debug_id_required"
REASON_DEBUG_ID_MISMATCH = "debug_id_mismatch"
REASON_MISSING_MAIN = "missing_main_program"
REASON_SYMBOL_CONFLICT = "symbol_content_conflict"
REASON_DUPLICATE_SYMBOL = "duplicate_symbol_debug_id_in_build"


@dataclass
class EvaluatedBuild:
    build: Build
    blocked: bool = False
    reasons: list[dict] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)

    def add_reason(self, code: str, message: str, **details: object) -> None:
        self.reasons.append({"code": code, "message": message, **details})


@dataclass
class SymbolContribution:
    """One symbol artifact offered by one build batch."""

    build_id: str
    architecture: str
    artifact: Artifact


@dataclass
class TriageResult:
    public_plan: dict
    internal_plan: dict
    block_manifest: dict
    evaluated: list[EvaluatedBuild]
    fatal_error: Optional[str] = None

    @property
    def blocked_batches(self) -> list[EvaluatedBuild]:
        return [item for item in self.evaluated if item.blocked]


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evaluate(build: Build) -> EvaluatedBuild:
    """Run every per-build check and enrich artifacts with hashes/sizes."""

    result = EvaluatedBuild(build=build)
    for message in build.entry_errors:
        result.add_reason(REASON_ENTRY_ERROR, message)
    if result.reasons:
        result.blocked = True

    seen_paths: dict[str, Artifact] = {}
    main_ids: list[str] = []
    symbol_ids: list[str] = []
    symbol_id_counts: dict[str, int] = defaultdict(int)

    for artifact in build.artifacts:
        if artifact.role not in KNOWN_ROLES:
            result.blocked = True
            result.add_reason(
                REASON_UNKNOWN_ROLE,
                f"unknown artifact role {artifact.role!r}",
                path=artifact.rel_path,
            )
            # Still resolve/hash below if possible so diagnostics are complete.
        try:
            resolved = resolve_artifact(build.root, artifact.rel_path)
        except UnsafePathError as exc:
            result.blocked = True
            result.add_reason(
                REASON_UNSAFE_PATH,
                str(exc),
                path=artifact.rel_path,
            )
            continue
        if artifact.rel_path in seen_paths:
            result.blocked = True
            result.add_reason(
                REASON_DUPLICATE_PATH,
                f"path {artifact.rel_path!r} is declared more than once",
                path=artifact.rel_path,
            )
            continue
        if not resolved.is_file():
            result.blocked = True
            result.add_reason(
                REASON_MISSING_FILE,
                f"declared artifact does not exist or is not a regular file: "
                f"{artifact.rel_path!r}",
                path=artifact.rel_path,
            )
            continue
        artifact.abs_path = resolved
        artifact.size = resolved.stat().st_size
        artifact.sha256 = _hash_file(resolved)
        seen_paths[artifact.rel_path] = artifact
        result.artifacts.append(artifact)

        if artifact.role == "main_program":
            if artifact.debug_id is None:
                result.blocked = True
                result.add_reason(
                    REASON_MAIN_DEBUG_ID,
                    "main_program artifact requires a debug_id",
                    path=artifact.rel_path,
                )
            else:
                main_ids.append(artifact.debug_id)
        elif artifact.role == SYMBOL_ROLE:
            if artifact.debug_id is None:
                result.blocked = True
                result.add_reason(
                    REASON_SYMBOL_DEBUG_ID,
                    "debug_symbol artifact requires a debug_id",
                    path=artifact.rel_path,
                )
            else:
                symbol_ids.append(artifact.debug_id)
                symbol_id_counts[artifact.debug_id] += 1

    main_programs = [a for a in result.artifacts if a.role == "main_program"]
    if not main_programs:
        result.blocked = True
        result.add_reason(
            REASON_MISSING_MAIN,
            "build declares no main_program artifact",
        )

    # Main programs and symbols of one batch must share their debug identity.
    if main_ids and symbol_ids and set(main_ids) != set(symbol_ids):
        result.blocked = True
        result.add_reason(
            REASON_DEBUG_ID_MISMATCH,
            "main program and symbol debug identifiers do not match",
            main_program_debug_ids=sorted(main_ids),
            symbol_debug_ids=sorted(symbol_ids),
        )

    for debug_id, count in sorted(symbol_id_counts.items()):
        if count > 1:
            result.blocked = True
            result.add_reason(
                REASON_DUPLICATE_SYMBOL,
                f"debug identifier {debug_id!r} is used by {count} symbol "
                "files inside one build",
                debug_id=debug_id,
                count=count,
            )

    return result


def _public_file_entry(batch_key: str, artifact: Artifact) -> dict:
    return OrderedDict(
        path=artifact.rel_path,
        role=artifact.role,
        debug_id=artifact.debug_id,
        size=artifact.size,
        sha256=artifact.sha256,
        batch=batch_key,
    )


def _build_public_plan(evaluated: list[EvaluatedBuild]) -> dict:
    batches = []
    all_files: list[dict] = []
    for item in sorted(
        (e for e in evaluated if not e.blocked),
        key=lambda e: (e.build.build_id, e.build.architecture),
    ):
        key = f"{item.build.build_id}:{item.build.architecture}"
        public_artifacts = [
            a for a in item.artifacts if a.role in PUBLIC_ROLES
        ]
        files = [
            _public_file_entry(key, a)
            for a in sorted(public_artifacts, key=lambda a: a.rel_path)
        ]
        all_files.extend(files)
        batches.append(
            OrderedDict(
                build_id=item.build.build_id,
                architecture=item.build.architecture,
                file_count=len(files),
                files=files,
            )
        )
    return OrderedDict(
        schema_version=SCHEMA_VERSION,
        archive="public",
        description=(
            "Files required at runtime only. Debug symbol files are never "
            "listed here."
        ),
        summary=OrderedDict(
            batch_count=len(batches),
            file_count=len(all_files),
        ),
        batches=batches,
    )


def _build_internal_plan(
    symbol_groups: dict[tuple[str, str], list[SymbolContribution]],
    conflicted_keys: set[tuple[str, str]],
) -> dict:
    by_arch: dict[str, dict[str, dict]] = OrderedDict()
    merged_entries = 0
    for (architecture, debug_id), contributions in sorted(
        symbol_groups.items()
    ):
        if (architecture, debug_id) in conflicted_keys:
            continue
        hashes = sorted({c.artifact.sha256 for c in contributions})
        # Phase 1 guarantees one symbol per debug id per build, so a single
        # hash means every contributor carries identical content.
        canonical = sorted(
            contributions, key=lambda c: c.artifact.rel_path
        )[0].artifact
        references = sorted(
            {f"{c.build_id}:{architecture}" for c in contributions}
        )
        entry = OrderedDict(
            debug_id=debug_id,
            merged=len(contributions) > 1,
            sha256=hashes[0],
            size=canonical.size,
            canonical_path=canonical.rel_path,
            reference_count=len(references),
            references=references,
            sources=[
                OrderedDict(
                    build_id=c.build_id,
                    path=c.artifact.rel_path,
                    sha256=c.artifact.sha256,
                )
                for c in sorted(
                    contributions,
                    key=lambda c: (c.build_id, c.artifact.rel_path),
                )
            ],
        )
        if entry["merged"]:
            merged_entries += 1
        by_arch.setdefault(architecture, OrderedDict()).setdefault(
            "debug_ids", OrderedDict()
        )[debug_id] = entry

    architecture_section = []
    total_entries = 0
    for architecture in sorted(by_arch):
        debug_ids = by_arch[architecture]["debug_ids"]
        total_entries += len(debug_ids)
        architecture_section.append(
            OrderedDict(
                architecture=architecture,
                entry_count=len(debug_ids),
                debug_ids=debug_ids,
            )
        )
    return OrderedDict(
        schema_version=SCHEMA_VERSION,
        archive="internal",
        description=(
            "Debug symbol store organized by architecture and debug "
            "identifier. Identical symbols shared by builds are stored once "
            "and referenced by every contributing batch."
        ),
        summary=OrderedDict(
            architecture_count=len(architecture_section),
            symbol_entry_count=total_entries,
            merged_entry_count=merged_entries,
            conflict_count=len(conflicted_keys),
        ),
        architectures=architecture_section,
    )


def _build_manifest(
    evaluated: list[EvaluatedBuild],
    conflicted_keys: set[tuple[str, str]],
) -> dict:
    blocked = []
    for item in sorted(
        (e for e in evaluated if e.blocked),
        key=lambda e: (e.build.build_id, e.build.architecture),
    ):
        blocked.append(
            OrderedDict(
                build_id=item.build.build_id,
                architecture=item.build.architecture,
                reason_count=len(item.reasons),
                reasons=item.reasons,
            )
        )

    publishable_batches = []
    public_files: list[dict] = []
    symbol_files: list[dict] = []
    for item in sorted(
        (e for e in evaluated if not e.blocked),
        key=lambda e: (e.build.build_id, e.build.architecture),
    ):
        key = f"{item.build.build_id}:{item.build.architecture}"
        public = sorted(
            (a for a in item.artifacts if a.role in PUBLIC_ROLES),
            key=lambda a: a.rel_path,
        )
        symbols = sorted(
            (a for a in item.artifacts if a.role == SYMBOL_ROLE),
            key=lambda a: (a.debug_id or "", a.rel_path),
        )
        public_entries = [_public_file_entry(key, a) for a in public]
        symbol_entries = [
            OrderedDict(
                path=a.rel_path,
                debug_id=a.debug_id,
                architecture=item.build.architecture,
                sha256=a.sha256,
                size=a.size,
                batch=key,
            )
            for a in symbols
        ]
        public_files.extend(public_entries)
        symbol_files.extend(symbol_entries)
        publishable_batches.append(
            OrderedDict(
                build_id=item.build.build_id,
                architecture=item.build.architecture,
                public_file_count=len(public_entries),
                symbol_file_count=len(symbol_entries),
                public_files=public_entries,
                symbol_files=symbol_entries,
            )
        )

    conflicts = [
        OrderedDict(architecture=arch, debug_id=debug_id)
        for arch, debug_id in sorted(conflicted_keys)
    ]
    return OrderedDict(
        schema_version=SCHEMA_VERSION,
        summary=OrderedDict(
            total_batches=len(evaluated),
            blocked_batch_count=len(blocked),
            publishable_batch_count=len(publishable_batches),
            symbol_conflict_count=len(conflicts),
        ),
        blocked_batches=blocked,
        symbol_conflicts=conflicts,
        publishable=OrderedDict(
            description=(
                "Files the unaffected batches may still publish while the "
                "blocked batches are held back."
            ),
            batches=publishable_batches,
            all_public_files=public_files,
            all_symbol_files=symbol_files,
        ),
    )


def triage(
    builds: list[Build],
) -> TriageResult:
    """Evaluate all builds, resolve symbol conflicts and build the plans."""

    seen_batch_keys: dict[tuple[str, str], str] = {}
    evaluated: list[EvaluatedBuild] = []
    for build in builds:
        key = (build.build_id, build.architecture)
        if key in seen_batch_keys:
            holder = EvaluatedBuild(build=build, blocked=True)
            holder.add_reason(
                "duplicate_build",
                f"build {build.build_id!r} for architecture "
                f"{build.architecture!r} is indexed more than once",
            )
            evaluated.append(holder)
            continue
        seen_batch_keys[key] = str(build.index_path)
        evaluated.append(evaluate(build))

    # Phase 2: only symbols from currently-healthy batches participate in
    # cross-build merging/conflict detection.
    symbol_groups: dict[tuple[str, str], list[SymbolContribution]] = (
        defaultdict(list)
    )
    for item in evaluated:
        if item.blocked:
            continue
        for artifact in item.artifacts:
            if artifact.role == SYMBOL_ROLE and artifact.debug_id:
                symbol_groups[
                    (item.build.architecture, artifact.debug_id)
                ].append(
                    SymbolContribution(
                        build_id=item.build.build_id,
                        architecture=item.build.architecture,
                        artifact=artifact,
                    )
                )

    conflicted_keys: set[tuple[str, str]] = set()
    for group_key, contributions in symbol_groups.items():
        hashes = {c.artifact.sha256 for c in contributions}
        if len(hashes) > 1:
            conflicted_keys.add(group_key)
            arch, debug_id = group_key
            contributors = sorted({c.build_id for c in contributions})
            for item in evaluated:
                if (
                    item.blocked
                    or item.build.build_id not in contributors
                    or item.build.architecture != arch
                ):
                    continue
                if any(
                    a.role == SYMBOL_ROLE and a.debug_id == debug_id
                    for a in item.artifacts
                ):
                    item.blocked = True
                    item.add_reason(
                        REASON_SYMBOL_CONFLICT,
                        "same debug identifier maps to symbol files with "
                        "different content",
                        architecture=arch,
                        debug_id=debug_id,
                        observed_sha256=sorted(hashes),
                        contributing_builds=contributors,
                    )

    # Conflict-blocked batches must not appear as symbol contributors.
    for group_key in conflicted_keys:
        symbol_groups.pop(group_key, None)

    public_plan = _build_public_plan(evaluated)
    internal_plan = _build_internal_plan(symbol_groups, conflicted_keys)
    block_manifest = _build_manifest(evaluated, conflicted_keys)
    return TriageResult(
        public_plan=public_plan,
        internal_plan=internal_plan,
        block_manifest=block_manifest,
        evaluated=evaluated,
    )
