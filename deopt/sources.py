"""Provenance of values that are still live when a guard fails.

Each live local of every inlined frame is described by a *source* that says
where the value comes from in the optimized (inlined) execution:

* :class:`ConstSource`  -- an immutable compile-time constant;
* :class:`RegisterSource` -- a value held in a virtual/physical register of
  the optimized trace;
* :class:`ReconstructSource` -- an object the optimizer allocated virtually
  and that must be rebuilt from its surviving field values.

A source is missing on purpose when the optimizer cannot provide provenance
(see :mod:`deopt.materialize`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ir import is_immutable


class Source:
    """Base class for value provenance."""


@dataclass(frozen=True)
class ConstSource(Source):
    value: Any

    def __post_init__(self) -> None:
        if not is_immutable(self.value):
            raise TypeError(
                "ConstSource may only carry immutable scalars, got "
                f"{type(self.value).__name__}"
            )


@dataclass(frozen=True)
class RegisterSource(Source):
    """A value still available in an optimized register.

    Two references carrying the *same* register name are the same object and
    must remain aliased after recovery.
    """

    register: str


@dataclass(frozen=True)
class ReconstructSource(Source):
    """An optimized-away object to rebuild, identified by ``vobj``.

    The same virtual object id appearing in several places (different locals,
    different inlined frames, even inside object fields) denotes one object
    and therefore one allocation with shared identity.
    """

    vobj: str
    tag: str
    # Surviving field values, themselves described by sources.  Fields only
    # written by eliminated stores are supplied separately as pending stores.
    fields: dict[str, Source] = field(default_factory=dict)
