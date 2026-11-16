"""Pinned-descriptor, bounded reads for untrusted local files."""

import os
import secrets
import stat
from pathlib import Path


def read_regular_bounded(path: Path | str, limit: int, label: str) -> bytes:
    if type(limit) is not int or limit < 1:
        raise ValueError("invalid byte limit")
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts
    directory = os.open(
        absolute.anchor,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        for part in parts[1:-1]:
            try:
                next_directory = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory,
                )
            except OSError as exc:
                raise ValueError(f"{label} path is not safe") from exc
            os.close(directory)
            directory = next_directory
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            descriptor = os.open(parts[-1], flags, dir_fd=directory)
        except OSError as exc:
            raise ValueError(f"{label} must be a regular non-symlink file") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"{label} is not regular")
            if info.st_size > limit:
                raise ValueError(f"{label} exceeds byte limit")
            chunks = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > limit:
                raise ValueError(f"{label} exceeds byte limit")
            return raw
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def write_private_atomic(
    path: Path | str,
    payload: bytes,
    label: str = "output",
    *,
    overwrite: bool = True,
) -> None:
    """Atomically write a mode-0600 regular file under a pinned non-symlink parent."""
    if not isinstance(payload, bytes):
        raise ValueError("payload must be bytes")
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts
    if not parts or absolute.name in {"", ".", ".."}:
        raise ValueError(f"{label} path is invalid")
    directory = os.open(
        absolute.anchor,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    temporary_name = f".{absolute.name}.{secrets.token_hex(12)}.tmp"
    created = False
    try:
        for part in parts[1:-1]:
            try:
                next_directory = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory,
                )
            except FileNotFoundError as exc:
                raise FileNotFoundError(f"{label} parent directory does not exist") from exc
            except OSError as exc:
                raise ValueError(f"{label} parent path is not safe") from exc
            os.close(directory)
            directory = next_directory
        try:
            existing = os.stat(absolute.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise ValueError(f"{label} destination is unsafe") from exc
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ValueError(f"{label} destination must be a regular file")
        if existing is not None and not overwrite:
            raise FileExistsError(f"{label} destination already exists")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory)
            created = True
        except OSError as exc:
            raise ValueError(f"{label} temporary path is unsafe") from exc
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short output write")
                view = view[written:]
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if overwrite:
            os.replace(temporary_name, absolute.name, src_dir_fd=directory, dst_dir_fd=directory)
        else:
            try:
                os.link(
                    temporary_name, absolute.name,
                    src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise FileExistsError(f"{label} destination already exists") from exc
            os.unlink(temporary_name, dir_fd=directory)
        created = False
        os.fsync(directory)
    finally:
        if created:
            try:
                os.unlink(temporary_name, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.close(directory)
