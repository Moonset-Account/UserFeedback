"""JSONPath-like field locations used in failure records."""

from __future__ import annotations

import json
import re

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def child(parent: str, key: str) -> str:
    """Return the path of an object member below ``parent``."""

    if _IDENTIFIER.match(key):
        return f"{parent}.{key}"
    return f"{parent}[{json.dumps(key, ensure_ascii=False)}]"


def index(parent: str, position: int) -> str:
    """Return the path of a list item below ``parent``."""

    return f"{parent}[{position}]"
