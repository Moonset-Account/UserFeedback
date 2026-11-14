"""Board description (JSON) loading and validation.

The board description tells the checker how to decode a firmware image
header: endianness, where the record count lives, the per-record field
layout (field sizes) and the board's reserved address ranges.
"""

import json


class BoardError(ValueError):
    """Raised when a board description is invalid."""


class Field:
    __slots__ = ("name", "size")

    def __init__(self, name, size):
        self.name = name
        self.size = size

    def __repr__(self):
        return "Field(%r, %r)" % (self.name, self.size)


class ReservedRegion:
    __slots__ = ("name", "start", "length")

    def __init__(self, name, start, length):
        self.name = name
        self.start = start
        self.length = length

    @property
    def end(self):
        return self.start + self.length


class BoardSpec:
    def __init__(self, name, endianness, fields, magic, count_offset,
                 count_size, reserved_regions):
        self.name = name
        self.endianness = endianness
        self.fields = fields
        self.magic = magic
        self.count_offset = count_offset
        self.count_size = count_size
        self.reserved_regions = reserved_regions

    @property
    def record_size(self):
        return sum(f.size for f in self.fields)

    @property
    def table_start(self):
        return self.count_offset + self.count_size


def _as_int(value, what, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int):
        raise BoardError("%s must be an integer" % what)
    if value < minimum:
        raise BoardError("%s must be >= %d" % (what, minimum))
    return value


def parse_board(data):
    """Validate a decoded board JSON object and return a BoardSpec."""
    if not isinstance(data, dict):
        raise BoardError("board description must be a JSON object")
    name = data.get("board", "<unnamed>")
    if not isinstance(name, str):
        raise BoardError("board must be a string")
    endianness = data.get("endianness")
    if endianness not in ("little", "big"):
        raise BoardError("endianness must be 'little' or 'big'")

    header = data.get("header", {})
    if not isinstance(header, dict):
        raise BoardError("header must be an object")
    count_offset = _as_int(header.get("count_offset", 0), "header.count_offset")
    count_size = _as_int(header.get("count_size"), "header.count_size", minimum=1)
    magic = None
    magic_hex = header.get("magic")
    if magic_hex is not None:
        if not isinstance(magic_hex, str):
            raise BoardError("header.magic must be a hex string")
        try:
            magic = bytes.fromhex(magic_hex)
        except ValueError:
            raise BoardError("header.magic is not valid hex")
        if not magic:
            raise BoardError("header.magic must not be empty")
        if count_offset < len(magic):
            raise BoardError("header.count_offset overlaps the magic bytes")

    record = data.get("record")
    if not isinstance(record, dict):
        raise BoardError("record must be an object")
    raw_fields = record.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise BoardError("record.fields must be a non-empty list")
    fields = []
    seen = set()
    for i, raw in enumerate(raw_fields):
        what = "record.fields[%d]" % i
        if not isinstance(raw, dict):
            raise BoardError(what + " must be an object")
        fname = raw.get("name")
        if not isinstance(fname, str) or not fname:
            raise BoardError(what + ".name must be a non-empty string")
        if fname in seen:
            raise BoardError("duplicate field name %r" % fname)
        seen.add(fname)
        fsize = _as_int(raw.get("size"), what + ".size", minimum=1)
        fields.append(Field(fname, fsize))
    for required in ("start", "length"):
        if required not in seen:
            raise BoardError("record.fields must include a %r field" % required)

    reserved = []
    raw_regions = data.get("reserved_regions", [])
    if not isinstance(raw_regions, list):
        raise BoardError("reserved_regions must be a list")
    for i, raw in enumerate(raw_regions):
        what = "reserved_regions[%d]" % i
        if not isinstance(raw, dict):
            raise BoardError(what + " must be an object")
        rname = raw.get("name", "region-%d" % i)
        if not isinstance(rname, str):
            raise BoardError(what + ".name must be a string")
        rstart = _as_int(raw.get("start"), what + ".start")
        rlength = _as_int(raw.get("length"), what + ".length")
        reserved.append(ReservedRegion(rname, rstart, rlength))

    return BoardSpec(name, endianness, fields, magic, count_offset,
                     count_size, reserved)


def load_board(path):
    """Read and validate a board description JSON file."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise BoardError("cannot read board description %s: %s" % (path, exc))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BoardError("invalid board JSON %s: %s" % (path, exc))
    return parse_board(data)
