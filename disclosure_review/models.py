"""Immutable-ish domain records for disclosure tracing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .ranges import Range


@dataclass(frozen=True)
class RecordRef:
    """An exact versioned material or submission record."""

    record_id: str
    version: str

    def __post_init__(self) -> None:
        if not self.record_id:
            raise ValueError("record_id must not be empty")
        # Accept integers in callers while keeping one canonical string form.
        if not isinstance(self.version, str):
            object.__setattr__(self, "version", str(self.version))
        if not self.version:
            raise ValueError("version must not be empty")

    @classmethod
    def parse(cls, value: "RecordRef | Mapping[str, object] | Sequence[object]") -> "RecordRef":
        if isinstance(value, RecordRef):
            return value
        if isinstance(value, Mapping):
            return cls(str(value["record_id"]), str(value.get("version", "")))
        if isinstance(value, Sequence) and len(value) == 2:
            return cls(str(value[0]), str(value[1]))
        raise ValueError(f"cannot parse record reference: {value!r}")

    @property
    def key(self) -> tuple[str, str]:
        return self.record_id, self.version

    def to_dict(self) -> dict[str, str]:
        return {"record_id": self.record_id, "version": self.version}

    def __str__(self) -> str:
        return f"{self.record_id}@{self.version}"


@dataclass(frozen=True, order=True)
class NodeRef:
    """One paragraph in one exact record version."""

    record_id: str
    version: str
    paragraph: int

    def __post_init__(self) -> None:
        if self.paragraph < 1:
            raise ValueError("paragraph numbers are 1-based")
        if not isinstance(self.version, str):
            object.__setattr__(self, "version", str(self.version))

    @property
    def record(self) -> RecordRef:
        return RecordRef(self.record_id, self.version)

    @classmethod
    def from_record(cls, ref: RecordRef, paragraph: int) -> "NodeRef":
        return cls(ref.record_id, ref.version, paragraph)

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "version": self.version,
            "paragraph": self.paragraph,
        }

    def __str__(self) -> str:
        return f"{self.record_id}@{self.version}#p{self.paragraph}"


@dataclass(frozen=True)
class Citation:
    """A citation/excerpt relationship between two paragraph ranges.

    ``holder_range`` locates the excerpt in the record that contains the
    citation.  ``source`` and ``source_range`` identify the exact material
    version and paragraphs actually used.

    When ranges have equal lengths, paragraphs are paired one-to-one.  For
    unequal ranges, callers may provide ``paragraph_map``; otherwise the
    conservative fallback makes every holder paragraph depend on every source
    paragraph in the cited range.
    """

    citation_id: str
    holder_range: Range
    source: RecordRef
    source_range: Range
    paragraph_map: Mapping[int, tuple[int, ...]] | None = None
    verbatim: bool = False
    note: str = ""

    def targets_for(self, holder_paragraph: int) -> tuple[int, ...]:
        if not self.holder_range.contains_number(holder_paragraph):
            return ()
        if self.paragraph_map is not None:
            mapped = self.paragraph_map.get(holder_paragraph)
            if mapped is None:
                raise ValueError(
                    f"citation {self.citation_id!r} has no mapping for holder "
                    f"paragraph {holder_paragraph}"
                )
            for source_number in mapped:
                if not self.source_range.contains_number(source_number):
                    raise ValueError(
                        f"citation {self.citation_id!r} maps holder paragraph "
                        f"{holder_paragraph} to source paragraph {source_number}, "
                        f"outside {self.source_range}"
                    )
            return tuple(mapped)
        if self.holder_range.length == self.source_range.length:
            offset = holder_paragraph - self.holder_range.start
            return (self.source_range.start + offset,)
        # Unequal ranges without an explicit map: preserve the whole-range
        # dependency rather than inventing a potentially wrong alignment.
        return tuple(self.source_range.numbers())

    def validate_map(self) -> list[str]:
        errors: list[str] = []
        if self.paragraph_map is None:
            return errors
        for holder_number, source_numbers in self.paragraph_map.items():
            if not self.holder_range.contains_number(holder_number):
                errors.append(
                    f"citation {self.citation_id}: mapped holder paragraph "
                    f"{holder_number} is outside {self.holder_range}"
                )
            for source_number in source_numbers:
                if not self.source_range.contains_number(source_number):
                    errors.append(
                        f"citation {self.citation_id}: mapped source paragraph "
                        f"{source_number} is outside {self.source_range}"
                    )
        return errors


def _coerce_paragraphs(
    paragraphs: Sequence[str] | Mapping[int | str, str],
) -> dict[int, str]:
    if isinstance(paragraphs, Mapping):
        result: dict[int, str] = {}
        for key, value in paragraphs.items():
            number = int(key)
            if number < 1:
                raise ValueError("paragraph numbers are 1-based")
            result[number] = str(value)
        return dict(sorted(result.items()))
    return {number: str(text) for number, text in enumerate(paragraphs, start=1)}


@dataclass
class VersionedRecord:
    """A submission, interview version, or other source-material version."""

    ref: RecordRef
    paragraphs: dict[int, str]
    citations: list[Citation] = field(default_factory=list)
    kind: str = "record"
    title: str = ""

    def __post_init__(self) -> None:
        # The dataclass type is intentionally broad for ergonomic construction.
        if not isinstance(self.ref, RecordRef):
            self.ref = RecordRef.parse(self.ref)  # type: ignore[arg-type]
        if not isinstance(self.paragraphs, dict):
            self.paragraphs = _coerce_paragraphs(self.paragraphs)  # type: ignore[arg-type]
        else:
            self.paragraphs = {int(k): str(v) for k, v in self.paragraphs.items()}

    @classmethod
    def create(
        cls,
        record_id: str,
        version: str | int,
        paragraphs: Sequence[str] | Mapping[int | str, str],
        citations: Sequence[Citation] | None = None,
        *,
        kind: str = "record",
        title: str = "",
    ) -> "VersionedRecord":
        return cls(
            ref=RecordRef(record_id, str(version)),
            paragraphs=_coerce_paragraphs(paragraphs),
            citations=list(citations or []),
            kind=kind,
            title=title,
        )

    @property
    def paragraph_count(self) -> int:
        return max(self.paragraphs, default=0)

    def text(self, number: int) -> str | None:
        return self.paragraphs.get(number)

    def citation_for_paragraph(self, number: int) -> list[Citation]:
        return [citation for citation in self.citations if citation.holder_range.contains_number(number)]

    def validate(self) -> list[str]:
        errors: list[str] = []
        numbers = set(self.paragraphs)
        if numbers and (min(numbers) != 1 or numbers != set(range(1, max(numbers) + 1))):
            errors.append(f"{self.ref}: paragraphs must be contiguous starting at 1")
        for citation in self.citations:
            if citation.holder_range.end > self.paragraph_count:
                errors.append(
                    f"{self.ref}: citation {citation.citation_id} holder range "
                    f"{citation.holder_range} exceeds paragraph count {self.paragraph_count}"
                )
            errors.extend(citation.validate_map())
        return errors


# A semantic alias makes call sites read clearly while retaining one model.
Submission = VersionedRecord


@dataclass(frozen=True)
class Approval:
    """Approved public scope for one exact material version.

    ``reviewed_hashes`` binds range approval to the exact reviewed paragraph
    contents.  A changed paragraph is not covered even if its number remains in
    an approved range.
    """

    ref: RecordRef
    approved_ranges: tuple[Range, ...] = ()
    withheld_ranges: tuple[Range, ...] = ()
    reviewed_hashes: Mapping[int, str] | None = None
    approval_id: str = ""
    reviewer: str = ""
    note: str = ""

    def approved_numbers(self) -> set[int]:
        from .ranges import ranges_to_numbers

        return ranges_to_numbers(self.approved_ranges)

    def withheld_numbers(self) -> set[int]:
        from .ranges import ranges_to_numbers

        return ranges_to_numbers(self.withheld_ranges)


@dataclass(frozen=True)
class CarryForward:
    """Explicit verbatim carry-forward links in a revision.

    ``paragraph_map[new_paragraph] = old_paragraph``.  Every link is verified
    against exact content fingerprints before prior approval can travel with
    the paragraph.
    """

    target: RecordRef
    source: RecordRef
    paragraph_map: Mapping[int, int]
    declaration_id: str = ""
    note: str = ""

    @classmethod
    def equal_range(
        cls,
        target: RecordRef,
        source: RecordRef,
        target_range: Range | Sequence[int] | int,
        source_range: Range | Sequence[int] | int,
        *,
        declaration_id: str = "",
    ) -> "CarryForward":
        tr = Range.from_value(target_range)
        sr = Range.from_value(source_range)
        if tr.length != sr.length:
            raise ValueError("equal-range carry-forward requires equal lengths")
        mapping = {
            target_number: source_number
            for target_number, source_number in zip(tr.numbers(), sr.numbers())
        }
        return cls(target=target, source=source, paragraph_map=mapping, declaration_id=declaration_id)


@dataclass(frozen=True)
class ReviewPolicy:
    """Tunable judgment policy with conservative defaults."""

    # Uncited paragraphs are ordinarily the investigator's own connecting text.
    uncited_paragraph_status: str = "disclose"  # or "withhold"/"confirm"
    # Revisions normally need an explicit carry-forward declaration. Tests or
    # legacy workflows may disable this to allow automatic same-number matching.
    require_carry_declaration_for_revision: bool = True
    # A declared verbatim citation is checked character-for-character.
    verify_verbatim_citations: bool = True

    def __post_init__(self) -> None:
        if self.uncited_paragraph_status not in {"disclose", "withhold", "confirm"}:
            raise ValueError("invalid uncited_paragraph_status")


@dataclass
class ReviewCase:
    """All inputs needed for one disclosure adjudication."""

    submission: VersionedRecord
    records: list[VersionedRecord] = field(default_factory=list)
    approvals: list[Approval] = field(default_factory=list)
    carry_forwards: list[CarryForward] = field(default_factory=list)
    policy: ReviewPolicy = field(default_factory=ReviewPolicy)
    # When provided, the submission is treated as a revision of this ref.
    baseline_submission: RecordRef | None = None

    def __post_init__(self) -> None:
        if self.baseline_submission is not None and not isinstance(
            self.baseline_submission, RecordRef
        ):
            self.baseline_submission = RecordRef.parse(self.baseline_submission)  # type: ignore[arg-type]
