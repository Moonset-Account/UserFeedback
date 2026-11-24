"""Lookup registry for exact record versions, approvals, and carry-forward links."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping, Sequence

from .hashing import fingerprint_paragraphs, fingerprint_text
from .models import Approval, CarryForward, RecordRef, VersionedRecord
from .ranges import Range, as_ranges, ranges_to_numbers


class RegistryError(ValueError):
    pass


class MaterialRegistry:
    def __init__(
        self,
        records: Iterable[VersionedRecord] | None = None,
        approvals: Iterable[Approval] | None = None,
        carry_forwards: Iterable[CarryForward] | None = None,
    ) -> None:
        self.records: dict[tuple[str, str], VersionedRecord] = {}
        self.approvals: list[Approval] = []
        self.carry_forwards: list[CarryForward] = []
        self._approvals_by_ref: dict[tuple[str, str], list[Approval]] = defaultdict(list)
        self._carry_by_target: dict[tuple[str, str], list[CarryForward]] = defaultdict(list)

        for record in records or []:
            self.add_record(record)
        for approval in approvals or []:
            self.add_approval(approval)
        for carry in carry_forwards or []:
            self.add_carry_forward(carry)

    def add_record(self, record: VersionedRecord) -> "MaterialRegistry":
        key = record.ref.key
        if key in self.records:
            raise RegistryError(f"duplicate record version: {record.ref}")
        self.records[key] = record
        return self

    def get(self, ref: RecordRef) -> VersionedRecord | None:
        return self.records.get(ref.key)

    def require(self, ref: RecordRef) -> VersionedRecord:
        record = self.get(ref)
        if record is None:
            raise RegistryError(f"record not found: {ref}")
        return record

    def versions_for(self, record_id: str) -> list[str]:
        return sorted(version for rid, version in self.records if rid == record_id)

    def add_approval(self, approval: Approval) -> "MaterialRegistry":
        self.approvals.append(approval)
        self._approvals_by_ref[approval.ref.key].append(approval)
        return self

    def approve(
        self,
        ref: RecordRef,
        approved_ranges: Sequence[Range | Sequence[int] | int] | None = None,
        *,
        withheld_ranges: Sequence[Range | Sequence[int] | int] | None = None,
        approval_id: str = "",
        reviewer: str = "",
        note: str = "",
        bind_to_current_content: bool = True,
    ) -> Approval:
        """Create an approval bound to the current text of an existing record."""

        record = self.require(ref)
        approved = as_ranges(approved_ranges)
        withheld = as_ranges(withheld_ranges)
        numbers = ranges_to_numbers([*approved, *withheld])
        reviewed_hashes = (
            {number: fingerprint_text(record.paragraphs[number]) for number in sorted(numbers)}
            if bind_to_current_content
            else None
        )
        approval = Approval(
            ref=ref,
            approved_ranges=tuple(approved),
            withheld_ranges=tuple(withheld),
            reviewed_hashes=reviewed_hashes,
            approval_id=approval_id,
            reviewer=reviewer,
            note=note,
        )
        self.add_approval(approval)
        return approval

    def approvals_for(self, ref: RecordRef) -> list[Approval]:
        return list(self._approvals_by_ref.get(ref.key, ()))

    def add_carry_forward(self, carry: CarryForward) -> "MaterialRegistry":
        self.carry_forwards.append(carry)
        self._carry_by_target[carry.target.key].append(carry)
        return self

    def carry_forwards_to(self, ref: RecordRef) -> list[CarryForward]:
        return list(self._carry_by_target.get(ref.key, ()))

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        for record in self.records.values():
            errors.extend(record.validate())
        return errors

    def fingerprint(self, ref: RecordRef, paragraph: int) -> str | None:
        record = self.get(ref)
        if record is None:
            return None
        text = record.text(paragraph)
        return None if text is None else fingerprint_text(text)

    def all_fingerprints(self, ref: RecordRef) -> Mapping[int, str] | None:
        record = self.get(ref)
        if record is None:
            return None
        return fingerprint_paragraphs(record.paragraphs)
