"""Durable JSON file helpers.

The crash protocol is:

1. write a uniquely named temporary file in the same directory;
2. flush and ``fsync`` its bytes;
3. atomically ``os.replace`` it onto the final path;
4. ``fsync`` the containing directory so the rename is durable.

Crash barriers are exposed after temp-file durability and after the atomic
replacement/directory fsync.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .crash import CrashController


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(
    path: Path,
    value: Any,
    *,
    crash: Optional[CrashController] = None,
    barrier_prefix: str = "write",
) -> Tuple[str, int]:
    """Atomically and durably write ``value``.

    Returns ``(sha256_hex, byte_length)``.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json_bytes(value)
    digest = sha256_bytes(data)
    # Unique temp names make a killed writer unable to overwrite another temp.
    tmp = path.parent / f".{path.name}.tmp.{uuid.uuid4().hex}"
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            # os.fdopen takes ownership of fd; no separate close needed on
            # successful construction.  This guard is for exceptional writes.
            raise
        if crash is not None:
            crash.check(
                f"{barrier_prefix}_temp_fsynced",
                final_path=path.name,
                temp_path=tmp.name,
                bytes=len(data),
            )
        os.replace(str(tmp), str(path))
        if crash is not None:
            # A real power loss before the directory fsync can leave either the
            # old or new directory entry; both are complete because file bytes
            # were fsynced and replacement is atomic.
            crash.check(
                f"{barrier_prefix}_rename_before_dir_fsync",
                final_path=path.name,
                bytes=len(data),
            )
        fsync_directory(path.parent)
        if crash is not None:
            crash.check(
                f"{barrier_prefix}_replaced",
                final_path=path.name,
                bytes=len(data),
                sha256=digest,
            )
        return digest, len(data)
    finally:
        # If we are crashing via Python exception before replace, leaving the
        # temp file is intentional: tests model an abrupt stop and do not run
        # cleanup.  The finally only removes a temp if this function is exiting
        # due to an ordinary I/O error before replacement.  SimulatedCrash is a
        # BaseException and would also trigger removal here, which would weaken
        # temp leftovers but not committed state.  To model fail-stop exactly,
        # do not unlink on SimulatedCrash.
        if tmp.exists():
            should_leave = False
            # The only BaseException used for deliberate cut points is
            # SimulatedCrash; importing it here avoids a module cycle at runtime.
            from .errors import SimulatedCrash

            if crash is not None and crash.crashed_event is not None:
                should_leave = True
            if isinstance(_current_exception(), SimulatedCrash):
                should_leave = True
            if not should_leave:
                try:
                    tmp.unlink()
                except OSError:
                    pass


def _current_exception() -> Optional[BaseException]:
    import sys

    return sys.exc_info()[1]


def read_json(path: Path) -> Any:
    with path.open("rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def verify_ref(root: Path, ref: Dict[str, Any]) -> Any:
    """Read a content reference and verify its recorded SHA-256/size."""

    rel = ref["path"]
    path = root / rel
    data = path.read_bytes()
    if len(data) != int(ref.get("size", -1)):
        from .errors import CorruptChunkError

        raise CorruptChunkError(f"size mismatch for {rel}")
    actual = sha256_bytes(data)
    if actual != ref.get("sha256"):
        from .errors import CorruptChunkError

        raise CorruptChunkError(f"sha256 mismatch for {rel}")
    return json.loads(data.decode("utf-8"))


def safe_group_id(group_id: str) -> str:
    if not isinstance(group_id, str) or not group_id:
        raise ValueError("group_id must be a non-empty string")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")
    if any(ch not in allowed for ch in group_id):
        raise ValueError(f"unsafe group_id: {group_id!r}")
    return group_id
