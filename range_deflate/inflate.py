"""Raw DEFLATE inflation with reset dictionaries and strict validation."""

from __future__ import annotations

import zlib
from typing import Optional

from .errors import FrameChecksumError


def inflate_frame(
    frame_id: str,
    compressed: bytes,
    expected_checksum: int,
    expected_length: int,
    dictionary: Optional[bytes] = None,
) -> bytes:
    """Inflate one raw (wbits=-15) DEFLATE frame.

    ``dictionary`` is the exact tail bytes of a predecessor frame (or
    ``None``/empty for the first frame). The result is accepted only when:

    * the stream reaches its end-of-stream marker (truncated input is
      rejected rather than silently returning partial output),
    * every compressed byte is consumed (trailing garbage is rejected),
    * the decompressed length matches the index, and
    * the CRC-32 matches ``expected_checksum``.

    Any failure is raised as :class:`FrameChecksumError` tagged with
    ``frame_id`` so the offending frame is always identifiable.
    """
    try:
        decompressor = zlib.decompressobj(
            -15, zdict=dictionary if dictionary else b""
        )
        output = decompressor.decompress(compressed)
        output += decompressor.flush()
    except zlib.error as exc:
        raise FrameChecksumError(
            frame_id,
            f"raw DEFLATE inflation failed "
            f"(dictionary length {len(dictionary) if dictionary else 0}): {exc}",
        ) from None

    if not decompressor.eof:
        raise FrameChecksumError(
            frame_id,
            "compressed stream ended before the end-of-block marker "
            "(frame is truncated)",
        )
    if decompressor.unused_data:
        raise FrameChecksumError(
            frame_id,
            f"{len(decompressor.unused_data)} trailing byte(s) after the "
            "end-of-block marker; compressed length in the index is wrong",
        )
    if len(output) != expected_length:
        raise FrameChecksumError(
            frame_id,
            f"decompressed length {len(output)} does not match index "
            f"length {expected_length} (wrong reset dictionary?)",
        )

    actual = zlib.crc32(output) & 0xFFFFFFFF
    if actual != expected_checksum:
        raise FrameChecksumError(
            frame_id,
            f"checksum mismatch: index records "
            f"0x{expected_checksum:08x}, decompressed bytes give "
            f"0x{actual:08x} (corruption or wrong reset dictionary)",
        )
    return output
