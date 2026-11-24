"""Anonymous docket-number generation."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Hashable


def _format_token(raw_hex: str) -> str:
    root = raw_hex[:32].upper()
    return "ANON-" + "-".join(root[i : i + 8] for i in range(0, 32, 8))


def anonymous_number(
    purpose: str,
    *parts: Hashable,
    secret: str | bytes | None = None,
    used: set[str] | None = None,
) -> str:
    """Create an unguessable anonymous docket number.

    With ``secret=None`` a random token is generated.  With a server-side
    secret the token is deterministic/HMAC-derived from the purpose and IDs,
    which helps tests and stable dispatch mappings without exposing real IDs.
    """

    used = used if used is not None else set()
    counter = 0
    while True:
        if secret is None:
            token = _format_token(secrets.token_hex(16))
        else:
            key = secret.encode("utf-8") if isinstance(secret, str) else secret
            suffix = f"#collision-{counter}" if counter else ""
            message = "\x1f".join((purpose, *(str(part) for part in parts), suffix))
            digest = hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()
            token = _format_token(digest)
        if token not in used:
            used.add(token)
            return token
        counter += 1
