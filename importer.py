"""Streaming contacts CSV importer (Python 3 standard library only).

Input format
------------
* The first logical record is the header.  Allowed columns are ``name`` and
  ``email`` (required) plus ``phone`` and ``note`` (optional).  Column order
  is free; unknown, duplicated or missing-required columns are fatal header
  errors and stop all further output.
* Records may be separated by LF, CRLF or CR.  Quoted fields may embed
  commas, newlines and doubled quotes (``""``).
* A double quote is only special at the very start of a field.  A quote
  anywhere else, or any character other than a delimiter / newline / EOF
  right after a closing quote, is a quote-syntax error: the whole logical
  record is rejected and parsing resumes at the next record separator.
* Blank physical lines (no characters at all) are skipped and do not
  consume a logical record number.
* Logical records are numbered from 1 (the header is record 1).  Newlines
  embedded inside quoted fields do NOT advance the record number, so error
  record numbers are stable no matter how the input is chunked.

Streaming
---------
``ContactImporter.feed(chunk)`` may be called with arbitrary chunks; the
result is required to be identical for every chunking of the same text.
``ContactImporter.close()`` must be called once at end of stream; it is
where ``ROW_UNTERMINATED_QUOTE`` is raised and where a final record without
a trailing newline is flushed.
"""

import re

REQUIRED_COLUMNS = ("name", "email")
OPTIONAL_COLUMNS = ("phone", "note")
ALLOWED_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

# Stable rejection categories.
HEADER_MISSING_REQUIRED = "HEADER_MISSING_REQUIRED"
HEADER_DUPLICATE_COLUMN = "HEADER_DUPLICATE_COLUMN"
HEADER_UNKNOWN_COLUMN = "HEADER_UNKNOWN_COLUMN"
ROW_FIELD_COUNT = "ROW_FIELD_COUNT"
ROW_QUOTE_SYNTAX = "ROW_QUOTE_SYNTAX"
ROW_UNTERMINATED_QUOTE = "ROW_UNTERMINATED_QUOTE"
ROW_EMPTY_NAME = "ROW_EMPTY_NAME"
ROW_INVALID_EMAIL = "ROW_INVALID_EMAIL"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CsvTokenizer(object):
    """Incremental CSV tokenizer.

    ``feed()`` and ``close()`` return lists of events:

    * ``("row", fields)``   - a complete logical row (list of str)
    * ``("bad_row",)``      - the current row was discarded after a
                              misplaced-quote syntax error
    * ``("unterminated",)`` - EOF reached inside a quoted field
                              (only emitted by ``close()``)
    """

    def __init__(self):
        self._state = "start"  # start | field | quotes | quote_in_quotes | skip | skip_cr
        self._cur = []
        self._fields = []
        self._field_started = False
        self._skip_lf = False  # previous char was a CR record separator

    # -- internal helpers --------------------------------------------
    def _end_field(self):
        self._fields.append("".join(self._cur))
        self._cur = []
        self._field_started = False

    def _end_row(self, events):
        if self._field_started or self._fields:
            if self._field_started:
                self._end_field()
            events.append(("row", self._fields))
        # else: blank physical line -> skipped, no event, no record number
        self._fields = []
        self._cur = []
        self._field_started = False
        self._state = "start"

    def _poison(self, events):
        events.append(("bad_row",))
        self._cur = []
        self._fields = []
        self._field_started = False
        self._state = "skip"

    # -- public API ---------------------------------------------------
    def feed(self, text):
        events = []
        for ch in text:
            if self._skip_lf:
                self._skip_lf = False
                if ch == "\n":
                    continue
            self._step(ch, events)
        return events

    def close(self):
        events = []
        if self._state == "quotes":
            events.append(("unterminated",))
        elif self._state == "quote_in_quotes":
            # The last character was a closing quote.
            self._state = "start"
            self._end_row(events)
        elif self._state in ("skip", "skip_cr"):
            pass  # poisoned row already reported, nothing more to emit
        else:
            self._end_row(events)  # flush final record without trailing newline
        self._state = "start"
        self._cur = []
        self._fields = []
        self._field_started = False
        self._skip_lf = False
        return events

    # -- state machine -------------------------------------------------
    def _step(self, ch, events):
        st = self._state
        if st == "quotes":
            if ch == '"':
                self._state = "quote_in_quotes"
            else:
                self._cur.append(ch)  # embedded newlines kept verbatim
        elif st == "quote_in_quotes":
            if ch == '"':
                self._cur.append('"')
                self._state = "quotes"
            elif ch == ",":
                self._end_field()
                self._field_started = True
                self._state = "start"
            elif ch == "\n":
                self._end_row(events)
            elif ch == "\r":
                self._end_row(events)
                self._skip_lf = True
            else:
                self._poison(events)
        elif st == "skip":
            if ch == "\n":
                self._state = "start"
            elif ch == "\r":
                self._state = "skip_cr"
        elif st == "skip_cr":
            self._state = "start"
            if ch == "\n":
                return
            self._step(ch, events)  # reprocess in start state
        elif st == "start":
            if ch == '"':
                self._state = "quotes"
                self._field_started = True
            elif ch == ",":
                self._end_field()
                self._field_started = True
            elif ch == "\n":
                self._end_row(events)
            elif ch == "\r":
                self._end_row(events)
                self._skip_lf = True
            else:
                self._cur.append(ch)
                self._field_started = True
                self._state = "field"
        else:  # "field"
            if ch == ",":
                self._end_field()
                self._field_started = True
                self._state = "start"
            elif ch == "\n":
                self._end_row(events)
            elif ch == "\r":
                self._end_row(events)
                self._skip_lf = True
            elif ch == '"':
                self._poison(events)
            else:
                self._cur.append(ch)


class ContactImporter(object):
    """Turns tokenizer events into contact records and classified errors."""

    def __init__(self):
        self._tok = CsvTokenizer()
        self._record_no = 0
        self._header = None
        self._fatal = False
        self.contacts = []
        self.errors = []

    def feed(self, text):
        for ev in self._tok.feed(text):
            self._handle(ev)

    def close(self):
        for ev in self._tok.close():
            self._handle(ev)

    def result(self):
        return {
            "contacts": [dict(c) for c in self.contacts],
            "errors": [dict(e) for e in self.errors],
        }

    # -- internals ------------------------------------------------------
    def _error(self, record, category, message):
        self.errors.append(
            {"record": record, "category": category, "message": message}
        )

    def _handle(self, ev):
        if self._fatal:
            return
        kind = ev[0]
        if kind == "unterminated":
            self._error(
                self._record_no + 1,
                ROW_UNTERMINATED_QUOTE,
                "end of stream inside a quoted field",
            )
            return
        if kind == "bad_row":
            self._record_no += 1
            self._error(
                self._record_no, ROW_QUOTE_SYNTAX, "misplaced double quote"
            )
            return
        # ("row", fields)
        self._record_no += 1
        fields = ev[1]
        if self._header is None:
            self._process_header(fields)
        else:
            self._process_row(fields)

    def _process_header(self, fields):
        seen = set()
        for name in fields:
            if name in seen:
                self._error(1, HEADER_DUPLICATE_COLUMN,
                            "duplicate column %r" % name)
                self._fatal = True
                return
            seen.add(name)
        for name in fields:
            if name not in ALLOWED_COLUMNS:
                self._error(1, HEADER_UNKNOWN_COLUMN,
                            "unknown column %r" % name)
                self._fatal = True
                return
        missing = [c for c in REQUIRED_COLUMNS if c not in seen]
        if missing:
            self._error(1, HEADER_MISSING_REQUIRED,
                        "missing required column(s): %s" % ",".join(missing))
            self._fatal = True
            return
        self._header = list(fields)

    def _process_row(self, fields):
        if len(fields) != len(self._header):
            self._error(
                self._record_no,
                ROW_FIELD_COUNT,
                "expected %d fields, got %d" % (len(self._header), len(fields)),
            )
            return
        raw = dict(zip(self._header, fields))
        contact = {c: raw.get(c, "") for c in ALLOWED_COLUMNS}
        if contact["name"] == "":
            self._error(self._record_no, ROW_EMPTY_NAME, "empty name")
            return
        if not EMAIL_RE.match(contact["email"]):
            self._error(self._record_no, ROW_INVALID_EMAIL,
                        "invalid email %r" % contact["email"])
            return
        self.contacts.append(contact)


def import_contacts(text, chunk_size=None):
    """One-shot helper: import ``text`` (optionally in fixed-size chunks)."""
    imp = ContactImporter()
    if chunk_size is None:
        imp.feed(text)
    else:
        for i in range(0, len(text), chunk_size):
            imp.feed(text[i:i + chunk_size])
    imp.close()
    return imp.result()
