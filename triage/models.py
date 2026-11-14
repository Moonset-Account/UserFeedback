"""In-memory representation of build indexes.

Index schema (one JSON document per build batch)::

    {
      "build_id": "B20261114-001",
      "architecture": "arm64",
      "artifacts": [
        {"role": "main_program",     "path": "bin/app",          "debug_id": "A1B2..."},
        {"role": "runtime_library",  "path": "lib/libfoo.dylib"},
        {"role": "runtime_resource", "path": "share/config.json"},
        {"role": "debug_symbol",     "path": "symbols/app.dbg",  "debug_id": "A1B2..."}
      ]
    }

Top-level structural problems are fatal (:class:`ManifestError`); problems
attached to a single artifact entry are collected on :class:`Build` and turn
that build batch into a blocked batch instead of taking the whole run down.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .errors import ManifestError
from .safety import normalize_debug_id

PUBLIC_ROLES = frozenset(
    {"main_program", "runtime_library", "runtime_resource"}
)
SYMBOL_ROLE = "debug_symbol"
KNOWN_ROLES = frozenset(PUBLIC_ROLES | {SYMBOL_ROLE})


@dataclass
class Artifact:
    """One file declared by a build index."""

    role: str
    rel_path: str
    debug_id: Optional[str] = None
    abs_path: Optional[Path] = None
    size: Optional[int] = None
    sha256: Optional[str] = None


@dataclass
class Build:
    """A single build batch: one architecture, one build number."""

    build_id: str
    architecture: str
    artifacts: list[Artifact] = field(default_factory=list)
    index_path: Optional[Path] = None
    root: Optional[Path] = None
    entry_errors: list[str] = field(default_factory=list)


@dataclass
class IndexSpec:
    """CLI pairing of an index file and the root its paths are relative to."""

    index_path: Path
    root: Path


def load_index(index_path: Path, root: Path) -> Build:
    """Parse a build index document.

    Fatal :class:`ManifestError` is raised only when the document cannot be
    mapped to a build batch at all (bad JSON, missing build_id / architecture
    / artifacts container).
    """

    try:
        raw = index_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read index {index_path}: {exc}") from exc
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid JSON in index {index_path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ManifestError(
            f"index {index_path} top-level value must be an object"
        )

    build_id = doc.get("build_id")
    architecture = doc.get("architecture")
    entries = doc.get("artifacts")
    if not isinstance(build_id, str) or not build_id.strip():
        raise ManifestError(
            f"index {index_path}: 'build_id' must be a non-empty string"
        )
    if not isinstance(architecture, str) or not architecture.strip():
        raise ManifestError(
            f"index {index_path}: 'architecture' must be a non-empty string"
        )
    if not isinstance(entries, list):
        raise ManifestError(
            f"index {index_path}: 'artifacts' must be a list"
        )

    build = Build(
        build_id=build_id.strip(),
        architecture=architecture.strip(),
        index_path=index_path,
        root=root,
    )
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            build.entry_errors.append(
                f"invalid_artifact_entry[{position}]: entry is not an object"
            )
            continue
        role = entry.get("role")
        rel_path = entry.get("path")
        if not isinstance(role, str) or not role.strip():
            build.entry_errors.append(
                f"invalid_artifact_entry[{position}]: missing 'role'"
            )
            continue
        if not isinstance(rel_path, str) or not rel_path:
            build.entry_errors.append(
                f"invalid_artifact_entry[{position}]: missing 'path'"
            )
            continue
        debug_id: Optional[str] = None
        raw_debug_id = entry.get("debug_id")
        if raw_debug_id is not None:
            try:
                debug_id = normalize_debug_id(
                    raw_debug_id,
                    field=f"artifacts[{position}].debug_id",
                )
            except ManifestError as exc:
                build.entry_errors.append(str(exc))
                continue
        build.artifacts.append(
            Artifact(
                role=role.strip(),
                rel_path=rel_path,
                debug_id=debug_id,
            )
        )
    return build
