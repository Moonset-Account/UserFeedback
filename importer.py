"""Atomic, safe batch import of ZIP resource packages into a shared area.

Standard library only. See README.md for the requirement -> code/test mapping.

Design summary
--------------
1. Plan first: every archive member is mapped to its target relative path
   before a single byte is extracted. Names that would land on the same
   location are detected with the target filesystem's case / Unicode rules
   (``collision_key``).
2. Containment: absolute paths, ``..`` components, backslashes, NUL bytes and
   empty targets are rejected; symlinks and special (non-regular) files are
   rejected. Paths can never leave the material directory.
3. Size limits: declared sizes from the central directory are used for a
   pre-check only. During extraction the actual expanded bytes are counted
   per entry and for the whole package while streaming.
4. Atomicity: a batch is extracted into a hidden staging directory inside the
   shared area and published with a single ``os.rename``. Any failure
   (corrupt archive, path conflict, expansion over limit) removes the staging
   directory, so the shared area never shows partial material.
5. Failure reports locate the archive entry (name + index) that caused the
   failure.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass

DEFAULT_MAX_ENTRY_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024
CHUNK = 64 * 1024
STAGING_PREFIX = ".staging-"

# Stable error codes used in failure reports.
ERR_BAD_ARCHIVE = "bad_archive"
ERR_NOT_A_DIR = "not_a_directory"
ERR_INVALID_LIMIT = "invalid_limit"
ERR_INVALID_BATCH_NAME = "invalid_batch_name"
ERR_BATCH_EXISTS = "batch_exists"
ERR_PATH_ABSOLUTE = "path_absolute"
ERR_PATH_BACKSLASH = "path_backslash"
ERR_PATH_ESCAPE = "path_escape"
ERR_PATH_EMPTY = "path_empty"
ERR_NAME_COLLISION = "name_collision"
ERR_FILE_DIR_CONFLICT = "file_dir_conflict"
ERR_SYMLINK = "symlink"
ERR_SPECIAL_FILE = "special_file"
ERR_ENCRYPTED = "encrypted_entry"
ERR_ENTRY_TOO_LARGE = "entry_too_large"
ERR_TOTAL_TOO_LARGE = "total_too_large"


class ImportFailure(Exception):
    """A rejected or failed import. Always locates the archive entry when one
    is responsible (``entry`` / ``entry_index``; both ``None`` for
    archive-level failures)."""

    def __init__(self, code, message, entry=None, entry_index=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.entry = entry
        self.entry_index = entry_index

    def to_report(self):
        return {
            "status": "error",
            "error": {
                "code": self.code,
                "message": self.message,
                "entry": self.entry,
                "entry_index": self.entry_index,
            },
        }


@dataclass
class EntryPlan:
    index: int          # position inside the archive
    name: str           # raw archive member name
    rel_path: str       # normalized target path relative to the batch dir ('/' separated)
    is_dir: bool
    declared_size: int  # uncompressed size from the central directory (pre-check only)


def normalize_platform(platform):
    """Map user-facing platform names to ``sys.platform`` style values."""
    if platform in (None, "auto"):
        return sys.platform
    aliases = {
        "windows": "win32",
        "win": "win32",
        "macos": "darwin",
        "mac": "darwin",
        "osx": "darwin",
    }
    return aliases.get(str(platform).lower(), str(platform).lower())


def collision_key(path, platform=None):
    """Key identifying the on-disk location ``path`` would occupy.

    - darwin (APFS/HFS+ default volumes): case-insensitive and Unicode
      normalization-insensitive -> NFD + casefold.
    - windows (NTFS default): case-insensitive, roughly NFC-preserving ->
      NFC + casefold.
    - everything else (e.g. Linux ext4): byte-exact -> identity.
    """
    p = normalize_platform(platform)
    if p == "darwin":
        return unicodedata.normalize("NFD", path).casefold()
    if p.startswith("win"):
        return unicodedata.normalize("NFC", path).casefold()
    return path


def _entry_kind(info):
    """Classify a ZipInfo as file/dir/symlink/special from its unix mode."""
    mode = (info.external_attr >> 16) & 0xFFFF
    ftype = stat.S_IFMT(mode)
    if ftype == 0:
        # No unix file-type bits recorded (e.g. Windows-produced archives or
        # plain permission bits only): fall back to the name convention.
        return "dir" if info.filename.endswith("/") else "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "special"


def _target_relpath(name, index):
    """Validate an archive member name and return (rel_path, is_dir)."""
    if "\x00" in name:
        raise ImportFailure(ERR_PATH_ESCAPE, "NUL byte in entry name", name, index)
    if "\\" in name:
        raise ImportFailure(
            ERR_PATH_BACKSLASH,
            "backslash in entry name is ambiguous across filesystems",
            name, index)
    if name.startswith("/"):
        raise ImportFailure(ERR_PATH_ABSOLUTE, "absolute path is not allowed", name, index)
    if len(name) >= 2 and name[1] == ":" and name[0].isalpha():
        raise ImportFailure(
            ERR_PATH_ABSOLUTE, "drive-letter absolute path is not allowed", name, index)
    is_dir = name.endswith("/")
    parts = []
    for comp in name.split("/"):
        if comp in ("", "."):
            continue
        if comp == "..":
            raise ImportFailure(
                ERR_PATH_ESCAPE,
                "'..' component would escape the material directory",
                name, index)
        parts.append(comp)
    if not parts:
        raise ImportFailure(ERR_PATH_EMPTY, "entry has no target path", name, index)
    return "/".join(parts), is_dir


def _check_layout_conflicts(plans, platform):
    """Reject entries that would occupy the same on-disk location."""
    dir_keys = set()
    for p in plans:
        comps = p.rel_path.split("/")
        for i in range(1, len(comps)):
            dir_keys.add(collision_key("/".join(comps[:i]), platform))
        if p.is_dir:
            dir_keys.add(collision_key(p.rel_path, platform))
    seen_files = {}
    for p in plans:
        if p.is_dir:
            continue
        key = collision_key(p.rel_path, platform)
        other = seen_files.get(key)
        if other is not None:
            raise ImportFailure(
                ERR_NAME_COLLISION,
                "entries #%d (%r) and #%d (%r) resolve to the same target "
                "location on this filesystem"
                % (other.index, other.name, p.index, p.name),
                p.name, p.index)
        if key in dir_keys:
            raise ImportFailure(
                ERR_FILE_DIR_CONFLICT,
                "entry #%d (%r) is a file but a directory occupies the same "
                "location" % (p.index, p.name),
                p.name, p.index)
        seen_files[key] = p


def plan_entries(zf, platform=None):
    """Build the target plan for every member of an open ZipFile.

    Raises ImportFailure (locating the offending entry) on the first member
    that cannot be imported safely.
    """
    plans = []
    for index, info in enumerate(zf.infolist()):
        name = info.filename
        kind = _entry_kind(info)
        if info.flag_bits & 0x1:
            raise ImportFailure(
                ERR_ENCRYPTED, "encrypted entries are not supported", name, index)
        if kind == "symlink":
            raise ImportFailure(
                ERR_SYMLINK, "symbolic links are not allowed", name, index)
        if kind == "special":
            raise ImportFailure(
                ERR_SPECIAL_FILE,
                "special files (device, fifo, socket, ...) are not allowed",
                name, index)
        rel_path, is_dir = _target_relpath(name, index)
        is_dir = is_dir or kind == "dir"
        plans.append(EntryPlan(index, name, rel_path, is_dir, info.file_size))
    _check_layout_conflicts(plans, platform)
    return plans


def precheck_sizes(plans, max_entry_bytes, max_total_bytes):
    """Pre-check using declared sizes only. The real enforcement happens
    while streaming (declared sizes are untrusted metadata)."""
    total = 0
    for p in plans:
        if p.is_dir:
            continue
        if p.declared_size > max_entry_bytes:
            raise ImportFailure(
                ERR_ENTRY_TOO_LARGE,
                "declared size %d exceeds per-entry limit %d (pre-check)"
                % (p.declared_size, max_entry_bytes),
                p.name, p.index)
        total += p.declared_size
        if total > max_total_bytes:
            raise ImportFailure(
                ERR_TOTAL_TOO_LARGE,
                "declared total exceeds package limit %d (pre-check)"
                % max_total_bytes,
                p.name, p.index)


def _extract_all(zf, plans, staging, max_entry_bytes, max_total_bytes):
    """Stream every planned entry into ``staging`` while enforcing the actual
    expanded-byte limits. Raises ImportFailure locating the entry on the
    first violation or read error."""
    staging_real = os.path.realpath(staging)
    infos = zf.infolist()
    written = []
    total = 0
    for p in plans:
        dest = os.path.join(staging, *p.rel_path.split("/"))
        # Defense in depth: the validated plan should already guarantee this.
        if os.path.commonpath((staging_real, os.path.realpath(dest))) != staging_real:
            raise ImportFailure(
                ERR_PATH_ESCAPE,
                "resolved target escapes the staging directory: %r" % p.rel_path,
                p.name, p.index)
        if p.is_dir:
            os.makedirs(dest, exist_ok=True)
            continue
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        count = 0
        try:
            with zf.open(infos[p.index], "r") as src, open(dest, "wb") as dst:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > max_entry_bytes:
                        raise ImportFailure(
                            ERR_ENTRY_TOO_LARGE,
                            "entry expands beyond per-entry limit of %d bytes"
                            % max_entry_bytes,
                            p.name, p.index)
                    if total + count > max_total_bytes:
                        raise ImportFailure(
                            ERR_TOTAL_TOO_LARGE,
                            "package expands beyond total limit of %d bytes"
                            % max_total_bytes,
                            p.name, p.index)
                    dst.write(chunk)
        except ImportFailure:
            raise
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
            raise ImportFailure(
                ERR_BAD_ARCHIVE, "cannot extract entry: %s" % exc, p.name, p.index)
        total += count
        written.append({"entry": p.name, "path": p.rel_path, "bytes": count})
    return written, total


def _validate_batch_name(name):
    if (not name or name in (".", "..") or "/" in name or "\\" in name
            or "\x00" in name or name != name.strip()):
        raise ImportFailure(
            ERR_INVALID_BATCH_NAME, "invalid batch name %r" % (name,))


def default_batch_name(archive_path):
    """Deterministic batch name: sanitized archive stem + short content hash."""
    stem = os.path.splitext(os.path.basename(archive_path))[0] or "batch"
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in stem)
    if safe in ("", ".", ".."):
        safe = "batch"
    h = hashlib.sha256()
    with open(archive_path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return "%s-%s" % (safe, h.hexdigest()[:10])


def _open_archive(archive_path):
    try:
        zf = zipfile.ZipFile(archive_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ImportFailure(
            ERR_BAD_ARCHIVE, "cannot open archive %r: %s" % (archive_path, exc))
    try:
        zf.infolist()
    except zipfile.BadZipFile as exc:
        zf.close()
        raise ImportFailure(
            ERR_BAD_ARCHIVE, "cannot read archive central directory: %s" % exc)
    return zf


def plan_report(archive_path, platform=None):
    """List every entry's target relative path without extracting anything."""
    with _open_archive(archive_path) as zf:
        plans = plan_entries(zf, platform=platform)
    return {
        "status": "ok",
        "archive": os.path.abspath(archive_path),
        "entry_count": len(plans),
        "entries": [
            {
                "index": p.index,
                "entry": p.name,
                "target": p.rel_path,
                "kind": "dir" if p.is_dir else "file",
                "declared_size": p.declared_size,
                "collision_key": collision_key(p.rel_path, platform),
            }
            for p in plans
        ],
    }


def import_archive(archive_path, shared_dir,
                   max_entry_bytes=DEFAULT_MAX_ENTRY_BYTES,
                   max_total_bytes=DEFAULT_MAX_TOTAL_BYTES,
                   batch_name=None, platform=None):
    """Import one ZIP archive as one new batch directory inside ``shared_dir``.

    Returns a success report dict. Raises ImportFailure (with the responsible
    archive entry located when applicable) on any rejection/failure; in that
    case the shared area is left exactly as it was.
    """
    if max_entry_bytes < 0 or max_total_bytes < 0:
        raise ImportFailure(ERR_INVALID_LIMIT, "size limits must be >= 0")
    if not os.path.isdir(shared_dir):
        raise ImportFailure(
            ERR_NOT_A_DIR,
            "shared directory %r does not exist or is not a directory" % shared_dir)
    with _open_archive(archive_path) as zf:
        plans = plan_entries(zf, platform=platform)
        precheck_sizes(plans, max_entry_bytes, max_total_bytes)
        if batch_name is None:
            batch_name = default_batch_name(archive_path)
        _validate_batch_name(batch_name)
        final = os.path.join(shared_dir, batch_name)
        if os.path.lexists(final):
            raise ImportFailure(
                ERR_BATCH_EXISTS,
                "batch %r already exists in the shared area" % batch_name)
        staging = tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=shared_dir)
        try:
            written, total = _extract_all(
                zf, plans, staging, max_entry_bytes, max_total_bytes)
            if os.path.lexists(final):
                raise ImportFailure(
                    ERR_BATCH_EXISTS,
                    "batch %r already exists in the shared area" % batch_name)
            os.rename(staging, final)  # atomic publish on the same filesystem
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return {
        "status": "ok",
        "batch_name": batch_name,
        "batch_dir": final,
        "entries": written,
        "total_bytes": total,
        "limits": {
            "max_entry_bytes": max_entry_bytes,
            "max_total_bytes": max_total_bytes,
        },
    }
