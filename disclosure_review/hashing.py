"""Content fingerprints used to bind approval to exact reviewed wording."""

from __future__ import annotations

import hashlib
from typing import Mapping


def fingerprint_text(text: str) -> str:
    """Return the stable fingerprint for one reviewed paragraph.

    The ``sha256:`` prefix makes the algorithm explicit in serialized reports.
    Text is intentionally not stripped or normalized: a whitespace or wording
    change is a content change and therefore requires reconfirmation unless a
    valid verbatim carry-forward link proves otherwise.
    """

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def fingerprint_paragraphs(paragraphs: Mapping[int, str]) -> dict[int, str]:
    return {number: fingerprint_text(text) for number, text in paragraphs.items()}
