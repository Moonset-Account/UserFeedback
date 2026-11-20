"""Offset-preserving, read-only MIME parser.

The standard-library :mod:`email` package does not retain absolute offsets of
the original source, so this module implements a small, strict scanner
itself.  Every reported offset is relative to the original source bytes,
which are never modified.
"""

from __future__ import annotations

from email.header import decode_header, make_header
from typing import Dict, List, Optional, Tuple

from .encodings import map_base64, map_identity, map_quoted_printable
from .model import MimeParseError, PartNode, PathEntry

_HEADER_TOKEN_CHARS = set(
    b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    b"!#$%&'*+-.^_`|~"
)


def parse_message(source: bytes) -> PartNode:
    """Parse a complete MIME message and return the root :class:`PartNode`."""
    if not isinstance(source, (bytes, bytearray, memoryview)):
        raise MimeParseError("source must be bytes-like")
    source = bytes(source)
    if not source:
        raise MimeParseError("empty source is not a MIME message")
    leaf_counter = [0]
    return _parse_region(source, 0, len(source), (), leaf_counter)


def _parse_region(
    source: bytes,
    start: int,
    end: int,
    parent_path: Tuple[PathEntry, ...],
    leaf_counter: List[int],
) -> PartNode:
    headers, body_start, body_end = _split_headers(source, start, end)
    content_type, params = _parse_header_value(headers.get("content-type", ""))
    filename = _filename_from_headers(headers)
    main_type = content_type.split("/", 1)[0] if content_type else ""

    if main_type == "multipart":
        node = PartNode(
            kind="multipart",
            content_type=content_type or "multipart/mixed",
            filename=filename,
            path=parent_path,
        )
        boundary = params.get("boundary")
        if not boundary:
            raise MimeParseError(
                f"multipart part at offset {start} has no boundary parameter"
            )
        regions = _scan_multipart(source, body_start, body_end, boundary)
        for index, (child_start, child_end) in enumerate(regions):
            child_headers, _, _ = _split_headers(
                source, child_start, child_end
            )
            child_type, _ = _parse_header_value(
                child_headers.get("content-type", "")
            )
            child_name = _filename_from_headers(child_headers)
            child_path = parent_path + (
                PathEntry("part", index, child_type or "application/octet-stream",
                          child_name),
            )
            node.children.append(
                _parse_region(
                    source, child_start, child_end, child_path, leaf_counter
                )
            )
        return node

    if content_type == "message/rfc822":
        node = PartNode(
            kind="rfc822",
            content_type=content_type,
            filename=filename,
            path=parent_path,
        )
        # A message/rfc822 body is itself a complete message.
        child_path = parent_path + (
            PathEntry("rfc822", 0, content_type, filename),
        )
        node.children.append(
            _parse_region(
                source, body_start, body_end, child_path, leaf_counter
            )
        )
        return node

    return _build_leaf(
        source, body_start, body_end, parent_path, content_type, filename,
        headers, leaf_counter,
    )


def _build_leaf(
    source: bytes,
    body_start: int,
    body_end: int,
    path: Tuple[PathEntry, ...],
    content_type: str,
    filename: Optional[str],
    headers: Dict[str, str],
    leaf_counter: List[int],
) -> PartNode:
    encoding = (
        headers.get("content-transfer-encoding", "")
        .strip()
        .split(";", 1)[0]
        .strip()
        .lower()
        or "7bit"
    )
    body = source[body_start:body_end]
    if encoding == "base64":
        units, decoded_length, reason = map_base64(body, body_start)
    elif encoding in ("quoted-printable", "quoted printable"):
        units, decoded_length, reason = map_quoted_printable(body, body_start)
    else:
        # 7bit / 8bit / binary / unknown encodings: bytes map one-to-one.
        units, decoded_length, reason = map_identity(body, body_start)
        if encoding not in ("7bit", "8bit", "binary"):
            encoding = "identity"

    node = PartNode(
        kind="leaf",
        content_type=content_type or "application/octet-stream",
        filename=filename,
        path=path,
        encoding=encoding,
        units=units,
        decoded_length=decoded_length,
        raw_body_start=body_start,
        raw_body_end=body_end,
        corrupted=reason is not None,
        corruption_reason=reason,
    )
    node.leaf_index = leaf_counter[0]
    leaf_counter[0] += 1
    return node


def _split_headers(
    source: bytes, start: int, end: int
) -> Tuple[Dict[str, str], int, int]:
    """Return (headers, body_start, body_end) for a message region."""
    pos = start
    blank = -1
    while pos < end:
        line_end = source.find(b"\n", pos, end)
        if line_end == -1:
            break
        if line_end == pos or (line_end == pos + 1 and source[pos] == 0x0D):
            blank = line_end  # blank line
            break
        pos = line_end + 1

    if blank == -1:
        raise MimeParseError(
            f"message region [{start}, {end}) has no header/body separator"
        )
    header_bytes = source[start:blank]
    body_start = blank + 1
    return _parse_headers(header_bytes, start), body_start, end


def _parse_headers(data: bytes, region_start: int) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    # Unfold CRLF (or bare LF) continuation lines into single logical lines.
    lines: List[bytes] = []
    for raw_line in data.split(b"\n"):
        if raw_line.endswith(b"\r"):
            raw_line = raw_line[:-1]
        if raw_line[:1] in (b" ", b"\t") and lines:
            lines[-1] = lines[-1] + b" " + raw_line[1:].lstrip()
        elif raw_line:
            lines.append(raw_line)

    for line in lines:
        colon = line.find(b":")
        if colon <= 0:
            raise MimeParseError(
                f"malformed header line at raw offset near {region_start}: "
                f"{line[:40]!r}"
            )
        name = line[:colon]
        if any(byte not in _HEADER_TOKEN_CHARS for byte in name):
            raise MimeParseError(
                f"illegal header name {name!r} at raw offset near "
                f"{region_start}"
            )
        value = line[colon + 1 :].strip().decode("ascii", "surrogateescape")
        headers[name.decode("ascii").lower()] = value
    return headers


def _parse_header_value(value: str) -> Tuple[str, Dict[str, str]]:
    """Split a structured header value into its main value and parameters."""
    parts = _split_top_level(value, ";")
    if not parts:
        return "", {}
    main = parts[0].strip().lower()
    params: Dict[str, str] = {}
    for chunk in parts[1:]:
        if "=" not in chunk:
            continue
        key, raw_value = chunk.split("=", 1)
        key = key.strip().lower()
        raw_value = raw_value.strip()
        if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] == '"':
            raw_value = raw_value[1:-1]
        params[key] = _decode_maybe(raw_value)
    return main, params

def _split_top_level(value: str, separator: str) -> List[str]:
    parts: List[str] = []
    buf: List[str] = []
    in_quotes = False
    for char in value:
        if char == '"':
            in_quotes = not in_quotes
            buf.append(char)
        elif char == separator and not in_quotes:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    parts.append("".join(buf))
    return parts

def _decode_maybe(value: str) -> str:
    if "=?" not in value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError, ValueError):
        return value


def _filename_from_headers(headers: Dict[str, str]) -> Optional[str]:
    disposition = headers.get("content-disposition", "")
    if disposition:
        _, params = _parse_header_value(disposition)
        if params.get("filename"):
            return params["filename"]
    content_type = headers.get("content-type", "")
    if content_type:
        _, params = _parse_header_value(content_type)
        if params.get("name"):
            return params["name"]
    return None


def _scan_multipart(
    source: bytes, start: int, end: int, boundary: str
) -> List[Tuple[int, int]]:
    """Return absolute ``[start, end)`` regions of the multipart children."""
    delimiter = b"--" + boundary.encode("ascii", "surrogateescape")
    regions: List[Tuple[int, int]] = []
    part_start: Optional[int] = None
    pos = start

    while pos < end:
        line_end = source.find(b"\n", pos, end)
        next_pos = end if line_end == -1 else line_end + 1
        line = source[pos:next_pos]
        stripped = line[:-2] if line.endswith(b"\r\n") else (
            line[:-1] if line.endswith(b"\n") else line
        )

        if stripped.startswith(delimiter):
            remainder = stripped[len(delimiter) :]
            # A valid boundary line is exactly "--boundary", optionally
            # followed by transport linear whitespace, or "--boundary--"
            # (the closing delimiter).  Anything else (e.g. a body line that
            # merely starts with the boundary text) is body text.
            if remainder == b"--":
                closing = True
            elif not remainder:
                closing = False
            elif all(byte in b" \t" for byte in remainder):
                closing = False
            else:
                pos = next_pos
                continue

            if part_start is not None:
                # The CRLF immediately preceding the boundary delimiter line
                # is transport framing, not body evidence: exclude it from
                # the child region so boundary lines and framing line breaks
                # can never appear in a located range.
                part_end = pos
                if part_end - part_start >= 2 and source[
                    part_end - 2 : part_end
                ] == b"\r\n":
                    part_end -= 2
                elif part_end > part_start and source[part_end - 1] == 0x0A:
                    part_end -= 1
                regions.append((part_start, part_end))
                part_start = None

            if closing:
                return regions
            part_start = next_pos  # content starts after the delimiter's EOL
        pos = next_pos

    raise MimeParseError(
        f"multipart boundary {boundary!r} has no closing delimiter"
    )
