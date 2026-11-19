"""Ordered token model for redistributed-source origin review.

The comparison library consumes two modules already normalized into ordered
token streams.  Each token carries an input-side binding number so that name
resolution (including declarations that live outside a candidate fragment) is
verified against the input rather than against spelling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Token kinds.
SCOPE_ENTER = "scope_enter"
SCOPE_EXIT = "scope_exit"
NAME_BIND = "name_bind"
NAME_REF = "name_ref"
LITERAL = "literal"

_KINDS = frozenset(
    {SCOPE_ENTER, SCOPE_EXIT, NAME_BIND, NAME_REF, LITERAL}
)


@dataclass(frozen=True)
class Token:
    """One annotated token in a module's ordered token stream.

    Fields:
        index: 0-based position inside the module stream.
        kind: one of the module-level kind constants.
        text: original source spelling (names, keywords, punctuation text).
        value: literal value for ``LITERAL`` tokens; ``None`` otherwise.
        binding: input binding number for local name tokens (binds and
            references).  ``None`` for non-name tokens and external names.
        scope_kind: scope flavor for enter/exit tokens (e.g. ``"function"``,
            ``"class"``, ``"block"``).
        external: ``True`` for names resolved outside the module (imports,
            builtins); such names match by spelling only.
    """

    index: int
    kind: str
    text: str = ""
    value: Any = None
    binding: int | None = None
    scope_kind: str | None = None
    external: bool = False

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"unknown token kind: {self.kind!r}")
        if self.index < 0:
            raise ValueError("token index must be non-negative")


def scope_enter(index: int, scope_kind: str, text: str = "") -> Token:
    return Token(index=index, kind=SCOPE_ENTER, text=text, scope_kind=scope_kind)


def scope_exit(index: int, scope_kind: str, text: str = "") -> Token:
    return Token(index=index, kind=SCOPE_EXIT, text=text, scope_kind=scope_kind)


def name_bind(index: int, text: str, binding: int) -> Token:
    if binding is None:
        raise ValueError("a binding token requires a binding number")
    return Token(index=index, kind=NAME_BIND, text=text, binding=binding)


def name_ref(index: int, text: str, binding: int | None, external: bool = False) -> Token:
    if not external and binding is None:
        raise ValueError(
            "a local reference requires a binding number; mark external names "
            "with external=True"
        )
    return Token(
        index=index,
        kind=NAME_REF,
        text=text,
        binding=binding,
        external=external,
    )


def literal(index: int, value: Any, text: str = "") -> Token:
    return Token(index=index, kind=LITERAL, text=text, value=value)
