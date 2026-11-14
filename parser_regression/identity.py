"""Content identity, rename-tolerant matching, and display summaries.

A sample is compared across builds by its *content*, not by its name.  That is
what lets a renamed-but-unchanged sample match its original baseline entry.
When an export carries no content and no hash (legacy exports) we fall back to
the sample name and record that weaker basis on the output row.
"""

import hashlib
import re
from typing import Optional, Tuple

SUMMARY_LIMIT = 72
_HEX64 = re.compile(r"[0-9a-f]{64}")
_WHITESPACE = re.compile(r"\s+")


class IdentityError(ValueError):
    """Raised when declared content metadata is malformed or inconsistent."""


def collapse_whitespace(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def summarize(content: Optional[str], summary: Optional[str], sample_name: str,
              limit: int = SUMMARY_LIMIT) -> str:
    """Build a deterministic one-line sample summary.

    An explicit ``summary`` wins, then collapsed content, then the sample name.
    Long values are truncated so table rows stay reviewable.
    """
    if summary is not None:
        base = collapse_whitespace(summary)
    elif content is not None:
        base = collapse_whitespace(content)
    else:
        base = ""
    if not base:
        base = collapse_whitespace(sample_name)
    if len(base) > limit:
        base = base[: limit - 3].rstrip() + "..."
    return base


def normalize_hash(raw: str) -> str:
    """Normalize a declared hash into a stable identity token.

    Supported forms: ``sha256:<64 hex>`` and a bare 64-character hex digest.
    Anything else is kept as an opaque declared token, so exports produced by
    other hash algorithms still match themselves across builds.
    """
    value = raw.strip()
    if not value:
        raise IdentityError("content_hash must not be blank")
    lowered = value.casefold()
    if lowered.startswith("sha256:"):
        digest = value.split(":", 1)[1].strip().casefold()
        if not _HEX64.fullmatch(digest):
            raise IdentityError("content_hash has a malformed sha256 digest")
        return "sha256:" + digest
    if _HEX64.fullmatch(value):
        return "sha256:" + value.casefold()
    return "declared:" + value


def resolve_identity(sample_name: str, content: Optional[str],
                     content_hash: Optional[str]) -> Tuple[str, str]:
    """Return ``(identity_token, basis)`` for one record.

    ``basis`` is one of ``hash``, ``content`` or ``name`` and is surfaced in
    the report so reviewers know how rename matching was performed.
    """
    if content_hash is not None:
        token = normalize_hash(content_hash)
        if content is not None and token.startswith("sha256:"):
            computed = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
            if token != computed:
                raise IdentityError("content_hash does not match content")
        return token, "hash"
    if content is not None:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return "sha256:" + digest, "content"
    return "name:" + sample_name.strip(), "name"


def comparison_mode(value: str) -> str:
    """Comparison key for parse modes; display keeps the original spelling."""
    return value.strip().casefold()


def comparison_environment(value: str) -> str:
    """Comparison key for environments; display keeps original spelling."""
    return value.strip().casefold()
