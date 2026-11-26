"""Version stamps and path normalization."""

from __future__ import annotations

import hashlib
import os
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class VersionStamp:
    """An immutable input/output version.

    ``revision`` is the coordinator's monotonic per-path observation number.
    ``fingerprint`` is a SHA-256 content fingerprint.  Equality uses both, so
    an externally observed content change and the coordinator's resulting
    revision are both represented in persisted build records.
    """

    revision: int
    fingerprint: str
    exists: bool = True

    @property
    def token(self) -> str:
        if not self.exists:
            return "<missing>"
        return f"r{self.revision}:{self.fingerprint[:12]}"

    def to_json(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "fingerprint": self.fingerprint,
            "exists": self.exists,
        }

    @classmethod
    def from_json(cls, value: dict[str, Any] | None) -> "VersionStamp | None":
        if value is None:
            return None
        return cls(
            revision=int(value["revision"]),
            fingerprint=str(value["fingerprint"]),
            exists=bool(value.get("exists", True)),
        )


MISSING_VERSION = VersionStamp(0, "", exists=False)


def fingerprint_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def normalize_path(path: str | os.PathLike[str], root: Path | None = None) -> str:
    """Return a stable POSIX-style relative key.

    With a local root, absolute paths inside the root are made relative.  The
    coordinator rejects keys that escape the root.  In memory mode, normalized
    ``../`` paths are still accepted because there is no surrounding filesystem.
    """

    raw = os.fspath(path)
    if root is not None:
        root_resolved = root.resolve()
        candidate = Path(raw)
        if candidate.is_absolute():
            try:
                candidate = candidate.resolve().relative_to(root_resolved)
            except (OSError, ValueError) as exc:
                raise ValueError(f"path {raw!r} is outside root {root!r}") from exc
        else:
            candidate = Path(raw)
        parts: list[str] = []
        for part in candidate.parts:
            if part in ("", "."):
                continue
            if part == "..":
                if parts:
                    parts.pop()
                else:
                    raise ValueError(f"path {raw!r} escapes root {root!r}")
            else:
                parts.append(part)
        return posixpath.normpath("/".join(parts)) if parts else "."

    normalized = raw.replace(os.sep, "/")
    normalized = posixpath.normpath(normalized)
    if normalized == ".":
        return normalized
    return normalized
