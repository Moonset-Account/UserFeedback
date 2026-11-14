"""Shared helpers to build synthetic firmware images for tests."""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fwlayout.board import parse_board  # noqa: E402


def make_board(**overrides):
    """A board spec mirroring samples/board.json (16-byte records)."""
    spec = {
        "board": "test-board",
        "endianness": "little",
        "header": {"magic": "46574c47", "count_offset": 4, "count_size": 4},
        "record": {"fields": [
            {"name": "type", "size": 4},
            {"name": "start", "size": 4},
            {"name": "length", "size": 4},
            {"name": "perms", "size": 4},
        ]},
        "reserved_regions": [{"name": "bootrom", "start": 0, "length": 256}],
    }
    spec.update(overrides)
    return parse_board(spec)


def _put(buf, pos, data):
    """Write data into buf, clipped to buf's current length."""
    if pos >= len(buf):
        return
    end = min(pos + len(data), len(buf))
    buf[pos:end] = data[:end - pos]


def build_image(board, records, image_size=None, count=None, magic=None):
    """Build a synthetic image.

    records: list of dicts field-name -> value.
    image_size: total image size; defaults to exactly the header size.
                A smaller size yields truncated/missing records.
    count: override the declared record count (defaults to len(records)).
    magic: override the magic bytes (pass b"XXXX" to break the header).
    """
    rsize = board.record_size
    n = len(records) if count is None else count
    table_start = board.table_start
    header_size = table_start + n * rsize
    size = header_size if image_size is None else image_size
    buf = bytearray(size)
    m = board.magic if magic is None else magic
    if m:
        _put(buf, 0, m)
    _put(buf, board.count_offset, n.to_bytes(board.count_size, board.endianness))
    for i, rec in enumerate(records):
        pos = table_start + i * rsize
        for f in board.fields:
            _put(buf, pos, int(rec.get(f.name, 0)).to_bytes(f.size, board.endianness))
            pos += f.size
    return bytes(buf)
