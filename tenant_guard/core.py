"""Core provenance value lattice and source-location records.

The analyzer is deliberately dependency free.  Values are immutable so that
branch joins and fixed-point comparisons cannot accidentally mutate a proof
still referenced by another path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class NodeRef:
    """A stable source location used in proofs and loss reports."""

    function: str
    node_id: str
    kind: str = "node"
    label: str = ""
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "function": self.function,
            "node_id": self.node_id,
            "kind": self.kind,
            "label": self.label,
            "order": self.order,
        }


@dataclass(frozen=True)
class Loss:
    """The first point on a provenance chain where tenant identity was lost."""

    node: NodeRef
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node.node_id,
            "function": self.node.function,
            "kind": self.node.kind,
            "code": self.code,
            "message": self.message,
            "location": self.node.to_dict(),
        }


def append_event(trace: tuple[NodeRef, ...], event: NodeRef | None) -> tuple[NodeRef, ...]:
    """Append a proof event without unrolling the same loop/recursive node.

    Repeated execution of the same static node does not create a new static
    proof obligation.  Skipping an already-present event keeps traces finite
    and lets equivalent loop/recursion states compare equal.
    """

    if event is None or event in trace:
        return trace
    return trace + (event,)


class PVal:
    """Immutable abstract provenance value.

    Kinds:

    * ``bottom``: no feasible value/path
    * ``tenant``: unambiguously derived from the current tenant source
    * ``tenant_literal``: a literal tenant identity, not necessarily current
    * ``literal``: ordinary literal/scalar
    * ``tuple``: ordered key components
    * ``unknown``: unresolved/confused/erased value; carries a :class:`Loss`
    * ``alt``: disjunction produced by branches or recursive summaries
    """

    __slots__ = (
        "kind",
        "scalar",
        "items",
        "loss",
        "trace",
        "concrete",
        "_exact",
        "_semantic",
    )

    def __init__(
        self,
        kind: str,
        *,
        scalar: Any = None,
        items: Iterable["PVal"] = (),
        loss: Loss | None = None,
        trace: Iterable[NodeRef] = (),
        concrete: str | None = None,
    ) -> None:
        item_tuple = tuple(items)
        trace_tuple = tuple(trace)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "scalar", scalar)
        object.__setattr__(self, "items", item_tuple)
        object.__setattr__(self, "loss", loss)
        object.__setattr__(self, "trace", trace_tuple)
        object.__setattr__(self, "concrete", concrete)

        loss_key = None
        if loss is not None:
            loss_key = (
                loss.code,
                loss.node.function,
                loss.node.node_id,
                loss.node.order,
            )
        exact = (
            kind,
            scalar,
            tuple(item._exact for item in item_tuple),
            loss_key,
            trace_tuple,
            concrete,
        )
        if kind == "alt":
            semantic_items: Any = frozenset(item.semantic_key for item in item_tuple)
        else:
            semantic_items = tuple(item.semantic_key for item in item_tuple)
        semantic = (kind, scalar, semantic_items, loss_key, concrete)
        object.__setattr__(self, "_exact", exact)
        object.__setattr__(self, "_semantic", semantic)

    @property
    def semantic_key(self) -> tuple[Any, ...]:
        return self._semantic  # type: ignore[return-value]

    def semantic_equal(self, other: object) -> bool:
        return isinstance(other, PVal) and self._semantic == other._semantic

    def __hash__(self) -> int:
        return hash(self._exact)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PVal) and self._exact == other._exact

    def __repr__(self) -> str:
        if self.kind in {"tenant", "unknown", "bottom"}:
            extra = f" {self.loss.code}" if self.loss else ""
            return f"<PVal {self.kind}{extra}>"
        if self.kind == "alt":
            return f"<PVal alt {[item.kind for item in self.items]}>"
        if self.kind == "tuple":
            return f"<PVal tuple {list(self.items)!r}>"
        return f"<PVal {self.kind}:{self.scalar!r}>"


BOTTOM = PVal("bottom")
NONE = PVal("literal", scalar=None)


def tenant_value(concrete: str | None = None, trace: Iterable[NodeRef] = ()) -> PVal:
    return PVal("tenant", concrete=concrete, trace=trace)


def literal_value(value: Any, *, tenant: bool = False, trace: Iterable[NodeRef] = ()) -> PVal:
    if tenant:
        return PVal("tenant_literal", scalar=value, trace=trace, concrete=str(value))
    return PVal("literal", scalar=value, trace=trace)


def tuple_value(items: Iterable[PVal], trace: Iterable[NodeRef] = ()) -> PVal:
    return PVal("tuple", items=tuple(items), trace=trace)


def unknown_value(loss: Loss, trace: Iterable[NodeRef] = ()) -> PVal:
    merged_trace = tuple(trace)
    if loss.node not in merged_trace:
        merged_trace = merged_trace + (loss.node,)
    return PVal("unknown", loss=loss, trace=merged_trace)


def alt_value(values: Iterable[PVal]) -> PVal:
    flat: list[PVal] = []
    for value in values:
        if value.kind == "bottom":
            continue
        if value.kind == "alt":
            flat.extend(value.items)
        else:
            flat.append(value)

    unique: list[PVal] = []
    for value in flat:
        if value not in unique:
            unique.append(value)

    if not unique:
        return BOTTOM
    if len(unique) == 1:
        return unique[0]
    return PVal("alt", items=tuple(unique))


def join_values(left: PVal, right: PVal) -> PVal:
    return alt_value((left, right))


def with_ref(value: PVal, ref: NodeRef | None) -> PVal:
    """Return ``value`` after flowing through static node ``ref``."""

    if ref is None:
        return value
    if value.kind == "alt":
        return alt_value(tuple(with_ref(item, ref) for item in value.items))

    new_trace = append_event(value.trace, ref)
    if new_trace == value.trace:
        return value
    return PVal(
        value.kind,
        scalar=value.scalar,
        items=value.items,
        loss=value.loss,
        trace=new_trace,
        concrete=value.concrete,
    )
