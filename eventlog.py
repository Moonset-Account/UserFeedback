"""Structured event log writer/reader.

Security goal: the log backend trusts severity / event_id metadata, so the
import path must guarantee that free-text (message body, application fields)
can never reshape a record's trusted metadata.  This module achieves that by:

* Validating trusted metadata (severity, event_id) before anything is written.
* Isolating application fields from reserved metadata keys (conflicts are
  rejected with the offending field name).
* Framing every record with an explicit byte-length header, so arbitrary
  newlines, control characters or delimiter-like bytes inside the body cannot
  bleed into neighbouring records.
* Serialising each record fully in memory before a single write, so one
  malformed record in a batch never emits partial bytes and never alters the
  trusted metadata of the other events.

Record wire format (all integers decimal ASCII)::

    EVL1 <payload-byte-length>\n
    <payload bytes (UTF-8 JSON)>\n

The payload is a JSON object: {"severity", "event_id", "message", "fields"}.
JSON string escaping plus length-prefix framing makes the body round-trip
byte-exact for any Unicode content.
"""

from __future__ import annotations

import json
import re

MAGIC = b"EVL1"
TRAILER = b"\n"
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024  # sanity bound for length headers

SEVERITIES = ("DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL")

#: Metadata keys owned by the event envelope.  Application custom fields may
#: not reuse them; attempts are rejected with the conflicting field name.
RESERVED_FIELDS = frozenset({"severity", "event_id", "message", "timestamp"})

_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$")


class EventLogError(Exception):
    """Base class for all event log errors."""


class ValidationError(EventLogError):
    """A record failed validation and was not written."""


class ReservedFieldError(ValidationError):
    """An application field collides with a reserved metadata field."""

    def __init__(self, field):
        self.field = field
        super().__init__(
            "application field %r conflicts with reserved metadata field" % (field,)
        )


class CorruptLogError(EventLogError):
    """The log file is structurally corrupt in the middle of the stream."""


class TruncatedRecordError(EventLogError):
    """The final record of the stream is incomplete."""

    def __init__(self, index, reason, expected=None, received=None):
        self.index = index
        self.reason = reason
        self.expected = expected
        self.received = received
        detail = "truncated record at index %d: %s" % (index, reason)
        if expected is not None:
            detail += " (expected %d bytes, received %d)" % (expected, received)
        super().__init__(detail)


def validate_event(severity, event_id, message, fields):
    """Validate one event; raise ValidationError/ReservedFieldError on problems."""
    if severity not in SEVERITIES:
        raise ValidationError(
            "invalid severity %r; must be one of %s" % (severity, ", ".join(SEVERITIES))
        )
    if not isinstance(event_id, str) or not _EVENT_ID_RE.match(event_id):
        raise ValidationError(
            "invalid event_id %r; 1-128 chars of [A-Za-z0-9_.:-], "
            "starting with alphanumeric" % (event_id,)
        )
    if not isinstance(message, str):
        raise ValidationError("message must be a str, got %s" % type(message).__name__)
    if fields is None:
        fields = {}
    if not isinstance(fields, dict):
        raise ValidationError("fields must be a dict, got %s" % type(fields).__name__)
    for key, value in fields.items():
        if not isinstance(key, str) or not key:
            raise ValidationError("application field names must be non-empty strings")
        if key in RESERVED_FIELDS:
            raise ReservedFieldError(key)
        try:
            json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "field %r is not JSON-serializable: %s" % (key, exc)
            )
    return fields


def build_record(severity, event_id, message, fields=None):
    """Validate and serialise one event; return the framed record as bytes."""
    fields = validate_event(severity, event_id, message, fields)
    payload = json.dumps(
        {
            "severity": severity,
            "event_id": event_id,
            "message": message,
            "fields": fields,
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValidationError("record payload too large: %d bytes" % len(payload))
    header = MAGIC + b" " + str(len(payload)).encode("ascii") + b"\n"
    return header + payload + TRAILER


def write_event(fileobj, severity, event_id, message, fields=None):
    """Write a single framed record to a binary file object."""
    record = build_record(severity, event_id, message, fields)
    fileobj.write(record)
    return len(record)


def write_batch(fileobj, events):
    """Write many events; one malformed event never affects the others.

    Each event is a mapping with keys severity, event_id, message and an
    optional fields dict.  Every record is fully validated and serialised in
    memory before its bytes touch the stream, so a failing record emits no
    partial bytes and cannot shift the framing (and therefore the trusted
    metadata) of any other record.

    Returns (written_count, errors) where errors is a list of
    (index, exception) tuples for the rejected events.
    """
    written = 0
    errors = []
    for index, event in enumerate(events):
        try:
            record = build_record(
                event.get("severity"),
                event.get("event_id"),
                event.get("message"),
                event.get("fields"),
            )
        except EventLogError as exc:
            errors.append((index, exc))
            continue
        fileobj.write(record)
        written += 1
    return written, errors


def _read_exact(fileobj, count):
    data = fileobj.read(count)
    return data if data is not None else b""


def read_events(fileobj):
    """Read all records from a binary file object.

    Returns (events, truncation):
      * events: list of dicts with keys severity, event_id, message, fields.
      * truncation: None if the stream ended cleanly at a record boundary,
        otherwise a dict {index, reason, expected, received} describing the
        incomplete final record.

    A structurally corrupt record in the *middle* of the stream raises
    CorruptLogError instead, because offsets after it cannot be trusted.
    """
    events = []
    index = 0
    while True:
        header = fileobj.readline()
        if header in (b"", ""):
            return events, None  # clean EOF at a record boundary
        if not header.endswith(b"\n"):
            return events, {
                "index": index,
                "reason": "partial header (EOF before newline)",
                "expected": None,
                "received": len(header),
            }
        header = header.rstrip(b"\n")
        parts = header.split(b" ")
        if len(parts) != 2 or parts[0] != MAGIC or not parts[1].isdigit():
            raise CorruptLogError(
                "bad record header at index %d: %r" % (index, header[:64])
            )
        length = int(parts[1])
        if length > MAX_PAYLOAD_BYTES:
            raise CorruptLogError(
                "implausible payload length %d at index %d" % (length, index)
            )
        payload = _read_exact(fileobj, length)
        if len(payload) < length:
            return events, {
                "index": index,
                "reason": "payload cut off by EOF",
                "expected": length,
                "received": len(payload),
            }
        trailer = _read_exact(fileobj, 1)
        if trailer != TRAILER:
            if trailer == b"":
                return events, {
                    "index": index,
                    "reason": "missing record trailer (EOF)",
                    "expected": 1,
                    "received": 0,
                }
            raise CorruptLogError("bad record trailer at index %d" % index)
        try:
            obj = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CorruptLogError("undecodable payload at index %d: %s" % (index, exc))
        if not isinstance(obj, dict) or not {"severity", "event_id", "message"} <= set(obj):
            raise CorruptLogError("payload at index %d misses required keys" % index)
        events.append(
            {
                "severity": obj["severity"],
                "event_id": obj["event_id"],
                "message": obj["message"],
                "fields": obj.get("fields", {}),
            }
        )
        index += 1
