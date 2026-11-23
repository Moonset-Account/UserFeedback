"""Synthetic object builder used to construct test fixtures.

Two raw-DEFLATE encoders are provided:

* :func:`encode_literal_frame` / :func:`encode_dictionary_frame` emit fixed
  Huffman blocks (BTYPE=01). Dictionary frames contain back-references whose
  distance reaches into the supplied reset dictionary, which is the only way
  to prove that a reader actually wires the predecessor's tail bytes into
  ``zlib.decompressobj(..., zdict=...)``.
* :func:`encode_stored_frame` emits uncompressed stored blocks (level 0),
  whose bytes are independent of any dictionary. This is what makes the
  "same compressed bytes, two dictionaries" scenario possible.

All tables follow RFC 1951 sections 3.2.5/3.2.6 and are covered by the test
suite against the standard-library ``zlib`` decompressor, including
exhaustive checks of every length and distance code.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .models import FrameEntry

# ---------------------------------------------------------------------- #
# Fixed Huffman canonical codes (RFC 1951 3.2.6), stored LSB-first.
# ---------------------------------------------------------------------- #


def _reverse(value: int, width: int) -> int:
    result = 0
    for _ in range(width):
        result = (result << 1) | (value & 1)
        value >>= 1
    return result


class _BitWriter:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self._current = 0
        self._bits = 0

    def bits(self, value: int, count: int) -> None:
        for _ in range(count):
            self._current |= (value & 1) << self._bits
            self._bits += 1
            value >>= 1
            if self._bits == 8:
                self.buffer.append(self._current)
                self._current = 0
                self._bits = 0

    def align(self) -> None:
        if self._bits:
            self.buffer.append(self._current)
            self._current = 0
            self._bits = 0


_FIXED_CANONICAL: List[Tuple[int, int]] = []
for _symbol in range(288):
    if _symbol <= 143:
        _FIXED_CANONICAL.append((0x30 + _symbol, 8))
    elif _symbol <= 255:
        _FIXED_CANONICAL.append((400 + (_symbol - 144), 9))
    elif _symbol <= 279:
        _FIXED_CANONICAL.append((_symbol - 256, 7))
    else:
        _FIXED_CANONICAL.append((192 + (_symbol - 280), 8))
_FIXED_REVERSED = [(_reverse(code, width), width) for code, width in _FIXED_CANONICAL]

_LENGTH_BASE = [
    3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59,
    67, 83, 99, 115, 131, 163, 195, 227, 258,
]
_LENGTH_EXTRA = [
    0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4,
    5, 5, 5, 5, 0,
]
_DIST_BASE = [
    1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513,
    769, 1025, 1537, 2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577,
]
_DIST_EXTRA = [
    0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10,
    11, 11, 12, 12, 13, 13,
]


def _find_interval(bases: Sequence[int], extras: Sequence[int], value: int) -> int:
    index = 0
    while index + 1 < len(bases) and value >= bases[index] + (1 << extras[index]):
        index += 1
    return index


def _emit_literal(writer: _BitWriter, symbol: int) -> None:
    code, width = _FIXED_REVERSED[symbol]
    writer.bits(code, width)


def _emit_length(writer: _BitWriter, length: int) -> None:
    index = _find_interval(_LENGTH_BASE, _LENGTH_EXTRA, length)
    _emit_literal(writer, 257 + index)
    writer.bits(length - _LENGTH_BASE[index], _LENGTH_EXTRA[index])


def _emit_distance(writer: _BitWriter, distance: int) -> None:
    index = _find_interval(_DIST_BASE, _DIST_EXTRA, distance)
    # Five-bit distance codes are canonical values sent LSB-first, so the
    # integer code is bit-reversed before the LSB-first packing.
    writer.bits(_reverse(index, 5), 5)
    writer.bits(distance - _DIST_BASE[index], _DIST_EXTRA[index])


# A plan is a sequence of ('lit', byte) or ('match', length, distance) ops.
Plan = List[Tuple]


def _encode_plan_blocks(blocks: Sequence[Tuple[Plan, bool]]) -> bytes:
    writer = _BitWriter()
    for plan, final in blocks:
        writer.bits(1 if final else 0, 1)
        writer.bits(1, 2)  # BTYPE = 01 fixed Huffman, LSB-first -> 10
        for operation in plan:
            if operation[0] == "lit":
                _emit_literal(writer, operation[1])
            else:
                _, length, distance = operation
                if not 3 <= length <= 258:
                    raise ValueError("match length must be within 3..258")
                if distance < 1:
                    raise ValueError("match distance must be positive")
                _emit_length(writer, length)
                _emit_distance(writer, distance)
        _emit_literal(writer, 256)  # end-of-block
    writer.align()
    return bytes(writer.buffer)


def encode_fixed_frame(plan: Plan) -> bytes:
    """Encode one final fixed-Huffman block from a literal/match plan."""
    return _encode_plan_blocks([(plan, True)])


def encode_literal_frame(data: bytes) -> bytes:
    """Encode ``data`` as a fixed-Huffman block of literal bytes."""
    return encode_fixed_frame([("lit", byte) for byte in data])


def encode_dictionary_frame(
    dictionary: bytes,
    match_length: int,
    distance: int,
    suffix: bytes = b"",
) -> Tuple[bytes, bytes]:
    """Encode a frame whose first symbols back-reference the dictionary.

    The match copies ``match_length`` bytes from ``distance`` bytes back,
    i.e. from the tail of ``dictionary`` when ``distance`` reaches into it.
    Returns ``(compressed_bytes, expected_decompressed_output)``.
    """
    if not 3 <= match_length <= 258:
        raise ValueError("match_length must be within 3..258")
    if distance < 1 or distance > len(dictionary):
        raise ValueError("distance must reach inside the dictionary")
    if match_length > distance:
        raise ValueError(
            "a dictionary-relative match cannot extend past the referenced "
            "dictionary bytes (match_length <= distance required)"
        )

    history = bytearray(dictionary)
    source = len(history) - distance
    matched = bytes(history[source:source + match_length])
    expected = matched + bytes(suffix)
    plan: Plan = [("match", match_length, distance)]
    plan.extend(("lit", byte) for byte in suffix)
    return encode_fixed_frame(plan), expected


def encode_stored_frame(data: bytes) -> bytes:
    """Encode ``data`` using raw stored blocks (no back-references).

    The output is a valid raw-DEFLATE stream that inflates identically
    regardless of the preset dictionary, so two index versions can share the
    exact same compressed bytes while declaring different dictionaries.
    """
    compressor = zlib.compressobj(0, zlib.DEFLATED, -15)
    return compressor.compress(bytes(data)) + compressor.flush()


# ---------------------------------------------------------------------- #
# Object assembly
# ---------------------------------------------------------------------- #


@dataclass
class BuiltFrame:
    entry: FrameEntry
    payload: bytes


@dataclass
class BuiltObject:
    compressed: bytes
    frames: tuple[BuiltFrame, ...]

    def entries(self) -> tuple[FrameEntry, ...]:
        return tuple(frame.entry for frame in self.frames)


def build_object(
    spec: Sequence[Tuple[bytes, Optional[str], int]],
) -> BuiltObject:
    """Assemble a compressed object and its index entries.

    Each spec item is ``(decompressed_payload, dict_frame_id, dict_size)``.
    The payload is encoded as a stored block. Compressed frames are packed
    back-to-back and every entry records its offset, length and CRC-32.
    """
    compressed = bytearray()
    built: List[BuiltFrame] = []
    logical_cursor = 0

    for index, (payload, dict_frame_id, dict_size) in enumerate(spec):
        payload = bytes(payload)
        if not payload:
            raise ValueError("frame payload must be non-empty")
        stream = encode_stored_frame(payload)
        offset = len(compressed)
        compressed.extend(stream)
        entry = FrameEntry(
            frame_id=f"f{index}",
            logical_start=logical_cursor,
            logical_end=logical_cursor + len(payload),
            compressed_offset=offset,
            compressed_length=len(stream),
            checksum=zlib.crc32(payload) & 0xFFFFFFFF,
            dict_frame_id=dict_frame_id,
            dict_size=dict_size,
        )
        built.append(BuiltFrame(entry=entry, payload=payload))
        logical_cursor += len(payload)

    return BuiltObject(compressed=bytes(compressed), frames=tuple(built))
