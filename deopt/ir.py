"""Intermediate representation with guards, inlined call sites and exception edges.

The IR is a tiny register-free, local-variable based statement machine.  Every
instruction lives in a :class:`~deopt.ir.Function` body at an integer program
counter.  A function also owns an exception table: *handler* entries describe
the exception edges that the *optimized* representation folds into a single
exit and that the fallback interpreter must restore explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def is_immutable(value: Any) -> bool:
    """Return ``True`` for values that may safely travel as a ConstSource.

    Reconstructed mutable objects never use this path, so only plain immutable
    Python scalars (and ``None``) qualify.  Tuples are deliberately excluded to
    keep the rule strict and auditable.
    """

    return value is None or isinstance(value, (bool, int, float, str))


class IRObject:
    """A mutable, name-tagged runtime object with string fields."""

    __slots__ = ("tag", "fields", "oid")

    def __init__(self, tag: str, oid: int):
        self.tag = tag
        self.oid = oid
        self.fields: dict[str, Any] = {}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"IRObject({self.tag}#{self.oid}, fields={list(self.fields)})"


class IRException(Exception):
    """Exception value raised by :class:`Raise` and propagated by the VM."""

    __slots__ = ("value",)

    def __init__(self, value: Any):
        super().__init__(f"IRException({value!r})")
        self.value = value


# --------------------------------------------------------------------------- #
# Instructions
# --------------------------------------------------------------------------- #


@dataclass
class Const:
    dst: str
    value: Any


@dataclass
class New:
    """Allocate an object.

    ``ctor_effect`` is the externally visible side effect of the *original*
    constructor.  Recovery reconstructs optimized-away objects without going
    through the constructor, therefore this effect must never be re-emitted.
    """

    dst: str
    tag: str
    ctor_effect: str


@dataclass
class GetField:
    dst: str
    obj: str
    name: str


@dataclass
class SetField:
    obj: str
    name: str
    src: str


@dataclass
class Move:
    dst: str
    src: str


@dataclass
class GuardType:
    """A normal guard.

    In generic (fallback) interpretation the guard is a real check that raises
    :class:`IRException` on mismatch.  In the optimized trace a failed guard
    triggers deoptimization with a *normal* continuation.
    """

    src: str
    expected_tag: str


@dataclass
class Call:
    """A call that may be inlined by the optimizer.

    ``fail_pc`` is the exception-edge landing point in the *caller* when the
    callee exits via an unhandled exception.
    """

    func: str
    args: list[str]
    dst: str | None
    fail_pc: int | None


@dataclass
class Return:
    src: str | None


@dataclass
class Raise:
    src: str


@dataclass
class BindExc:
    """Landing-pad instruction: bind the in-flight exception to ``dst``."""

    dst: str


@dataclass
class Jump:
    target: int


@dataclass
class EmitConst:
    """An observable side effect carrying an immutable marker."""

    marker: str


@dataclass
class Emit:
    """An observable side effect carrying a runtime value's oid (or scalar)."""

    src: str


@dataclass
class Handler:
    """Exception edge: any IRException raised inside ``[start, end)`` lands
    at ``target`` (which must be a :class:`BindExc`)."""

    start: int
    end: int
    target: int


@dataclass
class Function:
    name: str
    params: list[str]
    body: list[Any]
    handlers: list[Handler] = field(default_factory=list)

    def handler_for(self, pc: int) -> int | None:
        for h in self.handlers:
            if h.start <= pc < h.end:
                return h.target
        return None

    def label(self, pc: int) -> str:
        return f"{self.name}@{pc}"
