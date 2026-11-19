"""Constrained differential encoder.

The encoder turns an old base byte string into an approved target byte string
using two kinds of instructions:

* ``copy``  - reuse a contiguous run of bytes from the old base, provided the
  whole run lies inside a single *public interval*;
* ``literal`` - emit target bytes directly.

Only byte ranges covered by the caller supplied public intervals may be used as
copy sources.  Matches that would straddle an authorization boundary are
shortened to the boundary or replaced by literals, and identical content
existing both inside and outside the public regions can only be copied from
the public instance.
"""

from .encoder import (
    CopyOp,
    LiteralOp,
    Op,
    Plan,
    decode,
    encode,
    normalize_intervals,
    plan_cost,
)

__all__ = [
    "CopyOp",
    "LiteralOp",
    "Op",
    "Plan",
    "decode",
    "encode",
    "normalize_intervals",
    "plan_cost",
]
