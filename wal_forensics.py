"""Byte-level, read-only forensic reconstruction of SQLite WAL commits.

Given a SQLite main database file (the checkpoint baseline *before* the first
WAL frame, trusted to not have been rewritten by a later checkpoint) and the
raw bytes of one WAL segment, this module:

  * validates the WAL header (magic, format version, page size, salts and the
    header checksum) per the SQLite WAL format specification;
  * walks frames one by one, verifying the salt pair and the cumulative
    rolling checksum of every frame;
  * treats a frame whose "database size after commit" field is non-zero as a
    commit boundary;
  * stops at the first truncation / salt / checksum failure and keeps only
    the commits proven up to that point -- uncommitted tail frames never
    enter any view;
  * exposes, for every complete commit, a per-page-number view where a page
    written several times resolves to the last frame at or before that
    commit, and pages beyond the committing transaction's database size are
    excluded;
  * records, for every page, whether it comes from the main file or from a
    specific WAL frame (byte offsets included) as evidence;
  * reads page bytes lazily, on demand -- commits are never materialised as
    full database copies.

The module never opens evidence through SQLite and never writes to evidence
sources.  It only accepts ``bytes``-like objects or binary file-like objects
that the caller opened read-only.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union

WAL_MAGIC = 0x377F0682          # bit 0 set => big-endian checksums
WAL_VERSION = 3007000
WAL_HEADER_SIZE = 32
FRAME_HEADER_SIZE = 24
MIN_PAGE_SIZE = 512
MAX_PAGE_SIZE = 65536

__all__ = [
    "WAL_MAGIC",
    "WAL_VERSION",
    "WAL_HEADER_SIZE",
    "FRAME_HEADER_SIZE",
    "WalFormatError",
    "PageOutOfRangeError",
    "PageUnavailableError",
    "StopReason",
    "WalHeader",
    "FrameRecord",
    "CommitRecord",
    "PageEvidence",
    "PagedReader",
    "WalImage",
    "CommitView",
    "wal_checksum",
    "parse_wal",
]


class WalFormatError(Exception):
    """The WAL (or a requested structure within it) is not usable."""


class PageOutOfRangeError(WalFormatError):
    """Page lies outside the committing transaction's database size."""


class PageUnavailableError(WalFormatError):
    """Page is in range but absent from both the WAL view and the main file."""


class StopReason(str, Enum):
    """Why frame scanning stopped."""

    CLEAN = "clean"                    # every byte consumed, no error
    TRAILING_BYTES = "trailing_bytes"  # fewer than one frame header left over
    TRUNCATED_FRAME = "truncated_frame"  # frame cut off mid-record
    SALT_MISMATCH = "salt_mismatch"    # frame salts differ from header salts
    CHECKSUM_MISMATCH = "checksum_mismatch"  # rolling checksum broken


def wal_checksum(data: bytes, s1: int, s2: int, big_endian: bool) -> Tuple[int, int]:
    """SQLite WAL rolling checksum over ``data`` (length must be a multiple of 8).

    Returns the new (s1, s2) pair so callers can chain invocations.
    """

    if len(data) % 8 != 0:
        raise WalFormatError("checksum input length must be a multiple of 8")
    byteorder = "big" if big_endian else "little"
    for off in range(0, len(data), 8):
        x0 = int.from_bytes(data[off:off + 4], byteorder)
        x1 = int.from_bytes(data[off + 4:off + 8], byteorder)
        s1 = (s1 + x0 + s2) & 0xFFFFFFFF
        s2 = (s2 + x1 + s1) & 0xFFFFFFFF
    return s1, s2


class PagedReader:
    """Read-only, on-demand byte-range reader over bytes or a binary file object.

    Nothing is read until :meth:`read_at` is called, so wrapping a file object
    here does not copy it.  The object is never written to.
    """

    def __init__(self, source: Union[bytes, bytearray, memoryview, object]):
        if isinstance(source, (bytes, bytearray, memoryview)):
            self._blob: Optional[bytes] = bytes(source)
            self._file = None
            self._size = len(self._blob)
        elif hasattr(source, "read") and hasattr(source, "seek"):
            self._blob = None
            self._file = source
            pos = source.tell()
            source.seek(0, os.SEEK_END)
            self._size = source.tell()
            source.seek(pos)
        else:
            raise TypeError("source must be bytes-like or a binary file object")

    @property
    def size(self) -> int:
        return self._size

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self._size:
            raise WalFormatError(
                f"read outside source: offset={offset} length={length} size={self._size}"
            )
        if self._blob is not None:
            return self._blob[offset:offset + length]
        self._file.seek(offset)
        data = self._file.read(length)
        if len(data) != length:
            raise WalFormatError("short read from evidence source")
        return data


@dataclass(frozen=True)
class WalHeader:
    magic: int
    version: int
    page_size: int
    checkpoint_seq: int
    salt1: int
    salt2: int
    checksum1: int
    checksum2: int
    big_endian_checksums: bool


@dataclass(frozen=True)
class FrameRecord:
    index: int                 # 0-based frame number within this WAL segment
    page_number: int
    db_size_after: int         # non-zero => this frame commits the transaction
    header_offset: int         # byte offset of the 24-byte frame header
    data_offset: int           # byte offset of the page image inside the WAL


@dataclass(frozen=True)
class CommitRecord:
    index: int                 # 0-based commit number
    start_frame_index: int     # first frame of the committing transaction
    end_frame_index: int       # the commit frame (db_size_after != 0)
    db_size_pages: int         # database size in pages after this commit


@dataclass(frozen=True)
class PageEvidence:
    page_number: int
    source: str                # "main" or "wal"
    frame_index: Optional[int]  # set when source == "wal"
    file_offset: int           # byte offset of the page image in its source


class WalImage:
    """Parsed and verified view of one WAL segment."""

    def __init__(
        self,
        header: WalHeader,
        frames: List[FrameRecord],
        commits: List[CommitRecord],
        stop_reason: StopReason,
        stop_offset: int,
        reader: PagedReader,
    ):
        self.header = header
        self.frames: Tuple[FrameRecord, ...] = tuple(frames)
        self.commits: Tuple[CommitRecord, ...] = tuple(commits)
        self.stop_reason = stop_reason
        self.stop_offset = stop_offset
        self._reader = reader

    @property
    def page_size(self) -> int:
        return self.header.page_size

    @property
    def wal_size(self) -> int:
        return self._reader.size

    @property
    def frame_size(self) -> int:
        return FRAME_HEADER_SIZE + self.header.page_size

    @property
    def dropped_frame_estimate(self) -> int:
        """Frames (committed or not) abandoned at/after the stop offset."""
        if self.stop_reason is StopReason.CLEAN:
            return 0
        remaining = self._reader.size - self.stop_offset
        return max(1, remaining // self.frame_size) if remaining > 0 else 0

    @property
    def trailing_uncommitted_frames(self) -> int:
        """Verified frames that follow the last commit boundary."""
        if not self.commits:
            return len(self.frames)
        return len(self.frames) - 1 - self.commits[-1].end_frame_index

    def read_frame_page(self, frame_index: int) -> bytes:
        frame = self.frames[frame_index]
        return self._reader.read_at(frame.data_offset, self.page_size)

    def view(self, commit_index: int, main: Union[PagedReader, bytes, object]) -> "CommitView":
        """Lazy per-page view of the database as of commit ``commit_index``."""
        if not 0 <= commit_index < len(self.commits):
            raise WalFormatError(f"no such commit: {commit_index}")
        return CommitView(self, self.commits[commit_index], main)

    def latest_view(self, main: Union[PagedReader, bytes, object]) -> "CommitView":
        if not self.commits:
            raise WalFormatError("WAL contains no verified commit")
        return self.view(len(self.commits) - 1, main)


class CommitView:
    """Page-number keyed view of one committed database state.

    Only page-number -> frame-index metadata is built up front; page bytes
    are fetched from the WAL or the main file on each :meth:`read_page`.
    """

    def __init__(self, image: WalImage, commit: CommitRecord, main):
        self.image = image
        self.commit = commit
        self._main = main if isinstance(main, PagedReader) else PagedReader(main)
        self._page_map: Dict[int, int] = {}
        for frame in image.frames[: commit.end_frame_index + 1]:
            # later frames of the same page overwrite earlier ones
            self._page_map[frame.page_number] = frame.index

    @property
    def page_size(self) -> int:
        return self.image.page_size

    @property
    def page_count(self) -> int:
        """Database size in pages as of this commit (shrink-aware)."""
        return self.commit.db_size_pages

    def __contains__(self, page_number: int) -> bool:
        return 1 <= page_number <= self.page_count

    def _check_range(self, page_number: int) -> None:
        if not 1 <= page_number <= self.page_count:
            raise PageOutOfRangeError(
                f"page {page_number} outside commit database size "
                f"{self.page_count} pages"
            )

    def evidence(self, page_number: int) -> PageEvidence:
        """Where this commit's copy of ``page_number`` physically lives."""
        self._check_range(page_number)
        frame_index = self._page_map.get(page_number)
        if frame_index is None:
            offset = (page_number - 1) * self.page_size
            if offset + self.page_size > self._main.size:
                raise PageUnavailableError(
                    f"page {page_number} not in WAL and beyond main file "
                    f"({self._main.size} bytes)"
                )
            return PageEvidence(page_number, "main", None, offset)
        frame = self.image.frames[frame_index]
        return PageEvidence(page_number, "wal", frame_index, frame.data_offset)

    def read_page(self, page_number: int) -> bytes:
        ev = self.evidence(page_number)
        if ev.source == "wal":
            return self.image.read_frame_page(ev.frame_index)
        return self._main.read_at(ev.file_offset, self.page_size)


def _parse_header(reader: PagedReader) -> WalHeader:
    if reader.size < WAL_HEADER_SIZE:
        raise WalFormatError(
            f"WAL too small for a header: {reader.size} bytes"
        )
    raw = reader.read_at(0, WAL_HEADER_SIZE)
    magic, version, page_size, ckpt_seq, salt1, salt2, ck1, ck2 = struct.unpack(
        ">8I", raw
    )
    if magic == WAL_MAGIC:
        big_endian = False
    elif magic == WAL_MAGIC | 1:
        big_endian = True
    else:
        raise WalFormatError(f"bad WAL magic: 0x{magic:08x}")
    if version != WAL_VERSION:
        raise WalFormatError(f"unsupported WAL format version: {version}")
    if page_size == 1:
        page_size = MAX_PAGE_SIZE
    if (
        page_size < MIN_PAGE_SIZE
        or page_size > MAX_PAGE_SIZE
        or page_size & (page_size - 1)
    ):
        raise WalFormatError(f"invalid WAL page size: {page_size}")
    s1, s2 = wal_checksum(raw[:24], 0, 0, big_endian)
    if (s1, s2) != (ck1, ck2):
        raise WalFormatError("WAL header checksum mismatch")
    return WalHeader(
        magic=magic,
        version=version,
        page_size=page_size,
        checkpoint_seq=ckpt_seq,
        salt1=salt1,
        salt2=salt2,
        checksum1=ck1,
        checksum2=ck2,
        big_endian_checksums=big_endian,
    )


def parse_wal(wal: Union[PagedReader, bytes, bytearray, memoryview, object]) -> WalImage:
    """Parse and cryptographically verify one WAL segment.

    Raises :class:`WalFormatError` if the header itself is unusable.  Frame
    level problems (truncation, salt or checksum mismatch) do not raise: the
    returned :class:`WalImage` keeps every commit proven before the failure
    and reports the failure through ``stop_reason`` / ``stop_offset``.
    """

    reader = wal if isinstance(wal, PagedReader) else PagedReader(wal)
    header = _parse_header(reader)
    page_size = header.page_size
    frame_size = FRAME_HEADER_SIZE + page_size

    frames: List[FrameRecord] = []
    commits: List[CommitRecord] = []
    s1, s2 = header.checksum1, header.checksum2
    be = header.big_endian_checksums
    offset = WAL_HEADER_SIZE
    stop_reason = StopReason.CLEAN
    stop_offset = reader.size

    while offset + FRAME_HEADER_SIZE <= reader.size:
        frame_header = reader.read_at(offset, FRAME_HEADER_SIZE)
        pgno, db_size, fsalt1, fsalt2, fck1, fck2 = struct.unpack(">6I", frame_header)
        if offset + frame_size > reader.size:
            stop_reason = StopReason.TRUNCATED_FRAME
            stop_offset = offset
            break
        if (fsalt1, fsalt2) != (header.salt1, header.salt2):
            stop_reason = StopReason.SALT_MISMATCH
            stop_offset = offset
            break
        page = reader.read_at(offset + FRAME_HEADER_SIZE, page_size)
        s1, s2 = wal_checksum(frame_header[:8], s1, s2, be)
        s1, s2 = wal_checksum(page, s1, s2, be)
        if (s1, s2) != (fck1, fck2):
            stop_reason = StopReason.CHECKSUM_MISMATCH
            stop_offset = offset
            break
        index = len(frames)
        frames.append(
            FrameRecord(
                index=index,
                page_number=pgno,
                db_size_after=db_size,
                header_offset=offset,
                data_offset=offset + FRAME_HEADER_SIZE,
            )
        )
        if db_size != 0:
            start = commits[-1].end_frame_index + 1 if commits else 0
            commits.append(
                CommitRecord(
                    index=len(commits),
                    start_frame_index=start,
                    end_frame_index=index,
                    db_size_pages=db_size,
                )
            )
        offset += frame_size

    if stop_reason is StopReason.CLEAN and offset < reader.size:
        stop_reason = StopReason.TRAILING_BYTES
        stop_offset = offset

    return WalImage(header, frames, commits, stop_reason, stop_offset, reader)
