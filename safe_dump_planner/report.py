"""Serializable planning results and explanations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .geometry import ByteRange


def _range_to_dict(value: ByteRange | None) -> list[int] | None:
    return None if value is None else value.to_dict()


def _ranges_to_dicts(values: tuple[ByteRange, ...] | list[ByteRange]) -> list[list[int]]:
    return [r.to_dict() for r in values]


@dataclass
class Reason:
    """A machine-readable explanation for a decision."""

    code: str
    message: str
    target_id: str | None = None
    page_id: str | None = None
    domain: str | None = None
    domains: tuple[str, ...] = ()
    range: ByteRange | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.target_id is not None:
            out["target_id"] = self.target_id
        if self.page_id is not None:
            out["page_id"] = self.page_id
        if self.domain is not None:
            out["domain"] = self.domain
        if self.domains:
            out["domains"] = list(self.domains)
        if self.range is not None:
            out["range"] = self.range.to_dict()
        if self.details:
            out["details"] = _json_safe(self.details)
        return out


def _json_safe(value: Any) -> Any:
    if isinstance(value, ByteRange):
        return value.to_dict()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return value


@dataclass
class Fragment:
    """A retained contiguous byte interval."""

    page_id: str
    output_handle: str
    start: int
    end: int
    masked_ranges: tuple[ByteRange, ...] = ()
    reasons: tuple[Reason, ...] = ()

    @property
    def length(self) -> int:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_id": self.page_id,
            "output_handle": self.output_handle,
            "start": self.start,
            "end": self.end,
            "length": self.length,
            "masked_ranges": _ranges_to_dicts(self.masked_ranges),
            "reasons": [r.to_dict() for r in self.reasons],
        }


@dataclass
class PointerDecision:
    pointer_key: str
    page_id: str
    range: ByteRange
    status: str
    retained: bool
    clear_pointer_retained: bool
    target_page: str | None = None
    downstream_atom_count: int = 0
    reason: Reason | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pointer_key": self.pointer_key,
            "page_id": self.page_id,
            "range": self.range.to_dict(),
            "status": self.status,
            "retained": self.retained,
            "clear_pointer_retained": self.clear_pointer_retained,
            "target_page": self.target_page,
            "downstream_atom_count": self.downstream_atom_count,
            "reason": None if self.reason is None else self.reason.to_dict(),
        }


@dataclass
class TargetDecision:
    target_id: str
    priority: int
    status: str
    supported: bool
    selected_as_root: bool
    supported_as_dependency: bool = False
    complete: bool = True
    redacted: bool = False
    required_bytes: int = 0
    marginal_bytes: int | None = None
    reasons: tuple[Reason, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "priority": self.priority,
            "status": self.status,
            "supported": self.supported,
            "complete": self.complete,
            "redacted": self.redacted,
            "selected_as_root": self.selected_as_root,
            "supported_as_dependency": self.supported_as_dependency,
            "required_bytes": self.required_bytes,
            "marginal_bytes": self.marginal_bytes,
            "reasons": [r.to_dict() for r in self.reasons],
        }


@dataclass
class PagePlan:
    page_id: str
    output_handle: str
    role: str
    size: int
    coherent_domains: tuple[str, ...]
    allowed_ranges: tuple[ByteRange, ...]
    denied_ranges: tuple[ByteRange, ...]
    crop_ranges: tuple[ByteRange, ...]
    address_allowed: bool
    retained_ranges: tuple[ByteRange, ...] = ()
    masked_ranges: tuple[ByteRange, ...] = ()
    domain_allowed_ranges: dict[str, tuple[ByteRange, ...]] = field(default_factory=dict)

    def to_dict(self, *, include_domain_detail: bool = True) -> dict[str, Any]:
        out = {
            "page_id": self.page_id,
            "output_handle": self.output_handle,
            "role": self.role,
            "size": self.size,
            "coherent_domains": list(self.coherent_domains),
            "allowed_ranges": _ranges_to_dicts(self.allowed_ranges),
            "denied_ranges": _ranges_to_dicts(self.denied_ranges),
            "crop_ranges": _ranges_to_dicts(self.crop_ranges),
            "address_allowed": self.address_allowed,
            "retained_ranges": _ranges_to_dicts(self.retained_ranges),
            "masked_ranges": _ranges_to_dicts(self.masked_ranges),
        }
        if include_domain_detail:
            out["domain_allowed_ranges"] = {
                d: _ranges_to_dicts(rs) for d, rs in self.domain_allowed_ranges.items()
            }
        return out


@dataclass
class DumpPlan:
    budget_bytes: int | None
    objective: str
    retained_bytes: int
    fragment_count: int
    feasible: bool
    pages: dict[str, PagePlan]
    fragments: tuple[Fragment, ...]
    targets: tuple[TargetDecision, ...]
    pointer_decisions: tuple[PointerDecision, ...] = ()
    reasons: tuple[Reason, ...] = ()
    optimization: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget_bytes": self.budget_bytes,
            "objective": self.objective,
            "retained_bytes": self.retained_bytes,
            "fragment_count": self.fragment_count,
            "feasible": self.feasible,
            "optimization": _json_safe(self.optimization),
            "pages": {pid: p.to_dict() for pid, p in sorted(self.pages.items())},
            "fragments": [f.to_dict() for f in self.fragments],
            "targets": [t.to_dict() for t in self.targets],
            "pointer_decisions": [p.to_dict() for p in self.pointer_decisions],
            "reasons": [r.to_dict() for r in self.reasons],
        }

    def explain(self) -> str:
        lines: list[str] = []
        budget = "unlimited" if self.budget_bytes is None else f"{self.budget_bytes} B"
        lines.append(
            f"safe dump plan: retained={self.retained_bytes} B, "
            f"budget={budget}, fragments={self.fragment_count}"
        )
        for target in self.targets:
            state = target.status.upper()
            lines.append(
                f"- target {target.target_id} (priority={target.priority}): {state}"
            )
            for reason in target.reasons:
                where = f" {reason.range}" if reason.range else ""
                lines.append(f"    {reason.code}{where}: {reason.message}")
        for fragment in self.fragments:
            lines.append(
                f"+ fragment {fragment.output_handle} {fragment.start}..{fragment.end} "
                f"({fragment.length} B)"
            )
        if not self.fragments:
            lines.append("+ no fragments retained")
        return "\n".join(lines)
