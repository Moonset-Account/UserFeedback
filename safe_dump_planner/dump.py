"""Materialize planned fragments and verify that no restricted address leaks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import UnsafeOutputError
from .geometry import ByteRange
from .model import DumpRequest
from .planner import plan_safe_dump
from .report import DumpPlan


@dataclass
class DumpSegment:
    segment_id: str
    output_handle: str
    role: str
    start: int
    end: int
    data: bytes
    masked_ranges: tuple[ByteRange, ...] = ()

    @property
    def length(self) -> int:
        return self.end - self.start

    def to_dict(self, *, include_data: bool = True) -> dict[str, Any]:
        out = {
            "segment_id": self.segment_id,
            "output_handle": self.output_handle,
            "role": self.role,
            "start": self.start,
            "end": self.end,
            "length": self.length,
            "masked_ranges": [[r.start, r.end] for r in self.masked_ranges],
        }
        if include_data:
            out["data_hex"] = self.data.hex()
        return out


@dataclass
class SafeDumpArtifact:
    plan: DumpPlan
    segments: tuple[DumpSegment, ...]
    manifest: dict[str, Any] = field(default_factory=dict)

    def verify_no_restricted_addresses(self, request: DumpRequest) -> list[str]:
        """Return leak descriptions; an empty list means verification passed."""

        addresses = _restricted_addresses(request, self.plan)
        leaks: list[str] = []
        for segment in self.segments:
            for address in addresses:
                for pattern in _address_patterns(address, request.scan_text_addresses):
                    start = segment.data.find(pattern)
                    while start != -1:
                        leaks.append(
                            f"{segment.segment_id} contains an encoded restricted "
                            f"address in {len(pattern)} byte form at segment offset {start}"
                        )
                        start = segment.data.find(pattern, start + 1)
        return leaks

    def to_dict(self, *, include_plan: bool = False) -> dict[str, Any]:
        """Serialize the artifact.

        The default is public-output safe: it includes the opaque manifest and
        redacted segment bytes, but omits the control-plane plan containing
        logical page IDs.  Pass ``include_plan=True`` for an authorized local
        diagnostic response.
        """

        out = {
            "manifest": self.manifest,
            "segments": [s.to_dict() for s in self.segments],
        }
        if include_plan:
            out["plan"] = self.plan.to_dict()
        return out

    def control_plane_dict(self) -> dict[str, Any]:
        return self.to_dict(include_plan=True)


def materialize_dump(
    request: DumpRequest | dict[str, Any],
    plan: DumpPlan | None = None,
) -> SafeDumpArtifact:
    """Copy planned fragments and apply zeroization masks.

    The returned manifest deliberately uses opaque page handles and never places
    physical/virtual address integers in JSON.
    """

    if isinstance(request, dict):
        request = DumpRequest.from_dict(request)
    if plan is None:
        plan = plan_safe_dump(request)

    pages = {p.page_id: p for p in request.pages}
    segments: list[DumpSegment] = []
    manifest_segments: list[dict[str, Any]] = []

    for index, fragment in enumerate(plan.fragments):
        page = pages[fragment.page_id]
        raw = bytearray(page.padded_data[fragment.start : fragment.end])
        for mask_range in fragment.masked_ranges:
            local_start = max(mask_range.start, fragment.start) - fragment.start
            local_end = min(mask_range.end, fragment.end) - fragment.start
            for offset in range(local_start, local_end):
                raw[offset] = request.redaction_byte
        segment = DumpSegment(
            segment_id=f"seg-{index + 1:04d}",
            output_handle=fragment.output_handle,
            role=page.role,
            start=fragment.start,
            end=fragment.end,
            data=bytes(raw),
            masked_ranges=fragment.masked_ranges,
        )
        segments.append(segment)
        manifest_segments.append(segment.to_dict(include_data=False))

    artifact = SafeDumpArtifact(
        plan=plan,
        segments=tuple(segments),
        manifest={
            "schema": "safe-dump-manifest/v1",
            "budget_bytes": plan.budget_bytes,
            "retained_bytes": plan.retained_bytes,
            "fragment_count": plan.fragment_count,
            "redaction_byte": request.redaction_byte,
            "segments": manifest_segments,
            "pages": [
                {
                    "output_handle": pp.output_handle,
                    "role": pp.role,
                    "size": pp.size,
                    "address_allowed": pp.address_allowed,
                    "retained_ranges": [r.to_dict() for r in pp.retained_ranges],
                    "masked_ranges": [r.to_dict() for r in pp.masked_ranges],
                }
                for pp in sorted(plan.pages.values(), key=lambda p: p.output_handle)
                if pp.retained_ranges
            ],
        },
    )
    leaks = artifact.verify_no_restricted_addresses(request)
    if leaks:
        raise UnsafeOutputError(
            "refusing to materialize unsafe dump: " + "; ".join(leaks)
        )
    return artifact


def _restricted_addresses(request: DumpRequest, plan: DumpPlan) -> set[int]:
    addresses: set[int] = set()
    for page in request.pages:
        page_plan = plan.pages.get(page.page_id)
        if page_plan is not None and page_plan.address_allowed:
            continue
        if page.physical_address not in (None, 0):
            addresses.add(int(page.physical_address))
    for alias in request.aliases:
        page_plan = plan.pages.get(alias.page_id)
        if page_plan is not None and page_plan.address_allowed:
            continue
        if alias.virtual_address not in (None, 0):
            addresses.add(int(alias.virtual_address))
    for pointer in request.pointers:
        target_restricted = True
        if pointer.target_page is not None:
            page_plan = plan.pages.get(pointer.target_page)
            target_restricted = not (page_plan is not None and page_plan.address_allowed)
        elif pointer.target_alias_id is not None:
            alias = next(
                (a for a in request.aliases if a.alias_id == pointer.target_alias_id),
                None,
            )
            if alias is not None:
                page_plan = plan.pages.get(alias.page_id)
                target_restricted = not (
                    page_plan is not None and page_plan.address_allowed
                )
        else:
            # An unresolved value is not known to identify a restricted page.
            target_restricted = False
        if target_restricted:
            if pointer.value not in (None, 0):
                addresses.add(int(pointer.value))
            if pointer.target_virtual_address not in (None, 0):
                addresses.add(int(pointer.target_virtual_address))
    return addresses


def _address_patterns(address: int, scan_text: bool) -> list[bytes]:
    patterns: list[bytes] = []
    for width in (8, 4):
        if address < (1 << (8 * width)):
            patterns.append(address.to_bytes(width, "little"))
            patterns.append(address.to_bytes(width, "big"))
    if scan_text:
        patterns.append(hex(address).encode("ascii"))
    return patterns
