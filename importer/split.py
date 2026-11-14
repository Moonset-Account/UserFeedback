"""Splitting an input file (JSON Lines or a JSON array) into record chunks.

Chunking is conservative: a JSON Lines record always ends at a single ``\n``
byte (never at Unicode line separators, which are legal inside JSON strings),
and an array element ends at the top-level ``,`` / ``]`` boundary.  The importer
measures each chunk as read, so a record that claims to be small is still
measured at its real byte length.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import INPUT_SIZE_LIMIT, INVALID_ENCODING, INVALID_JSON, ImportFailure
from .limits import Limits

BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class Chunk:
    """One record candidate: its 1-based line and the exact bytes read."""

    line: int
    raw: bytes


@dataclass(frozen=True)
class SplitResult:
    chunks: list[Chunk]
    failures: list[ImportFailure]


def detect_format(raw: bytes, limits: Limits) -> str:
    """Pick ``jsonl`` or ``json_array`` from the first non-space byte."""

    if len(raw) > limits.max_file_bytes:
        raise ImportFailure(
            INPUT_SIZE_LIMIT,
            "$",
            f"input is {len(raw)} bytes, exceeds max_file_bytes "
            f"{limits.max_file_bytes}",
        )

    body = raw[len(BOM):] if raw.startswith(BOM) else raw
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ImportFailure(
            INVALID_ENCODING,
            "$",
            f"input is not valid UTF-8 (byte offset {exc.start})",
            column=exc.start + 1,
        ) from exc

    if text.lstrip().startswith("["):
        return "json_array"
    return "jsonl"


def _split_jsonl(raw: bytes) -> SplitResult:
    """Split on ``\\n`` bytes only, keeping exact byte ranges."""

    chunks: list[Chunk] = []
    body = raw[len(BOM):] if raw.startswith(BOM) else raw

    for line_number, line in enumerate(body.split(b"\n"), start=1):
        if line.endswith(b"\r"):
            line = line[:-1]
        if not line.strip():
            continue
        chunks.append(Chunk(line=line_number, raw=line))

    return SplitResult(chunks, [])


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def _split_json_array(text: str) -> SplitResult:
    """Split a top-level JSON array into its element texts."""

    chunks: list[Chunk] = []
    failures: list[ImportFailure] = []

    i = _skip_ws(text, 0)
    if i >= len(text) or text[i] != "[":
        return SplitResult([], [])  # caller chose the format; defensive only
    i = _skip_ws(text, i + 1)
    if i < len(text) and text[i] == "]":
        return _trailing_check(text, i + 1, chunks, failures)

    while True:
        start = i
        depth = 0
        in_string = False
        j = i
        while j < len(text):
            char = text[j]
            if in_string:
                if char == "\\":
                    j += 2
                    continue
                if char == '"':
                    in_string = False
                j += 1
                continue
            if char == '"':
                in_string = True
                j += 1
                continue
            if char in "{[":
                depth += 1
            elif char in "}]":
                if depth == 0:
                    j += 1 if char == "}" else 0
                    break
                depth -= 1
            elif char == "," and depth == 0:
                break
            j += 1

        chunk_text = text[start:j].rstrip()
        line = text.count("\n", 0, start) + 1
        if chunk_text:
            chunks.append(Chunk(line=line, raw=chunk_text.encode("utf-8")))
        else:
            failures.append(
                ImportFailure(INVALID_JSON, "$", "empty array element", line=line)
            )

        j = _skip_ws(text, j)
        if j >= len(text):
            failures.append(
                ImportFailure(INVALID_JSON, "$", "unterminated JSON array", line=line)
            )
            break
        if text[j] == ",":
            i = _skip_ws(text, j + 1)
            continue
        if text[j] == "]":
            return _trailing_check(text, j + 1, chunks, failures)
        # Stray character between elements.
        line = text.count("\n", 0, j) + 1
        failures.append(
            ImportFailure(
                INVALID_JSON,
                "$",
                f"unexpected character {text[j]!r} between array elements",
                line=line,
            )
        )
        i = _skip_ws(text, j + 1)

    return SplitResult(chunks, failures)


def _trailing_check(
    text: str, offset: int, chunks: list[Chunk], failures: list[ImportFailure]
) -> SplitResult:
    """Reject non-whitespace content after the top-level array's ``]``."""

    remainder = text[offset:]
    if remainder.strip():
        line = text.count("\n", 0, offset + len(remainder) - len(remainder.lstrip())) + 1
        failures.append(
            ImportFailure(
                INVALID_JSON,
                "$",
                "trailing content after the top-level JSON array",
                line=line,
            )
        )
    return SplitResult(chunks, failures)


def split_records(raw: bytes, limits: Limits) -> SplitResult:
    """Split a validated input into per-record chunks."""

    fmt = detect_format(raw, limits)
    if fmt == "json_array":
        body = raw[len(BOM):] if raw.startswith(BOM) else raw
        return _split_json_array(body.decode("utf-8"))
    return _split_jsonl(raw)
