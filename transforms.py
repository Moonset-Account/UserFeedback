"""Equivalence transforms over CSV text, used by the property tests.

Each transform rewrites the *representation* of a CSV document while
preserving the *logical* records the importer produces:

* ``transform_newlines`` - rewrites record separators (LF/CRLF/CR) but
  never touches newlines inside quoted fields.
* ``transform_columns``  - permutes columns of every full-width row
  (header included); short/long rows are kept verbatim so that
  ROW_FIELD_COUNT classifications are preserved.
* ``transform_quoting``  - re-serializes with a different quoting style
  (minimal / quote everything / quote empty fields).
"""

from importer import CsvTokenizer

NEWLINE_STYLES = {"lf": "\n", "crlf": "\r\n", "cr": "\r"}
QUOTING_MODES = ("minimal", "all", "quote_empty")


def parse_rows(text):
    """Parse *well-formed* CSV text into a list of rows (lists of str)."""
    tok = CsvTokenizer()
    events = tok.feed(text) + tok.close()
    rows = []
    for ev in events:
        if ev[0] != "row":
            raise ValueError("sample is not cleanly round-trippable: %s" % (ev,))
        rows.append(ev[1])
    return rows


def serialize_field(value, quoting):
    need = any(c in value for c in '",\n\r')
    if (
        quoting == "all"
        or (quoting == "minimal" and need)
        or (quoting == "quote_empty" and (need or value == ""))
    ):
        return '"' + value.replace('"', '""') + '"'
    return value


def serialize_rows(rows, newline="\n", quoting="minimal"):
    out = []
    for row in rows:
        if len(row) == 1 and row[0] == "":
            # A lone empty unquoted field would be a blank line, which the
            # importer skips; force quoting so the row survives.
            out.append('""')
        else:
            out.append(",".join(serialize_field(v, quoting) for v in row))
    return newline.join(out) + (newline if out else "")


def apply_perm(row, perm):
    """new_row[k] = row[perm[k]]; rows of a different width are kept as-is."""
    if len(row) != len(perm):
        return list(row)
    return [row[i] for i in perm]


def transform_newlines(text, newline):
    """Rewrite record separators to ``newline`` (one of \\n, \\r\\n, \\r).

    Newlines inside quoted fields are field content and are left untouched.
    """
    out = []
    i = 0
    n = len(text)
    in_quotes = False
    while i < n:
        ch = text[i]
        if in_quotes:
            out.append(ch)
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':
                    out.append('"')
                    i += 2
                    continue
                in_quotes = False
            i += 1
        else:
            if ch == '"':
                in_quotes = True
                out.append(ch)
                i += 1
            elif ch == "\r":
                if i + 1 < n and text[i + 1] == "\n":
                    i += 1
                out.append(newline)
                i += 1
            elif ch == "\n":
                out.append(newline)
                i += 1
            else:
                out.append(ch)
                i += 1
    return "".join(out)


def transform_columns(text, perm, newline="\n", quoting="minimal"):
    rows = parse_rows(text)
    return serialize_rows([apply_perm(r, perm) for r in rows], newline, quoting)


def transform_quoting(text, quoting, newline="\n"):
    return serialize_rows(parse_rows(text), newline, quoting)
