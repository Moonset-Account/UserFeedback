"""Streaming JSON redaction over arbitrary byte chunks.

Framing
-------
The input stream is a sequence of independent JSON records in JSON Lines
form: each record is one line containing a complete JSON value, records are
separated by ``\\n``.  Blank lines (whitespace only) are ignored and are not
counted as records.  Output is committed per complete record: a successfully
processed record is emitted (redacted) followed by ``\\n``; a failed record
emits nothing at all (atomic failure) and is reported through ``errors``.

Chunking
--------
``feed()`` accepts arbitrary byte chunks.  A record is only parsed once its
terminating newline (or end-of-stream via ``finish()``) has been seen, so
UTF-8 sequences, ``\\uXXXX`` escapes and surrogate pairs may be split across
chunks at any byte offset without changing the result.

Matching
--------
Only string *values* at selected field paths are scanned.  Matching is done
on decoded Unicode scalar values (raw UTF-8 and all escape forms, including
surrogate pairs, are decoded first).  At each position the longest matching
phrase wins; scanning then continues right after that match (leftmost-longest,
no overlap of matches).  The replacement text is re-encoded as a legal JSON
string body (UTF-8, control characters escaped).  Bytes that are not part of
a match are copied verbatim from the source record, preserving whitespace,
number spellings and untouched escape forms.

Failure model
-------------
A record fails as a whole (no partial output) when it is malformed JSON,
invalid UTF-8, contains non-standard constants (NaN/Infinity), or exceeds the
per-record buffer budget.  Later records are processed independently.  Error
entries contain only the record index and a reason code -- never record
content, so raw sensitive values cannot leak through errors.
"""

from __future__ import annotations

import json

__all__ = [
    "StreamRedactor",
    "RecordError",
    "parse_path",
    "ARRAY",
    "WILDCARD",
    "ERR_MALFORMED",
    "ERR_UTF8",
    "ERR_BUDGET",
]

ERR_MALFORMED = "malformed_json"
ERR_UTF8 = "invalid_utf8"
ERR_BUDGET = "budget_exceeded"

_QUOTE = 0x22
_BACKSLASH = 0x5C

_WS_BYTES = b" \t\r\n"
_WS_SET = frozenset(_WS_BYTES)
_DELIM_SET = frozenset(b" \t\r\n,]}")

_SIMPLE_ESCAPES = {
    0x22: 0x22,  # \"
    0x5C: 0x5C,  # \\
    0x2F: 0x2F,  # \/
    0x62: 0x08,  # \b
    0x66: 0x0C,  # \f
    0x6E: 0x0A,  # \n
    0x72: 0x0D,  # \r
    0x74: 0x09,  # \t
}


class _Sentinel:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


#: Path segment matching any array element position.
ARRAY = _Sentinel("ARRAY")
#: Path segment matching any single object key or array element position.
WILDCARD = _Sentinel("WILDCARD")


def parse_path(path):
    """Normalize a field path into a tuple of segments.

    Accepts dotted strings (``"user.name"``, ``"items[].name"``,
    ``"a.*.b"``) or iterables of segments.  ``"[]"`` selects every array
    element, ``"*"`` any single segment.  The empty string selects the
    document root (relevant when the whole record is a string).
    """
    if isinstance(path, str):
        if path == "":
            return ()
        parts = path.split(".")
    else:
        parts = list(path)
    segments = []
    for part in parts:
        if part is ARRAY or part is WILDCARD:
            segments.append(part)
            continue
        if not isinstance(part, str) or not part:
            raise ValueError("invalid path segment: %r" % (part,))
        while part.endswith("[]"):
            # "items[]" selects every element of the array field "items";
            # the key and the array position become two segments.
            head = part[:-2]
            if head:
                segments.append(head)
            segments.append(ARRAY)
            part = ""
        if part == "*":
            segments.append(WILDCARD)
        elif part:
            segments.append(part)
    return tuple(segments)


def _decode_scalars(content):
    """Decode a JSON string body into ``(codepoint, start, end)`` triples.

    Offsets are relative to ``content`` and delimit the exact source bytes
    that produced each scalar (escape sequence or UTF-8 sequence), so
    unmatched scalars can be re-emitted byte-identically.  ``content`` must
    be a valid JSON string body (the record is validated before scanning).
    A lone surrogate escape decodes to its own surrogate code point.
    """
    out = []
    i = 0
    n = len(content)
    while i < n:
        b = content[i]
        if b == _BACKSLASH:
            kind = content[i + 1]
            if kind == 0x75:  # 'u'
                hi = int(content[i + 2 : i + 6], 16)
                if 0xD800 <= hi <= 0xDBFF and content[i + 6 : i + 8] == b"\\u":
                    lo = int(content[i + 8 : i + 12], 16)
                    if 0xDC00 <= lo <= 0xDFFF:
                        cp = 0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00)
                        out.append((cp, i, i + 12))
                        i += 12
                        continue
                out.append((hi, i, i + 6))
                i += 6
                continue
            out.append((_SIMPLE_ESCAPES[kind], i, i + 2))
            i += 2
            continue
        if b < 0x80:
            out.append((b, i, i + 1))
            i += 1
            continue
        if b < 0xC0:
            raise ValueError("stray UTF-8 continuation byte")
        if b < 0xE0:
            size = 2
        elif b < 0xF0:
            size = 3
        else:
            size = 4
        cp = ord(content[i : i + size].decode("utf-8"))
        out.append((cp, i, i + size))
        i += size
    return out


def _decode_key(content):
    return "".join(chr(cp) for cp, _, _ in _decode_scalars(content))


def _build_trie(phrases):
    root = {}
    for phrase in phrases:
        if not phrase:
            raise ValueError("empty phrase is not allowed")
        node = root
        for ch in phrase:
            node = node.setdefault(ord(ch), {})
        node[None] = None  # terminal marker
    return root


def _longest_match(trie, scalars, pos):
    """Length (in scalars) of the longest phrase starting at ``pos``."""
    node = trie
    best = 0
    depth = 0
    n = len(scalars)
    k = pos
    while k < n:
        node = node.get(scalars[k][0])
        if node is None:
            break
        depth += 1
        if None in node:
            best = depth
        k += 1
    return best


def _redact_string(content, trie, replacement):
    """Redact one string body, preserving unmatched source bytes."""
    scalars = _decode_scalars(content)
    out = bytearray()
    out.append(_QUOTE)
    i = 0
    total = len(scalars)
    while i < total:
        length = _longest_match(trie, scalars, i)
        if length:
            out.extend(replacement)
            i += length
        else:
            _, start, end = scalars[i]
            out.extend(content[start:end])
            i += 1
    out.append(_QUOTE)
    return bytes(out)


class _RecordProcessor:
    """Single-pass structural walk over one validated record.

    Copies every source byte except the bodies of selected string values,
    which are replaced by their redacted form.
    """

    __slots__ = ("rec", "n", "patterns", "trie", "repl", "out")

    def __init__(self, record, patterns, trie, repl):
        self.rec = record
        self.n = len(record)
        self.patterns = patterns
        self.trie = trie
        self.repl = repl
        self.out = bytearray()

    def run(self):
        i = self._value(0, ())
        i = self._ws(i)
        if i != self.n:
            raise ValueError("trailing data after top-level value")
        return bytes(self.out)

    def _ws(self, i):
        j = i
        rec = self.rec
        n = self.n
        while j < n and rec[j] in _WS_SET:
            j += 1
        self.out.extend(rec[i:j])
        return j

    def _string_end(self, i):
        rec = self.rec
        n = self.n
        i += 1
        while i < n:
            c = rec[i]
            if c == _BACKSLASH:
                i += 2
            elif c == _QUOTE:
                return i + 1
            else:
                i += 1
        raise ValueError("unterminated string")

    def _selected(self, path):
        for pattern in self.patterns:
            if len(pattern) != len(path):
                continue
            for want, got in zip(pattern, path):
                if want is WILDCARD:
                    continue
                if want is ARRAY:
                    if got is not ARRAY:
                        break
                elif want != got:
                    break
            else:
                return True
        return False

    def _value(self, i, path):
        i = self._ws(i)
        c = self.rec[i]
        if c == _QUOTE:
            end = self._string_end(i)
            if self._selected(path):
                self.out.extend(
                    _redact_string(self.rec[i + 1 : end - 1], self.trie, self.repl)
                )
            else:
                self.out.extend(self.rec[i:end])
            return end
        if c == 0x7B:  # '{'
            self.out.append(c)
            i = self._ws(i + 1)
            if self.rec[i] == 0x7D:  # '}'
                self.out.append(0x7D)
                return i + 1
            while True:
                end = self._string_end(i)
                key = _decode_key(self.rec[i + 1 : end - 1])
                self.out.extend(self.rec[i:end])
                i = self._ws(end)
                if self.rec[i] != 0x3A:  # ':'
                    raise ValueError("expected ':'")
                self.out.append(0x3A)
                i = self._value(i + 1, path + (key,))
                i = self._ws(i)
                c = self.rec[i]
                self.out.append(c)
                i += 1
                if c == 0x2C:  # ','
                    i = self._ws(i)
                    continue
                if c != 0x7D:
                    raise ValueError("expected '}'")
                return i
        if c == 0x5B:  # '['
            self.out.append(c)
            i = self._ws(i + 1)
            if self.rec[i] == 0x5D:  # ']'
                self.out.append(0x5D)
                return i + 1
            while True:
                i = self._value(i, path + (ARRAY,))
                i = self._ws(i)
                c = self.rec[i]
                self.out.append(c)
                i += 1
                if c == 0x2C:  # ','
                    continue
                if c != 0x5D:
                    raise ValueError("expected ']'")
                return i
        # number or literal: copy verbatim up to the next delimiter
        j = i
        rec = self.rec
        n = self.n
        while j < n and rec[j] not in _DELIM_SET:
            j += 1
        if j == i:
            raise ValueError("empty value")
        self.out.extend(rec[i:j])
        return j


def _reject_constant(value):
    raise ValueError("non-standard JSON constant: %s" % value)


class RecordError:
    """Failure of one record.  Carries no record content by design."""

    __slots__ = ("record_index", "reason")

    def __init__(self, record_index, reason):
        self.record_index = record_index
        self.reason = reason

    def __repr__(self):
        return "RecordError(record_index=%d, reason=%r)" % (
            self.record_index,
            self.reason,
        )

    def __eq__(self, other):
        return (
            isinstance(other, RecordError)
            and self.record_index == other.record_index
            and self.reason == other.reason
        )


class StreamRedactor:
    """Incremental redactor for a JSON Lines byte stream.

    Parameters
    ----------
    paths:
        Iterable of field paths (see :func:`parse_path`).  Only string
        values at these paths are scanned for phrases.
    phrases:
        Iterable of sensitive phrases (``str``).  Matched on decoded
        Unicode scalar values; empty phrases are rejected.
    replacement:
        Replacement text, re-encoded as a legal JSON string body.
    max_record_bytes:
        Per-record buffer budget.  A record whose buffered bytes exceed
        this fails with ``budget_exceeded`` and is skipped.
    """

    def __init__(self, paths, phrases, replacement, max_record_bytes=1 << 20):
        if max_record_bytes <= 0:
            raise ValueError("max_record_bytes must be positive")
        self._patterns = [parse_path(p) for p in paths]
        self._trie = _build_trie(phrases)
        encoded = json.dumps(replacement, ensure_ascii=False)[1:-1]
        try:
            self._repl = encoded.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("replacement is not UTF-8 encodable") from None
        self._max = max_record_bytes
        self._buf = bytearray()
        self._skipping = False
        self._index = 0
        #: List of :class:`RecordError` for every failed record, in order.
        self.errors = []

    @property
    def next_record_index(self):
        """Index the next record will get (counts records, not blank lines)."""
        return self._index

    def feed(self, chunk):
        """Consume a chunk; return output bytes for records completed by it."""
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("feed() expects a bytes-like object")
        data = bytes(chunk)
        out = bytearray()
        pos = 0
        n = len(data)
        while pos < n:
            if self._skipping:
                nl = data.find(b"\n", pos)
                if nl < 0:
                    return bytes(out)
                pos = nl + 1
                self._skipping = False
                continue
            nl = data.find(b"\n", pos)
            if nl < 0:
                self._buf.extend(data[pos:])
                pos = n
                if len(self._buf) > self._max:
                    self._buf.clear()
                    self._skipping = True
                    self._fail(ERR_BUDGET)
                continue
            self._buf.extend(data[pos:nl])
            pos = nl + 1
            line = bytes(self._buf)
            del self._buf[:]
            emitted = self._record(line)
            if emitted is not None:
                out.extend(emitted)
                out.append(0x0A)
        return bytes(out)

    def finish(self):
        """Flush end-of-stream; a trailing partial record fails atomically."""
        out = bytearray()
        if self._skipping:
            # The oversized record was already failed when it blew the budget.
            self._skipping = False
            self._buf.clear()
        elif self._buf:
            line = bytes(self._buf)
            del self._buf[:]
            emitted = self._record(line)
            if emitted is not None:
                out.extend(emitted)
                out.append(0x0A)
        return bytes(out)

    def _fail(self, reason):
        self.errors.append(RecordError(self._index, reason))
        self._index += 1

    def _record(self, line):
        if not line.strip(_WS_BYTES):
            return None  # blank separator line: not a record
        if len(line) > self._max:
            self._fail(ERR_BUDGET)
            return None
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            self._fail(ERR_UTF8)
            return None
        try:
            json.loads(text, parse_constant=_reject_constant)
        except ValueError:
            self._fail(ERR_MALFORMED)
            return None
        try:
            result = _RecordProcessor(line, self._patterns, self._trie, self._repl).run()
        except Exception:
            self._fail(ERR_MALFORMED)
            return None
        self._index += 1
        return result
