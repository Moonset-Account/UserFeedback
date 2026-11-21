"""On-disk shard format and seekable, padding-aware reads.

A shard file owns one logical rectangle of a single rank-2 tensor.  Its on-disk
layout is deliberately simple and self-describing::

    +------------------------- fixed binary header (38 bytes) -------------------------+
    | magic (4s) | version (H) | rows (I) | cols (I) | row0 (I) | col0 (I) |           |
    | stride_floats (I) | valid_rows (I) | valid_cols (I) | end_padding (I)           |
    +-----------------------------------------------------------------------------------+
    | data region: valid_rows physical rows                                             |
    |   per row: valid_cols little-endian float32 values, then                          |
    |            (stride_floats - valid_cols) * 4 bytes of row-end padding              |
    | after the last row: end_padding bytes of end-of-file alignment padding            |
    +-----------------------------------------------------------------------------------+

The *logical rectangle* is ``rows[rect.row0:rect.row0+valid_rows]`` by
``cols[rect.col0:rect.col0+valid_cols]``; ``rows``/``cols`` describe the
*physical* allocation, which may be larger than the valid shape (for example a
column shard whose physical rows span the full tensor).  Reads use
``seek``/``read`` so that only bytes intersecting the requested rectangle are
touched; padding bytes are never returned and every read is reported as a
half-open byte interval for auditing.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import BinaryIO, Iterable, List, Sequence, Tuple

from .errors import ShardFormatError
from .geometry import Rect

__all__ = [
    "MAGIC",
    "FORMAT_VERSION",
    "DTYPE_FLOAT32_LE",
    "HEADER_SIZE",
    "HEADER_STRUCT",
    "ShardHeader",
    "ShardFile",
    "ByteRange",
    "write_shard",
    "merge_byte_ranges",
]

MAGIC = b"CKSH"
FORMAT_VERSION = 1
DTYPE_FLOAT32_LE = 0
FLOAT32_BYTES = 4

# Native struct fields with explicit little-endian prefix => no native padding.
HEADER_STRUCT = struct.Struct("<4sH7I")
HEADER_SIZE = HEADER_STRUCT.size  # 4 + 2 + 7*4 = 38


@dataclass(frozen=True)
class ByteRange:
    """Half-open byte interval ``[start, end)`` inside one shard file."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError("invalid byte interval")

    @property
    def length(self) -> int:
        return self.end - self.start

    def contains(self, other: "ByteRange") -> bool:
        return self.start <= other.start and other.end <= self.end

    def __str__(self) -> str:
        return f"bytes[{self.start}:{self.end}]"


def merge_byte_ranges(ranges: Iterable[ByteRange]) -> Tuple[ByteRange, ...]:
    """Merge overlapping or adjacent byte intervals; keep deterministic order."""

    ordered = sorted(ranges, key=lambda r: (r.start, r.end))
    merged: List[ByteRange] = []
    for current in ordered:
        if not merged or current.start > merged[-1].end:
            merged.append(current)
        elif current.end > merged[-1].end:
            merged[-1] = ByteRange(merged[-1].start, current.end)
    return tuple(merged)


@dataclass(frozen=True)
class ShardHeader:
    """Parsed fixed header of a shard file."""

    rows: int
    cols: int
    rect: Rect
    stride_floats: int
    valid_rows: int
    valid_cols: int
    end_padding: int

    @property
    def row_stride_bytes(self) -> int:
        return self.stride_floats * FLOAT32_BYTES

    @property
    def data_size(self) -> int:
        """Size of the data region, including row-end and end padding."""

        return self.valid_rows * self.row_stride_bytes + self.end_padding

    def row_data_bytes(self) -> int:
        """Bytes of valid float32 data per physical row (excluding padding)."""

        return self.valid_cols * FLOAT32_BYTES


def _validate_header(header: ShardHeader) -> None:
    if header.rows <= 0 or header.cols <= 0:
        raise ShardFormatError("physical shape must be positive")
    if header.valid_rows <= 0 or header.valid_cols <= 0:
        raise ShardFormatError("valid shape must be positive")
    if header.stride_floats < header.valid_cols:
        raise ShardFormatError("row stride is smaller than the valid row width")
    if header.end_padding < 0:
        raise ShardFormatError("end padding cannot be negative")
    if (
        header.rect.row0 + header.valid_rows > header.rows
        or header.rect.col0 + header.valid_cols > header.cols
    ):
        raise ShardFormatError(
            f"logical rectangle {header.rect} overflows physical shape "
            f"{header.rows}x{header.cols}"
        )


class ShardFile:
    """Seekable reader for one shard file.

    The file is opened lazily; use it as a context manager or call
    :meth:`close` explicitly.  Every :meth:`read_rect` records the exact byte
    intervals it touched, available through :attr:`accessed_ranges`.
    """

    def __init__(self, path: str):
        self.path = path
        self._file: BinaryIO | None = None
        self._header: ShardHeader | None = None
        self._accessed: List[ByteRange] = []

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "ShardFile":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self) -> None:
        if self._file is not None:
            return
        try:
            file_obj = open(self.path, "rb")
        except OSError as exc:
            raise ShardFormatError(f"cannot open shard {self.path!r}: {exc}") from exc
        try:
            header = self._read_header(file_obj)
            _validate_header(header)
            actual_size = os.fstat(file_obj.fileno()).st_size
            expected_size = HEADER_SIZE + header.data_size
            if actual_size != expected_size:
                raise ShardFormatError(
                    f"shard {self.path!r} has {actual_size} bytes but its header "
                    f"requires {expected_size} (header={HEADER_SIZE}, data="
                    f"{header.data_size})"
                )
        except Exception:
            file_obj.close()
            raise
        self._file = file_obj
        self._header = header

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    @property
    def closed(self) -> bool:
        return self._file is None

    @staticmethod
    def _read_header(file_obj: BinaryIO) -> ShardHeader:
        raw = file_obj.read(HEADER_SIZE)
        if len(raw) < HEADER_SIZE:
            raise ShardFormatError("file too small to contain a shard header")
        try:
            (
                magic,
                version,
                rows,
                cols,
                row0,
                col0,
                stride_floats,
                valid_rows,
                valid_cols,
                end_padding,
            ) = HEADER_STRUCT.unpack(raw)
        except struct.error as exc:
            raise ShardFormatError(f"malformed shard header: {exc}") from exc
        if magic != MAGIC:
            raise ShardFormatError(f"bad magic {magic!r}, expected {MAGIC!r}")
        if version != FORMAT_VERSION:
            raise ShardFormatError(
                f"unsupported shard format version {version}, "
                f"expected {FORMAT_VERSION}"
            )
        try:
            rect = Rect(row0, col0, row0 + valid_rows, col0 + valid_cols)
        except ValueError as exc:
            raise ShardFormatError(f"invalid logical rectangle in header: {exc}") from exc
        return ShardHeader(
            rows=rows,
            cols=cols,
            rect=rect,
            stride_floats=stride_floats,
            valid_rows=valid_rows,
            valid_cols=valid_cols,
            end_padding=end_padding,
        )

    # -- metadata ----------------------------------------------------------

    @property
    def header(self) -> ShardHeader:
        if self._header is None:
            raise ShardFormatError("shard file is not open")
        return self._header

    @property
    def accessed_ranges(self) -> Tuple[ByteRange, ...]:
        """Merged byte intervals touched since the file was opened."""

        return merge_byte_ranges(self._accessed)

    # -- reading -----------------------------------------------------------

    def read_rect(self, target: Rect) -> List[List[float]]:
        """Read the intersection of ``target`` with this shard's valid data.

        Returns a row-major list of rows; each row is a list of little-endian
        float32 values.  Only bytes of valid elements are read: row-end and
        end-of-file padding are skipped with ``seek`` and never returned.
        """

        if self._file is None or self._header is None:
            raise ShardFormatError("shard file is not open")
        header = self._header
        wanted = Rect.coerce(target).intersect(header.rect)
        if wanted.is_empty:
            return []

        result: List[List[float]] = []
        for row in range(wanted.row0, wanted.row1):
            local_row = row - header.rect.row0
            offset = (
                HEADER_SIZE
                + local_row * header.row_stride_bytes
                + (wanted.col0 - header.rect.col0) * FLOAT32_BYTES
            )
            count = wanted.cols
            self._file.seek(offset)
            raw = self._file.read(count * FLOAT32_BYTES)
            if len(raw) != count * FLOAT32_BYTES:
                raise ShardFormatError(
                    f"short read in shard {self.path!r} at {ByteRange(offset, offset + len(raw))}"
                )
            self._accessed.append(ByteRange(offset, offset + len(raw)))
            result.append(list(struct.unpack(f"<{count}f", raw)))
        return result


# ---------------------------------------------------------------------------
# Writer (used to build synthetic old checkpoints and recovered new shards)
# ---------------------------------------------------------------------------


def write_shard(
    path: str,
    *,
    rows: int,
    cols: int,
    rect: Rect | Sequence[int],
    stride_floats: int | None = None,
    values: Sequence[Sequence[float]],
    end_padding: int = 0,
) -> ShardHeader:
    """Write one shard file and return the header that was persisted.

    ``values`` is the row-major valid data: ``len(values) == valid_rows`` and
    every row has ``valid_cols`` elements.  When ``stride_floats`` is omitted
    it defaults to ``valid_cols`` (no row-end padding).  Any larger stride
    writes zero-filled row-end padding; ``end_padding`` appends zero bytes
    after the final row for whole-file alignment.
    """

    logical = Rect.coerce(rect)
    valid_rows = logical.rows
    valid_cols = logical.cols
    if valid_rows == 0 or valid_cols == 0:
        raise ValueError("a shard must own a non-empty rectangle")
    if rows <= 0 or cols <= 0:
        raise ValueError("physical shape must be positive")
    if logical.row0 + valid_rows > rows or logical.col0 + valid_cols > cols:
        raise ValueError(
            f"logical rectangle {logical} overflows physical shape {rows}x{cols}"
        )
    if stride_floats is None:
        stride_floats = valid_cols
    if stride_floats < valid_cols:
        raise ValueError("stride_floats must be >= valid column count")
    if end_padding < 0:
        raise ValueError("end_padding must be non-negative")
    if len(values) != valid_rows:
        raise ValueError(
            f"values has {len(values)} rows but the rectangle needs {valid_rows}"
        )

    pad_floats = stride_floats - valid_cols
    row_pad = b"\x00" * (pad_floats * FLOAT32_BYTES)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    with open(path, "wb") as file_obj:
        file_obj.write(
            HEADER_STRUCT.pack(
                MAGIC,
                FORMAT_VERSION,
                rows,
                cols,
                logical.row0,
                logical.col0,
                stride_floats,
                valid_rows,
                valid_cols,
                end_padding,
            )
        )
        for row_index, row in enumerate(values):
            if len(row) != valid_cols:
                raise ValueError(
                    f"values row {row_index} has {len(row)} elements, "
                    f"expected {valid_cols}"
                )
            try:
                file_obj.write(struct.pack(f"<{valid_cols}f", *row))
            except struct.error as exc:
                raise ValueError(f"row {row_index} contains non-float data: {exc}") from exc
            file_obj.write(row_pad)
        if end_padding:
            file_obj.write(b"\x00" * end_padding)

    header = ShardHeader(
        rows=rows,
        cols=cols,
        rect=logical,
        stride_floats=stride_floats,
        valid_rows=valid_rows,
        valid_cols=valid_cols,
        end_padding=end_padding,
    )
    _validate_header(header)
    return header
