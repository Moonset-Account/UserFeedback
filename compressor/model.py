"""Core data model for the register-update message compression library.

The domain:

* At most four bounded integer registers.
* A finite set of declared integer constants.
* A finite set of declared operations.  Every candidate message may only
  reference declared operations (and therefore declared constants).
* Messages are either updates (:class:`Update`), observation barriers
  (:class:`Barrier`) or in-situ business receipts (:class:`Receipt`, or a
  receipt attached to another message).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple, Union

ASSIGN = "assign"
SWAP = "swap"
SADD = "sadd"
COND = "cond"
VALID_KINDS = frozenset((ASSIGN, SWAP, SADD, COND))


class InvalidDeclaration(ValueError):
    """Raised when a declaration is structurally invalid."""


class InvalidCandidate(ValueError):
    """Raised when a candidate message stream violates the declaration."""


@dataclass(frozen=True)
class Operation:
    """One declared operation.

    Kind semantics (LO/HI are the target register bounds):

    * ``assign``: ``r[target] = constant``
    * ``swap``:   exchange ``r[target]`` and ``r[source]``
    * ``sadd``:   ``r[target] = clamp(r[target] + constant, LO, HI)``
    * ``cond``:   if ``low <= r[source] <= high`` then apply ``action``
      (``assign`` with ``constant`` or ``sadd`` with ``constant``) to
      ``r[target]``; otherwise leave the state untouched.
    """

    kind: str
    target: int
    source: Optional[int] = None
    constant: Optional[int] = None
    low: Optional[int] = None
    high: Optional[int] = None
    action: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in VALID_KINDS:
            raise InvalidDeclaration(f"unknown operation kind {self.kind!r}")
        if self.kind == COND:
            if self.action not in (ASSIGN, SADD):
                raise InvalidDeclaration(
                    "conditional update action must be 'assign' or 'sadd'"
                )


@dataclass(frozen=True)
class Update:
    """An update message referencing a declared operation by index."""

    operation: int
    receipt: Optional[int] = None


@dataclass(frozen=True)
class Barrier:
    """An observation barrier publishing the named registers in index order."""

    registers: Tuple[int, ...]
    receipt: Optional[int] = None


@dataclass(frozen=True)
class Receipt:
    """A standalone in-situ business receipt marker."""

    receipt_id: int


Message = Union[Update, Barrier, Receipt]


@dataclass(frozen=True)
class Declaration:
    """Static context: register bounds, declared constants and operations."""

    registers: Tuple[Tuple[int, int], ...]
    constants: Tuple[int, ...]
    operations: Tuple[Operation, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.registers) <= 4:
            raise InvalidDeclaration("between 1 and 4 registers are required")
        for idx, (lo, hi) in enumerate(self.registers):
            if not (isinstance(lo, int) and isinstance(hi, int)):
                raise InvalidDeclaration("register bounds must be integers")
            if lo > hi:
                raise InvalidDeclaration(f"register {idx} has lo > hi")
        if len(set(self.constants)) != len(self.constants):
            raise InvalidDeclaration("declared constants must be distinct")
        if not self.operations:
            raise InvalidDeclaration("at least one operation must be declared")
        if len(self.operations) > 65535:
            raise InvalidDeclaration("too many declared operations")
        nregs = len(self.registers)
        const_set = frozenset(self.constants)
        for op in self.operations:
            if not 0 <= op.target < nregs:
                raise InvalidDeclaration("operation target register out of range")
            if op.kind == SWAP:
                if not isinstance(op.source, int) or not 0 <= op.source < nregs:
                    raise InvalidDeclaration("swap source register out of range")
                if op.source == op.target:
                    raise InvalidDeclaration("swap requires distinct registers")
            if op.kind in (ASSIGN, SADD):
                if not isinstance(op.constant, int) or op.constant not in const_set:
                    raise InvalidDeclaration(
                        f"{op.kind} constant must be one of the declared constants"
                    )
            if op.kind == COND:
                if not isinstance(op.source, int) or not 0 <= op.source < nregs:
                    raise InvalidDeclaration("cond source register out of range")
                for name, value in (
                    ("low", op.low),
                    ("high", op.high),
                    ("constant", op.constant),
                ):
                    if not isinstance(value, int) or value not in const_set:
                        raise InvalidDeclaration(
                            f"cond {name} must be a declared constant"
                        )
                if op.low > op.high:
                    raise InvalidDeclaration("cond interval is empty (low > high)")

    @property
    def nregs(self) -> int:
        return len(self.registers)

    def bounds(self, register: int) -> Tuple[int, int]:
        return self.registers[register]


@dataclass(frozen=True)
class Stream:
    """A sequence of messages under a declaration."""

    declaration: Declaration
    messages: Tuple[Message, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        nregs = self.declaration.nregs
        for msg in self.messages:
            if isinstance(msg, Update):
                if not 0 <= msg.operation < len(self.declaration.operations):
                    raise InvalidCandidate(
                        f"update references undeclared operation {msg.operation}"
                    )
                if msg.receipt is not None and not isinstance(msg.receipt, int):
                    raise InvalidCandidate("receipt ids must be integers")
            elif isinstance(msg, Barrier):
                if not msg.registers:
                    raise InvalidCandidate("barrier must observe at least one register")
                if len(set(msg.registers)) != len(msg.registers):
                    raise InvalidCandidate("barrier registers must be distinct")
                for r in msg.registers:
                    if not 0 <= r < nregs:
                        raise InvalidCandidate("barrier register out of range")
                if msg.receipt is not None and not isinstance(msg.receipt, int):
                    raise InvalidCandidate("receipt ids must be integers")
            elif isinstance(msg, Receipt):
                if not isinstance(msg.receipt_id, int):
                    raise InvalidCandidate("receipt ids must be integers")
            else:
                raise InvalidCandidate(f"unknown message type {type(msg).__name__}")


def anchor_descriptor(message: Message) -> Optional[Tuple]:
    """Return the structural anchor descriptor of a message.

    Anchors are the externally visible events whose order must be preserved:
    barriers (with their observed register set and optional receipt) and
    receipts.  Receipt-free updates are not anchors; a receipt attached to an
    update is represented as the same descriptor as a standalone receipt.
    """
    if isinstance(message, Barrier):
        return ("barrier", message.registers, message.receipt)
    if isinstance(message, Receipt):
        return ("receipt", message.receipt_id)
    if isinstance(message, Update) and message.receipt is not None:
        return ("receipt", message.receipt)
    return None


def anchor_sequence(messages: Sequence[Message]) -> Tuple[Tuple, ...]:
    return tuple(a for a in (anchor_descriptor(m) for m in messages) if a is not None)
