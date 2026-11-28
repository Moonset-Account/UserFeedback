"""fpiso: an executable isolation-semantics model for floating-point state.

The package models a shared cooperative-computation runtime in which each
*protection domain* owns its own rounding attribute, exception mask bits and
cumulative (sticky) status, while operands and intermediate results are exact
arbitrary-precision rationals rounded once into a small configurable binary
format.

Public API
----------
``Format`` / ``RoundingMode``
    Small binary format definition and the five rounding attributes.
``Domain``
    One protection domain: format, rounding, masks, sticky flags, pending
    traps.  Supports ``add``, ``mul`` and one-rounding ``fma``.
``Runtime``
    The nesting stack.  ``enter_domain`` / ``exit_domain`` (and the
    ``domain_scope`` / ``audit_scope`` context managers) suspend and resume
    domains without sharing, merging or leaking flags.
``Flag`` / ``MaskBits``
    The overflow / underflow / inexact flags and per-domain mask bits.
"""

from .domain import Domain, DomainConfig, DomainState, OpResult, OpTrace
from .errors import (
    DomainActiveError,
    DomainLifecycleError,
    DomainOwnershipError,
    FpisoError,
    FormatError,
    IsolationError,
    NoActiveDomainError,
    OperandError,
)
from .exact import as_exact
from .flags import FLAG_ORDER, Flag, MaskBits, flags_text, ordered_flags
from .format import ROUNDING_ORDER, SMALL, Format, RoundingMode
from .rounding import Rounded, round_exact
from .runtime import ExitReport, Runtime
from .value import FPValue

__all__ = [
    "Domain",
    "DomainActiveError",
    "DomainConfig",
    "DomainLifecycleError",
    "DomainOwnershipError",
    "DomainState",
    "ExitReport",
    "FLAG_ORDER",
    "FPValue",
    "FpisoError",
    "Flag",
    "Format",
    "FormatError",
    "IsolationError",
    "MaskBits",
    "NoActiveDomainError",
    "OpResult",
    "OpTrace",
    "OperandError",
    "ROUNDING_ORDER",
    "Rounded",
    "RoundingMode",
    "Runtime",
    "SMALL",
    "as_exact",
    "flags_text",
    "ordered_flags",
    "round_exact",
]

__version__ = "1.0.0"
