"""Transfer-encoding mappers.

Each mapper scans the raw body of a leaf part and produces:

* a list of :class:`~mime_locator.model.Unit` linking decoded byte spans to
  absolute raw byte ranges in the original source;
* the total decoded length;
* an optional corruption reason (the part is then marked corrupted and can
  no longer be located, while its siblings remain usable).

Only base64, quoted-printable and the identity family (7bit/8bit/binary and
unknown encodings, treated transparently) are supported.
"""

from __future__ import annotations

import base64
import binascii
from typing import List, Optional, Tuple

from .model import Unit

B64_ALPHABET = set(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)
HEX_DIGITS = set(b"0123456789abcdefABCDEF")
# ASCII control characters that quoted-printable may not contain literally.
QP_FORBIDDEN = set(range(0, 9)) | {11, 12} | set(range(14, 32))


def _find_non_space(body: bytes, pos: int) -> int:
    """Advance past base64 whitespace (spaces, tabs, CR, LF)."""
    while pos < len(body) and body[pos] in b" \t\r\n":
        pos += 1
    return pos


def map_base64(
    body: bytes, body_start: int
) -> Tuple[List[Unit], int, Optional[str]]:
    """Map base64 four-character groups to decoded byte spans.

    Whitespace (including soft line breaks) is transport-only and skipped;
    each emitted unit is exactly one complete four-character group, so a
    single selected decoded byte is always attributed to its full group.
    """
    units: List[Unit] = []
    decoded_pos = 0
    pos = 0
    group_index = 0
    total = len(body)
    b64_data = B64_ALPHABET - {0x3D}

    while True:
        pos = _find_non_space(body, pos)
        if pos >= total:
            break

        group_raw_start = body_start + pos
        group: List[int] = []
        # Track each member's raw span so a group can be reconstructed even
        # when whitespace appears inside it.
        member_ranges: List[Tuple[int, int]] = []
        while len(group) < 4:
            pos = _find_non_space(body, pos)
            if pos >= total:
                break
            byte = body[pos]
            if byte == 0x3D and len(group) >= 2:
                break  # padding begins; handled below
            if byte not in b64_data:
                if byte == 0x3D:
                    return (
                        units,
                        decoded_pos,
                        f"illegal base64 padding in group {group_index} "
                        f"at raw offset {body_start + pos}",
                    )
                return (
                    units,
                    decoded_pos,
                    f"invalid base64 character 0x{byte:02x} in group "
                    f"{group_index} at raw offset {body_start + pos}",
                )
            member_ranges.append(
                (body_start + pos, body_start + pos + 1)
            )
            group.append(byte)
            pos += 1

        if len(group) < 2:
            return (
                units,
                decoded_pos,
                f"truncated base64 group {group_index}: only "
                f"{len(group)} data character(s) at raw offset "
                f"{group_raw_start}",
            )

        # Consume the required padding characters (only '=' allowed).
        padding = 0
        while len(group) + padding < 4:
            pos = _find_non_space(body, pos)
            if pos >= total or body[pos] != 0x3D:
                return (
                    units,
                    decoded_pos,
                    f"truncated base64 group {group_index}: missing padding "
                    f"at raw offset {group_raw_start}",
                )
            member_ranges.append(
                (body_start + pos, body_start + pos + 1)
            )
            padding += 1
            pos += 1

        if padding:
            # Padding may only appear on the final group of the body.
            if _find_non_space(body, pos) < total:
                return (
                    units,
                    decoded_pos,
                    f"base64 padding in non-final group {group_index} "
                    f"at raw offset {group_raw_start}",
                )
            decoded_len = 3 - padding
            if decoded_len <= 0:
                return (
                    units,
                    decoded_pos,
                    f"illegal base64 padding in group {group_index} "
                    f"at raw offset {group_raw_start}",
                )
        else:
            decoded_len = 3

        full_group = bytes(group + [0x3D] * padding)
        try:
            decoded = base64.b64decode(full_group, validate=True)
        except (binascii.Error, ValueError) as exc:
            return (
                units,
                decoded_pos,
                f"undecodable base64 group {group_index} "
                f"at raw offset {group_raw_start}: {exc}",
            )
        if len(decoded) != decoded_len:
            return (
                units,
                decoded_pos,
                f"base64 group {group_index} decoded to {len(decoded)} "
                f"bytes, expected {decoded_len}",
            )

        units.append(
            Unit(
                decoded_start=decoded_pos,
                decoded_end=decoded_pos + decoded_len,
                raw_ranges=tuple(member_ranges),
            )
        )
        decoded_pos += decoded_len
        group_index += 1

    return units, decoded_pos, None


def map_quoted_printable(
    body: bytes, body_start: int
) -> Tuple[List[Unit], int, Optional[str]]:
    """Map quoted-printable content to decoded byte spans.

    Soft line breaks (``=\\r\\n``) are transport-only and produce no decoded
    bytes; hard line breaks decode to ``\\n`` and map to the original CR/LF
    raw bytes.
    """
    units: List[Unit] = []
    decoded_pos = 0
    literal_start: Optional[int] = None  # body-relative offset
    total = len(body)

    def flush_literal(end_pos: int) -> None:
        nonlocal literal_start, decoded_pos
        if literal_start is not None:
            length = end_pos - literal_start
            units.append(
                Unit(
                    decoded_start=decoded_pos,
                    decoded_end=decoded_pos + length,
                    raw_ranges=(
                        (body_start + literal_start, body_start + end_pos),
                    ),
                )
            )
            decoded_pos += length
            literal_start = None

    pos = 0
    while pos < total:
        byte = body[pos]

        if byte == 0x3D:  # '='
            # Soft line break first: '=' followed by CRLF (or a bare LF).
            if pos + 2 < total and body[pos + 1 : pos + 3] == b"\r\n":
                flush_literal(pos)
                pos += 3
                continue
            if pos + 1 < total and body[pos + 1] == 0x0A:
                flush_literal(pos)
                pos += 2
                continue
            # Hex escape '=' HH
            if (
                pos + 2 < total
                and body[pos + 1] in HEX_DIGITS
                and body[pos + 2] in HEX_DIGITS
            ):
                flush_literal(pos)
                units.append(
                    Unit(
                        decoded_start=decoded_pos,
                        decoded_end=decoded_pos + 1,
                        raw_ranges=((body_start + pos, body_start + pos + 3),),
                    )
                )
                decoded_pos += 1
                pos += 3
                continue
            return (
                units,
                decoded_pos,
                f"malformed quoted-printable escape at raw offset "
                f"{body_start + pos}",
            )

        if byte in (0x0D, 0x0A):
            # Normalize CRLF or a bare LF to a single decoded '\n'.
            if byte == 0x0D and pos + 1 < total and body[pos + 1] == 0x0A:
                flush_literal(pos)
                units.append(
                    Unit(
                        decoded_start=decoded_pos,
                        decoded_end=decoded_pos + 1,
                        raw_ranges=((body_start + pos, body_start + pos + 2),),
                    )
                )
                decoded_pos += 1
                pos += 2
            elif byte == 0x0A:
                flush_literal(pos)
                units.append(
                    Unit(
                        decoded_start=decoded_pos,
                        decoded_end=decoded_pos + 1,
                        raw_ranges=((body_start + pos, body_start + pos + 1),),
                    )
                )
                decoded_pos += 1
                pos += 1
            else:
                return (
                    units,
                    decoded_pos,
                    f"stray carriage return at raw offset {body_start + pos}",
                )
            continue

        if byte in QP_FORBIDDEN:
            return (
                units,
                decoded_pos,
                f"illegal control byte 0x{byte:02x} in quoted-printable "
                f"body at raw offset {body_start + pos}",
            )

        # Literal byte (printable ASCII, TAB or high-bit octet).
        if literal_start is None:
            literal_start = pos
        pos += 1

    flush_literal(total)
    return units, decoded_pos, None


def map_identity(
    body: bytes, body_start: int
) -> Tuple[List[Unit], int, Optional[str]]:
    """Map an unencoded body one-to-one onto its raw bytes.

    One unit per decoded byte keeps ``locate`` able to return exact
    sub-ranges while still excluding every byte outside the part body.
    """
    units = [
        Unit(
            decoded_start=index,
            decoded_end=index + 1,
            raw_ranges=((body_start + index, body_start + index + 1),),
        )
        for index in range(len(body))
    ]
    return units, len(body), None
