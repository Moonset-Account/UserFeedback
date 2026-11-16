"""Runtime-only construction of synthetic credential-shaped test values.

No returned value is a live credential. Splitting canonical prefixes prevents public
Git blobs from containing token-shaped literals while preserving detector tests.
"""


def assemble(*parts: str) -> str:
    if not parts or not all(isinstance(part, str) for part in parts):
        raise ValueError("synthetic parts must be strings")
    return "".join(parts)
