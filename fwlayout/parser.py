"""Firmware image header / partition table parsing (read-only).

Only the header magic, the record-count field and the partition records
are decoded; partition contents are never touched.

Header layout (all integers use the board's endianness)::

    [magic bytes]            optional, from board description
    [record count]           count_size bytes at count_offset
    [record 0]               record_size bytes
    [record 1]
    ...

header_size = count_offset + count_size + record_count * record_size

If the magic is missing/mismatched or the record-count field lies
outside the image, the header size cannot be confirmed and the caller
must stop checking this image (other images may still be checked).
"""


class Record:
    __slots__ = ("index", "offset", "truncated", "available", "fields")

    def __init__(self, index, offset, truncated, available, fields):
        self.index = index            # position in the partition table
        self.offset = offset          # byte offset of the record in the image
        self.truncated = truncated    # record not fully present in the image
        self.available = available    # bytes of this record actually present
        self.fields = fields          # field name -> int or None (not present)


class HeaderResult:
    __slots__ = ("determinable", "reason", "record_count", "table_start",
                 "record_size", "header_size", "records", "missing_records")

    def __init__(self, determinable, reason=None, record_count=None,
                 table_start=None, record_size=None, header_size=None,
                 records=None, missing_records=0):
        self.determinable = determinable
        self.reason = reason
        self.record_count = record_count
        self.table_start = table_start
        self.record_size = record_size
        self.header_size = header_size
        self.records = records if records is not None else []
        self.missing_records = missing_records


def parse_image(image, board):
    """Parse the header of ``image`` (bytes) according to ``board``.

    Returns a HeaderResult.  When the header size cannot be confirmed,
    ``determinable`` is False and ``reason`` explains why.
    """
    if board.magic is not None:
        if len(image) < len(board.magic):
            return HeaderResult(
                False,
                "image (%d bytes) is shorter than the %d-byte header magic"
                % (len(image), len(board.magic)))
        if image[:len(board.magic)] != board.magic:
            return HeaderResult(False, "header magic mismatch")

    count_end = board.count_offset + board.count_size
    if count_end > len(image):
        return HeaderResult(
            False,
            "record-count field [%d, %d) lies outside the %d-byte image"
            % (board.count_offset, count_end, len(image)))

    count = int.from_bytes(image[board.count_offset:count_end], board.endianness)
    rsize = board.record_size
    table_start = count_end

    # Only records at least partially present in the image are
    # materialized; a declared count far beyond the image size is
    # reported via missing_records instead of being iterated.
    if len(image) > table_start:
        present = min(count, (len(image) - table_start + rsize - 1) // rsize)
    else:
        present = 0

    records = []
    for i in range(present):
        off = table_start + i * rsize
        available = min(rsize, len(image) - off)
        fields = {}
        pos = off
        for f in board.fields:
            if pos + f.size <= len(image):
                fields[f.name] = int.from_bytes(image[pos:pos + f.size],
                                                board.endianness)
            else:
                fields[f.name] = None
            pos += f.size
        records.append(Record(i, off, available < rsize, available, fields))

    missing = count - present
    header_size = table_start + count * rsize
    return HeaderResult(True, None, count, table_start, rsize, header_size,
                        records, missing)
