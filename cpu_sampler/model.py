"""Immutable data model: symbol images, symbol matches and mappings.

All validation that can be done without cross-image knowledge lives in
``__post_init__`` of the frozen dataclasses, so an object that exists is
always self-consistent:

* every address range is a half-open interval ``[lo, hi)`` with ``lo < hi``;
* inline children stay inside their parent range;
* siblings never overlap (they may be adjacent);
* function ranges of an image stay inside the image's ``size``.

Overlap is rejected instead of resolved because an ambiguous symbol table
would make profiling output silently wrong.
"""

from __future__ import annotations

from collections.abc import Iterable as ABCIterable
from dataclasses import dataclass, field
from typing import Any, Tuple

from .errors import SymbolTableError, ValidationError

__all__ = [
    "InlineFrame",
    "FunctionRange",
    "ModuleImage",
    "SymbolMatch",
    "require_int",
    "require_text",
]


def require_int(value: Any, field: str, *, minimum: int | None = None) -> int:
    """Return ``value`` if it is a plain int within ``minimum``."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(
            f"{field} must be an integer, got {type(value).__name__}"
        )
    if minimum is not None and value < minimum:
        raise ValidationError(f"{field} must be >= {minimum}, got {value}")
    return value


def require_text(value: Any, field: str) -> str:
    """Return ``value`` if it is a non-empty string."""

    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} must be a non-empty string")
    return value


def _symbol_int(value: Any, field: str, *, minimum: int | None = None) -> int:
    try:
        return require_int(value, field, minimum=minimum)
    except ValidationError as exc:  # normalise to the symbol-table sub-error
        raise SymbolTableError(str(exc)) from None


def _symbol_text(value: Any, field: str) -> str:
    try:
        return require_text(value, field)
    except ValidationError as exc:
        raise SymbolTableError(str(exc)) from None


def _freeze_nodes(value: Any, field: str, cls: type) -> Tuple[Any, ...]:
    """Coerce ``value`` (any iterable of ``cls``) into a tuple of ``cls``."""

    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, ABCIterable):
        raise SymbolTableError(f"{field} must be an iterable of {cls.__name__}")
    nodes = []
    for index, node in enumerate(value):
        if not isinstance(node, cls):
            raise SymbolTableError(
                f"{field}[{index}] must be a {cls.__name__}, "
                f"got {type(node).__name__}"
            )
        nodes.append(node)
    return tuple(nodes)


def _validate_span(name: Any, lo: Any, hi: Any, label: str) -> Tuple[str, int, int]:
    name = _symbol_text(name, f"{label} name")
    lo = _symbol_int(lo, f"{label} {name!r} lo", minimum=0)
    hi = _symbol_int(hi, f"{label} {name!r} hi", minimum=0)
    if lo >= hi:
        raise SymbolTableError(
            f"{label} {name!r} has an empty or inverted range [{lo}, {hi})"
        )
    return name, lo, hi


def _ordered_children(
    children: Tuple[Any, ...], lo: int, hi: int, owner: str
) -> Tuple[Any, ...]:
    """Sort ``children`` by ``lo`` and check nesting / overlap invariants."""

    ordered = tuple(sorted(children, key=lambda node: (node.lo, node.hi)))
    previous = None
    for node in ordered:
        if node.lo < lo or node.hi > hi:
            raise SymbolTableError(
                f"{owner}: child {node.name!r} range [{node.lo}, {node.hi}) "
                f"escapes parent range [{lo}, {hi})"
            )
        if previous is not None and node.lo < previous.hi:
            raise SymbolTableError(
                f"{owner}: children {previous.name!r} [{previous.lo}, "
                f"{previous.hi}) and {node.name!r} [{node.lo}, {node.hi}) overlap"
            )
        previous = node
    return ordered


@dataclass(frozen=True)
class InlineFrame:
    """One inlined function instance inside a physical function.

    ``[lo, hi)`` are addresses relative to the module base, exactly like the
    enclosing :class:`FunctionRange`.  ``children`` are further nested inline
    instances, so a chain of nested inlining is a chain of nested nodes.
    """

    name: str
    lo: int
    hi: int
    children: Tuple["InlineFrame", ...] = ()
    _child_starts: Tuple[int, ...] = field(
        default=(), init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        name, lo, hi = _validate_span(self.name, self.lo, self.hi, "inline frame")
        children = _freeze_nodes(self.children, "inline frame children", InlineFrame)
        children = _ordered_children(
            children, lo, hi, f"inline frame {name!r}"
        )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "lo", lo)
        object.__setattr__(self, "hi", hi)
        object.__setattr__(self, "children", children)
        object.__setattr__(self, "_child_starts", tuple(c.lo for c in children))

    def contains(self, relative_address: int) -> bool:
        return self.lo <= relative_address < self.hi

    @property
    def child_starts(self) -> Tuple[int, ...]:
        """Sorted start offsets of ``children``, for binary search."""

        return self._child_starts

    @property
    def span(self) -> int:
        return self.hi - self.lo


@dataclass(frozen=True)
class FunctionRange:
    """A physical function, addressed relative to its module base."""

    name: str
    lo: int
    hi: int
    inlines: Tuple[InlineFrame, ...] = ()
    _inline_starts: Tuple[int, ...] = field(
        default=(), init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        name, lo, hi = _validate_span(self.name, self.lo, self.hi, "function")
        inlines = _freeze_nodes(self.inlines, "function inlines", InlineFrame)
        inlines = _ordered_children(inlines, lo, hi, f"function {name!r}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "lo", lo)
        object.__setattr__(self, "hi", hi)
        object.__setattr__(self, "inlines", inlines)
        object.__setattr__(self, "_inline_starts", tuple(n.lo for n in inlines))

    @property
    def inline_starts(self) -> Tuple[int, ...]:
        return self._inline_starts

    def contains(self, relative_address: int) -> bool:
        return self.lo <= relative_address < self.hi

    @property
    def span(self) -> int:
        return self.hi - self.lo


@dataclass(frozen=True)
class ModuleImage:
    """A symbolised module build.

    ``(module, version)`` is the identity used by load/unload events and by
    the symbol table.  ``size`` is the address extent of the image; function
    ranges are relative to the module base and must fit inside it.
    """

    module: str
    version: str
    size: int
    functions: Tuple[FunctionRange, ...] = ()
    build_id: str | None = None
    _function_starts: Tuple[int, ...] = field(
        default=(), init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        module = _symbol_text(self.module, "image module")
        version = _symbol_text(self.version, "image version")
        size = _symbol_int(self.size, f"image {module}@{version} size", minimum=1)
        if self.build_id is not None:
            _symbol_text(self.build_id, f"image {module}@{version} build_id")
        functions = _freeze_nodes(
            self.functions, "image functions", FunctionRange
        )
        functions = _ordered_children(
            functions, 0, size, f"image {module}@{version}"
        )
        object.__setattr__(self, "module", module)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "functions", functions)
        object.__setattr__(
            self, "_function_starts", tuple(f.lo for f in functions)
        )

    @property
    def function_starts(self) -> Tuple[int, ...]:
        """Sorted start offsets of ``functions``, for binary search."""

        return self._function_starts

    @property
    def key(self) -> Tuple[str, str]:
        return (self.module, self.version)

    def describe(self) -> str:
        return f"{self.module}@{self.version}"


@dataclass(frozen=True)
class SymbolMatch:
    """Result of resolving one relative address inside one image."""

    module: str
    version: str
    relative_address: int
    function: str
    chain: Tuple[str, ...]
    build_id: str | None = None

    @property
    def inline_depth(self) -> int:
        return len(self.chain) - 1

    @property
    def leaf_name(self) -> str:
        return self.chain[-1]
