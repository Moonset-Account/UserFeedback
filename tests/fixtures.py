"""Synthetic MIME fixtures and verification helpers.

All messages are generated locally in the tests; nothing is fetched from a
network.  Every generated message uses CRLF line endings per RFC 2045.
"""

from __future__ import annotations

import base64
import quopri
from typing import List, Optional, Sequence, Tuple

CRLF = b"\r\n"


def b64_soft_wrap(encoded: bytes, width: int = 24) -> bytes:
    """Insert CRLF soft line breaks into a base64 string."""
    lines = [encoded[i : i + width] for i in range(0, len(encoded), width)]
    return CRLF.join(lines) + CRLF


def canonical_base64(payload: bytes, width: int = 24) -> bytes:
    return b64_soft_wrap(base64.b64encode(payload), width)


def canonical_qp(text: bytes) -> bytes:
    return quopri.encodestring(text)


def part(headers: Sequence[str], body: bytes) -> bytes:
    head = CRLF.join(line.encode("ascii") for line in headers)
    return head + CRLF + CRLF + body


def multipart(boundary: str, children: Sequence[bytes], preamble: bytes = b"") -> bytes:
    """Assemble a multipart body (children already serialized).

    Per RFC 2046 every boundary delimiter line is preceded by a CRLF that
    belongs to the preceding part.  Children whose body does not already end
    with a line break get one so the delimiter always starts a new line.
    """
    out: List[bytes] = []
    if preamble:
        out.append(preamble)
    for child in children:
        out.append(b"--" + boundary.encode("ascii") + CRLF)
        out.append(child)
        if not child.endswith(CRLF):
            out.append(CRLF)
    out.append(b"--" + boundary.encode("ascii") + b"--" + CRLF)
    return b"".join(out)


def message(headers: Sequence[str], body: bytes) -> bytes:
    return part(headers, body)


def simple_multipart_message() -> Tuple[bytes, bytes, bytes]:
    """A multipart/mixed with a QP text part and a soft-wrapped base64 part."""
    boundary = "BOUND-MAIN-01"
    text_payload = "café line one\r\nsecond line\r\n".encode("utf-8")
    bin_payload = bytes(range(256)) * 3  # 768 bytes, many base64 groups

    text_part = part(
        [
            "Content-Type: text/plain; charset=utf-8",
            "Content-Transfer-Encoding: quoted-printable",
        ],
        canonical_qp(text_payload),
    )
    bin_part = part(
        [
            "Content-Type: application/octet-stream",
            'Content-Disposition: attachment; filename="blob.bin"',
            "Content-Transfer-Encoding: base64",
        ],
        canonical_base64(bin_payload),
    )
    body = multipart(boundary, [text_part, bin_part])
    raw = message(
        [
            "MIME-Version: 1.0",
            "From: reviewer@example.invalid",
            "To: evidence@example.invalid",
            "Subject: forwarded exhibit",
            f'Content-Type: multipart/mixed; boundary="{boundary}"',
        ],
        body,
    )
    return raw, text_payload, bin_payload


def nested_message() -> Tuple[bytes, bytes]:
    """Outer multipart containing a message/rfc822 with an inner multipart."""
    inner_boundary = "BOUND-INNER-02"
    inner_payload = b"nested inner evidence payload 0123456789"
    inner_text = part(
        ["Content-Type: text/plain", "Content-Transfer-Encoding: base64"],
        canonical_base64(inner_payload),
    )
    inner_note = part(
        ["Content-Type: text/plain", "Content-Transfer-Encoding: 7bit"],
        b"plain inner note\r\n",
    )
    inner_body = multipart(inner_boundary, [inner_text, inner_note])
    inner_msg = message(
        [
            "MIME-Version: 1.0",
            "From: inner@example.invalid",
            f'Content-Type: multipart/mixed; boundary="{inner_boundary}"',
        ],
        inner_body,
    )

    outer_boundary = "BOUND-OUTER-03"
    outer_text = part(
        ["Content-Type: text/plain", "Content-Transfer-Encoding: 7bit"],
        b"outer covering note\r\n",
    )
    rfc822_part = part(
        ["Content-Type: message/rfc822", "Content-Disposition: attachment"],
        inner_msg,
    )
    outer_body = multipart(outer_boundary, [outer_text, rfc822_part])
    raw = message(
        [
            "MIME-Version: 1.0",
            "From: reviewer@example.invalid",
            f'Content-Type: multipart/mixed; boundary="{outer_boundary}"',
        ],
        outer_body,
    )
    return raw, inner_payload


def duplicates_and_corruption_message() -> Tuple[bytes, bytes]:
    """Four attachments: duplicate healthy parts, a corrupt one, a QP one."""
    boundary = "BOUND-DUP-04"
    shared_payload = b"identical attachment contents"
    healthy = part(
        [
            "Content-Type: application/octet-stream",
            'Content-Disposition: attachment; filename="same.bin"',
            "Content-Transfer-Encoding: base64",
        ],
        canonical_base64(shared_payload),
    )
    # A non-alphabet character ('#') inside a group makes the part corrupted;
    # the healthy attachments before and after it stay independently usable.
    corrupt = part(
        [
            "Content-Type: application/octet-stream",
            'Content-Disposition: attachment; filename="broken.b64"',
            "Content-Transfer-Encoding: base64",
        ],
        b"QUJDREVGR###\r\n",
    )
    qp_part = part(
        [
            "Content-Type: text/plain",
            'Content-Disposition: attachment; filename="note.txt"',
            "Content-Transfer-Encoding: quoted-printable",
        ],
        b"soft=E2=80=93break=3Dhere\r\nline two\r\n",
    )
    body = multipart(
        boundary, [healthy, healthy, corrupt, qp_part]
    )
    raw = message(
        [
            "MIME-Version: 1.0",
            f'Content-Type: multipart/mixed; boundary="{boundary}"',
        ],
        body,
    )
    return raw, shared_payload


def qp_gap_message() -> bytes:
    """A QP part whose body contains a soft line break in the middle."""
    boundary = "BOUND-QP-05"
    # Decoded target: "before" + é(UTF-8 C3 A9) + "orafter"
    body_text = b"before" + b"\xc3\xa9" + b"orafter"
    # Hand-written QP: hex escapes for the two high-bit octets, with a soft
    # line break spliced between 'or' and 'a' so the break is transport-only.
    qp_body = b"before=C3=A9or=\r\nafter\r\n"
    child = part(
        [
            "Content-Type: text/plain; charset=utf-8",
            "Content-Transfer-Encoding: quoted-printable",
        ],
        qp_body,
    )
    return message(
        [
            "MIME-Version: 1.0",
            f'Content-Type: multipart/mixed; boundary="{boundary}"',
        ],
        multipart(boundary, [child]),
    ), body_text, qp_body


def identity_message() -> bytes:
    boundary = "BOUND-ID-06"
    payload = b"raw 8bit binary \x00\x01\x02 payload"
    child = part(
        [
            "Content-Type: application/octet-stream",
            "Content-Transfer-Encoding: 8bit",
        ],
        payload,
    )
    return message(
        [
            "MIME-Version: 1.0",
            f'Content-Type: multipart/mixed; boundary="{boundary}"',
        ],
        multipart(boundary, [child]),
    ), payload


def decode_located_bytes(archive, location) -> bytes:
    """Independently re-decode the raw ranges using stdlib codecs."""
    raw = location.extract(archive.source)
    if location.encoding == "base64":
        return base64.b64decode(raw)
    if location.encoding == "quoted-printable":
        return quopri.decodestring(raw)
    return raw


def assert_ranges_within_body(testcase, archive, leaf, location) -> None:
    """No reported range may touch boundary lines or escape the part body."""
    for start, end in location.raw_ranges:
        testcase.assertGreaterEqual(start, leaf.raw_body_start)
        testcase.assertLessEqual(end, leaf.raw_body_end)
        testcase.assertLess(start, end)
    # Boundary delimiters themselves must never be covered.
    for start, end in location.raw_ranges:
        testcase.assertNotIn(b"--BOUND", archive.source[start:end])
