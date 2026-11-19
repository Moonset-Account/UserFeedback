"""Column-aware normalization and keyed digest helpers.

The internal roster is preprocessed with a fixed test key.  Each column uses
its own normalization rule, and the digest is bound to the column name so a
value digest produced for one column can never be compared against another
column's digest even when the raw values coincide.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from typing import Callable, Dict

# Fixed test key.  It only exists so the pipeline can be exercised end to end
# without a real deployment secret; it is not a production credential.
TEST_KEY = b"feishu-nightly-20261120-fixed-test-key"

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(value: object) -> str:
    """Generic text normalization.

    Unicode NFKC folding, stripping of surrounding whitespace and collapsing
    of internal whitespace runs.  ``None`` becomes the empty string.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    return _WHITESPACE_RE.sub(" ", text).strip()


def normalize_name(value: object) -> str:
    """Name column: generic normalization plus case folding."""
    return normalize_text(value).casefold()


def normalize_email(value: object) -> str:
    """Email column: trim, casefold the address, remove internal spaces."""
    text = normalize_text(value).casefold()
    return text.replace(" ", "")


def normalize_department(value: object) -> str:
    """Department column: generic normalization plus case folding."""
    return normalize_text(value).casefold()


def normalize_employee_id(value: object) -> str:
    """Employee ID column: trim and uppercase, whitespace removed."""
    return normalize_text(value).replace(" ", "").upper()


def normalize_phone(value: object) -> str:
    """Phone column: keep digits only (leading '+' is dropped)."""
    text = normalize_text(value)
    return "".join(ch for ch in text if ch.isdigit())


# Per-column normalization rules.  Unknown columns fall back to the generic
# text rule, which still keeps their digest column-bound.
COLUMN_NORMALIZERS: Dict[str, Callable[[object], str]] = {
    "name": normalize_name,
    "email": normalize_email,
    "department": normalize_department,
    "employee_id": normalize_employee_id,
    "phone": normalize_phone,
}


def normalize_value(column: str, value: object) -> str:
    """Apply the normalization rule registered for ``column``."""
    normalizer = COLUMN_NORMALIZERS.get(column, normalize_text)
    return normalizer(value)


def digest_value(column: str, value: object, key: bytes = TEST_KEY) -> str:
    """Return the hex HMAC-SHA256 digest of a normalized cell value.

    The column name is part of the signed message, so digests are only ever
    comparable within the same column.
    """
    normalized = normalize_value(column, value)
    message = column.encode("utf-8") + b"\x1f" + normalized.encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def constant_time_digest_equal(left: str, right: str) -> bool:
    """Compare two digests without leaking timing information."""
    return hmac.compare_digest(left, right)
