"""Hand-written expected import results for every file in samples/.

These constants are the single source of truth for both the unittest
suite (tests/test_rejection.py) and the verification wrapper
(tools/local_verify.py).  They were derived from the format
specification in importer.py, not from the implementation's output.

A signature is::

    {"contacts": [{"name":..,"email":..,"phone":..,"note":..}, ...],
     "errors":   [{"record": <logical record no>, "category": ...}, ...]}
"""

from importer import (
    HEADER_DUPLICATE_COLUMN,
    HEADER_MISSING_REQUIRED,
    HEADER_UNKNOWN_COLUMN,
    ROW_EMPTY_NAME,
    ROW_FIELD_COUNT,
    ROW_INVALID_EMAIL,
    ROW_QUOTE_SYNTAX,
    ROW_UNTERMINATED_QUOTE,
)


def contact(name, email, phone="", note=""):
    return {"name": name, "email": email, "phone": phone, "note": note}


def err(record, category):
    return {"record": record, "category": category}


EXPECTED_SIGNATURES = {
    "valid_basic": {
        "contacts": [
            contact("Alice", "alice@example.com", "123-456", "friend"),
            contact("Bob", "bob@example.org"),
        ],
        "errors": [],
    },
    "valid_quoted": {
        "contacts": [
            contact("Smith, Jane", "jane@example.com", "", "line1\nline2"),
            contact('Say "hi"', "hi@example.net", "", "plain"),
        ],
        "errors": [],
    },
    "valid_minimal": {
        "contacts": [contact("Carol", "carol@example.io")],
        "errors": [],
    },
    "valid_header_only": {"contacts": [], "errors": []},
    "valid_no_trailing_newline": {
        "contacts": [contact("Dave", "dave@example.dev")],
        "errors": [],
    },
    "empty": {"contacts": [], "errors": []},
    "mixed_valid_invalid": {
        "contacts": [
            contact("Alice", "alice@example.com", "1", "ok"),
            contact("Carol", "carol@example.net"),
        ],
        "errors": [err(3, ROW_FIELD_COUNT), err(4, ROW_INVALID_EMAIL)],
    },
    "invalid_missing_required": {
        "contacts": [],
        "errors": [err(1, HEADER_MISSING_REQUIRED)],
    },
    "invalid_duplicate_column": {
        "contacts": [],
        "errors": [err(1, HEADER_DUPLICATE_COLUMN)],
    },
    "invalid_unknown_column": {
        "contacts": [],
        "errors": [err(1, HEADER_UNKNOWN_COLUMN)],
    },
    "invalid_field_count": {
        "contacts": [contact("Bob", "bob@example.org")],
        "errors": [err(2, ROW_FIELD_COUNT)],
    },
    "invalid_quote_syntax": {
        "contacts": [contact("Carol", "carol@example.net")],
        "errors": [err(2, ROW_QUOTE_SYNTAX)],
    },
    "invalid_unterminated": {
        "contacts": [contact("Alice", "alice@example.com")],
        "errors": [err(3, ROW_UNTERMINATED_QUOTE)],
    },
    "invalid_empty_name": {
        "contacts": [],
        "errors": [err(2, ROW_EMPTY_NAME)],
    },
    "invalid_email": {
        "contacts": [],
        "errors": [err(2, ROW_INVALID_EMAIL)],
    },
    "invalid_multiline_record_numbering": {
        "contacts": [contact("Alice", "alice@example.com", "", "line1\nline2")],
        "errors": [err(3, ROW_INVALID_EMAIL)],
    },
}
