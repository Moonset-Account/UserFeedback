"""Output-side frame records produced by symbolising a raw stack sample.

A raw sample frame yields

* one :class:`ResolvedFrame` per level of the inline chain the symbol table
  reports — outermost first — so the emitted stack is a literal
  root-to-leaf expansion of the inlined call chain; or
* exactly one :class:`ResolvedFrame` with ``resolved=False`` acting as the
  placeholder for an address that could not be located.

Every emitted frame keeps

* ``frame_index`` — which raw sample frame it came from;
* ``inline_depth`` — where it sits inside that raw frame's inline chain
  (``0`` is the physical function);
* ``address`` — the absolute address exactly as sampled;
* ``lookup_address`` — the address actually looked up (they differ for
  return addresses, see :mod:`cpu_sampler.profiler`);
* the mapping identity and generation it was resolved against.

That provenance is what makes two stacks taken at the same absolute address
before and after a module swap individually traceable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

__all__ = [
    "PLACEHOLDER",
    "ROLE_CALL_SITE",
    "ROLE_INLINE",
    "ROLE_LEAF",
    "ROLE_PLACEHOLDER",
    "ResolvedFrame",
    "ResolvedStack",
]

PLACEHOLDER = "<unresolved>"

ROLE_LEAF = "leaf"
ROLE_CALL_SITE = "call-site"
ROLE_INLINE = "inline"
ROLE_PLACEHOLDER = "placeholder"


@dataclass(frozen=True)
class ResolvedFrame:
    """One output frame, symbolised or a placeholder."""

    #: Position in the input stack, counting from the root (0) to the leaf.
    frame_index: int
    #: Position inside that raw frame's inline chain, outermost is ``0``.
    inline_depth: int
    #: ``leaf`` | ``call-site`` | ``inline`` | ``placeholder``.
    role: str
    #: Absolute address exactly as it appeared in the sample.
    address: int
    #: Address handed to the symbol table (adjusted for return addresses).
    lookup_address: int
    #: ``False`` for a placeholder frame.
    resolved: bool
    #: This level's symbol; ``PLACEHOLDER`` when unresolved.
    name: str
    #: Outermost-to-innermost inline chain; empty for a placeholder.
    chain: Tuple[str, ...] = ()
    #: Module identity the frame was resolved against, if any.
    module: Optional[str] = None
    version: Optional[str] = None
    build_id: Optional[str] = None
    #: Base address of the mapping that was active when the sample was taken.
    base: Optional[int] = None
    #: ``lookup_address - base``, when a mapping covered the address.
    relative_address: Optional[int] = None
    #: Kind of placeholder, when ``resolved`` is ``False``.
    reason: Optional[str] = None
    #: Mapping generation assigned by the load event, for provenance.
    generation: Optional[int] = None

    @property
    def is_inline(self) -> bool:
        return self.resolved and self.inline_depth > 0

    @property
    def is_placeholder(self) -> bool:
        return not self.resolved

    def describe(self) -> str:
        if not self.resolved:
            location = ""
            if self.module is not None:
                location = f" in {self.module}@{self.version}"
            return f"{PLACEHOLDER} {self.address:#x}{location} ({self.reason})"
        origin = f"{self.module}@{self.version}"
        if self.build_id:
            origin += f"[{self.build_id}]"
        marker = " (inline)" if self.is_inline else ""
        return f"{self.name}{marker} @ {origin}+{self.relative_address:#x}"


@dataclass(frozen=True)
class ResolvedStack:
    """A fully symbolised sample: root-first frames plus its provenance."""

    timestamp: int
    pid: int
    frames: Tuple[ResolvedFrame, ...]

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(frame.name for frame in self.frames)

    @property
    def has_placeholders(self) -> bool:
        return any(not frame.resolved for frame in self.frames)

    @property
    def placeholder_count(self) -> int:
        return sum(1 for frame in self.frames if not frame.resolved)

    @property
    def raw_frame_count(self) -> int:
        return len({frame.frame_index for frame in self.frames})

    def describe(self) -> str:
        lines = [f"sample pid={self.pid} t={self.timestamp}"]
        for frame in self.frames:
            lines.append(f"  [{frame.frame_index}] {frame.describe()}")
        return "\n".join(lines)
