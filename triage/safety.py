"""Path and identifier validation helpers.

Build indexes are untrusted input.  Every artifact path must resolve inside the
artifact root that was supplied on the command line; absolute paths, parent
traversal and symbolic links are refused so a crafted index cannot make the
planner read files outside the build output.
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import ManifestError, UnsafePathError

# Debug identifiers are opaque to the build system: they only need to be
# printable tokens without whitespace or path separators.
_ALLOWED_DEBUG_ID_CHARS = set(
    "0123456789abcdefABCDEF-_.+ABCDEFGHIJKLMNOPQRSTUVWXYZ"
)


def normalize_debug_id(raw: object, *, field: str) -> str:
    """Validate and normalize a debug identifier.

    Returns the identifier as a string or raises ``ManifestError``.
    """

    if not isinstance(raw, str) or not raw:
        raise ManifestError(f"{field} must be a non-empty string")
    value = raw.strip()
    if not value:
        raise ManifestError(f"{field} must be a non-empty string")
    bad = sorted({ch for ch in value if ch not in _ALLOWED_DEBUG_ID_CHARS})
    if bad:
        raise ManifestError(
            f"{field} contains unsupported characters: {''.join(bad)!r}"
        )
    if value in (".", ".."):
        raise ManifestError(f"{field} must not be a path component")
    return value


def resolve_artifact(root: Path, relative_path: object) -> Path:
    """Resolve *relative_path* under *root* with strict containment checks.

    Raises ``UnsafePathError`` for absolute paths, parent traversal, empty
    values, or any path whose resolution leaves *root* (including symlink
    escapes).  The returned path is absolute and has no redundant components.
    """

    if not isinstance(relative_path, str) or not relative_path:
        raise UnsafePathError("artifact path must be a non-empty string")
    candidate = relative_path.replace("\\", "/")
    if candidate.startswith("/"):
        raise UnsafePathError(
            f"artifact path must be relative, got {relative_path!r}"
        )
    parts = [part for part in candidate.split("/") if part not in ("", ".")]
    if not parts:
        raise UnsafePathError(
            f"artifact path has no usable components: {relative_path!r}"
        )
    if any(part == ".." for part in parts):
        raise UnsafePathError(
            f"artifact path must not contain '..': {relative_path!r}"
        )

    root_resolved = root.resolve(strict=False)
    target = root_resolved.joinpath(*parts)
    # Walk every component so a symlink pointing outside the root is caught
    # even when the final file does not exist yet.
    current = root_resolved
    for part in parts:
        current = current / part
        if current.is_symlink():
            link_target = os.readlink(current)
            expanded = (
                Path(link_target)
                if os.path.isabs(link_target)
                else current.parent / link_target
            )
            resolved_link = expanded.resolve(strict=False)
            try:
                resolved_link.relative_to(root_resolved)
            except ValueError as exc:
                raise UnsafePathError(
                    f"symlink escapes artifact root: {relative_path!r}"
                ) from exc
    resolved = target.resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise UnsafePathError(
            f"artifact path escapes artifact root: {relative_path!r}"
        ) from exc
    return resolved
