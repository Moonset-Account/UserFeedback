"""Dictionary/JSON facade for synthetic cases and automation."""

from __future__ import annotations

from typing import Any, Mapping

from .engine import review_case
from .hashing import fingerprint_text
from .models import (
    Approval,
    CarryForward,
    Citation,
    RecordRef,
    ReviewCase,
    ReviewPolicy,
    VersionedRecord,
)
from .registry import MaterialRegistry
from .reporting import ReviewReport
from .ranges import Range, ranges_to_numbers


def parse_ref(value: RecordRef | Mapping[str, Any] | str | list[Any] | tuple[Any, Any]) -> RecordRef:
    if isinstance(value, RecordRef):
        return value
    if isinstance(value, str):
        if "@" not in value:
            raise ValueError(f"record reference must look like id@version: {value!r}")
        record_id, version = value.rsplit("@", 1)
        return RecordRef(record_id, version)
    if isinstance(value, Mapping):
        record_id = value.get("record_id", value.get("id"))
        version = value.get("version")
        if record_id is None or version is None:
            raise ValueError(f"invalid record reference: {value!r}")
        return RecordRef(str(record_id), str(version))
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return RecordRef(str(value[0]), str(value[1]))
    raise ValueError(f"cannot parse record reference: {value!r}")


def parse_range(value: Any) -> Range:
    if isinstance(value, Range):
        return value
    if isinstance(value, Mapping):
        if "start" in value and "end" in value:
            return Range(int(value["start"]), int(value["end"]))
        if "start" in value and "length" in value:
            start = int(value["start"])
            return Range(start, start + int(value["length"]) - 1)
    if isinstance(value, int):
        return Range.point(value)
    if isinstance(value, (list, tuple)):
        if len(value) == 2:
            return Range(int(value[0]), int(value[1]))
        if len(value) == 1:
            return Range.point(int(value[0]))
    raise ValueError(f"cannot parse paragraph range: {value!r}")


def parse_ranges(values: Any) -> list[Range]:
    """Parse a list of range specs.

    Every outer item is one range: an integer is a point range and a
    ``[start, end]`` pair is an inclusive range.  Therefore ``[1, 3]`` means
    paragraphs 1 and 3, while ``[[1, 3]]`` means the single range 1-3.
    """

    if values is None or values == []:
        return []
    if isinstance(values, (Range, Mapping, int)):
        return [parse_range(values)]
    if isinstance(values, (list, tuple)):
        return [parse_range(item) for item in values]
    raise ValueError(f"cannot parse ranges: {values!r}")


def parse_citation(data: Mapping[str, Any]) -> Citation:
    holder_range = parse_range(
        data.get("holder_range", data.get("excerpt_range", data.get("range")))
    )
    source = parse_ref(data.get("source", data.get("source_ref")))
    source_range = parse_range(data.get("source_range", data.get("cited_range")))
    raw_map = data.get("paragraph_map")
    paragraph_map: dict[int, tuple[int, ...]] | None = None
    if raw_map is not None:
        paragraph_map = {}
        for key, value in raw_map.items():
            holder_number = int(key)
            if isinstance(value, (list, tuple)):
                paragraph_map[holder_number] = tuple(int(item) for item in value)
            else:
                paragraph_map[holder_number] = (int(value),)
    return Citation(
        citation_id=str(data.get("citation_id", data.get("id", "citation"))),
        holder_range=holder_range,
        source=source,
        source_range=source_range,
        paragraph_map=paragraph_map,
        verbatim=bool(data.get("verbatim", False)),
        note=str(data.get("note", "")),
    )


def parse_record(data: Mapping[str, Any], *, default_kind: str = "record") -> VersionedRecord:
    ref_data = data.get("ref", data)
    if isinstance(ref_data, RecordRef):
        ref = ref_data
    elif "record_id" in data or "id" in data:
        ref = parse_ref(data)
    else:
        ref = parse_ref(ref_data)
    paragraphs = data.get("paragraphs", data.get("texts", {}))
    citations = [parse_citation(item) for item in data.get("citations", [])]
    return VersionedRecord(
        ref=ref,
        paragraphs=paragraphs,
        citations=citations,
        kind=str(data.get("kind", default_kind)),
        title=str(data.get("title", "")),
    )


def parse_carry(data: Mapping[str, Any]) -> CarryForward:
    raw_map = data.get("paragraph_map", data.get("mapping", {}))
    paragraph_map = {int(key): int(value) for key, value in raw_map.items()}
    target = parse_ref(data.get("target", data.get("target_ref")))
    source = parse_ref(data.get("source", data.get("source_ref")))
    return CarryForward(
        target=target,
        source=source,
        paragraph_map=paragraph_map,
        declaration_id=str(data.get("declaration_id", data.get("id", ""))),
        note=str(data.get("note", "")),
    )


def _parse_approval(data: Mapping[str, Any], registry: MaterialRegistry) -> Approval:
    ref = parse_ref(data.get("ref", data))
    approved = parse_ranges(data.get("approved_ranges", data.get("approved", [])))
    withheld = parse_ranges(data.get("withheld_ranges", data.get("withheld", [])))

    reviewed_hashes = data.get("reviewed_hashes", None)
    if reviewed_hashes == "auto" or (
        reviewed_hashes is None and bool(data.get("content_bound", True))
    ):
        record = registry.get(ref)
        if record is not None:
            numbers = ranges_to_numbers([*approved, *withheld])
            reviewed_hashes = {
                number: fingerprint_text(record.paragraphs[number])
                for number in sorted(numbers)
                if number in record.paragraphs
            }
        else:
            reviewed_hashes = None
    elif isinstance(reviewed_hashes, Mapping):
        reviewed_hashes = {int(key): str(value) for key, value in reviewed_hashes.items()}

    return Approval(
        ref=ref,
        approved_ranges=tuple(approved),
        withheld_ranges=tuple(withheld),
        reviewed_hashes=reviewed_hashes,
        approval_id=str(data.get("approval_id", data.get("id", ""))),
        reviewer=str(data.get("reviewer", "")),
        note=str(data.get("note", "")),
    )


def case_from_dict(payload: Mapping[str, Any]) -> ReviewCase:
    submission = parse_record(payload["submission"], default_kind="submission")
    records = [parse_record(item) for item in payload.get("records", [])]

    registry = MaterialRegistry(records=[submission, *records])
    approvals = [_parse_approval(item, registry) for item in payload.get("approvals", [])]
    carry_forwards = [parse_carry(item) for item in payload.get("carry_forwards", [])]

    policy_data = payload.get("policy", {})
    policy = ReviewPolicy(
        uncited_paragraph_status=str(policy_data.get("uncited_paragraph_status", "disclose")),
        require_carry_declaration_for_revision=bool(
            policy_data.get("require_carry_declaration_for_revision", True)
        ),
        verify_verbatim_citations=bool(policy_data.get("verify_verbatim_citations", True)),
    )
    baseline = payload.get("baseline_submission")
    return ReviewCase(
        submission=submission,
        records=records,
        approvals=approvals,
        carry_forwards=carry_forwards,
        policy=policy,
        baseline_submission=parse_ref(baseline) if baseline is not None else None,
    )


def review_payload(payload: Mapping[str, Any]) -> ReviewReport:
    return review_case(case_from_dict(payload))


def review_dict(payload: Mapping[str, Any], *, include_trace: bool = True) -> dict[str, Any]:
    return review_payload(payload).to_dict(include_trace=include_trace)
