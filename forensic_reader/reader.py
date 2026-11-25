"""Evidence-driven forensic chip-dump reader."""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from typing import Any

from .errors import (
    ChipValidationError,
    EvidenceFormatError,
    ForensicReaderError,
    LogicalRangeError,
)
from .evidence import Evidence, KnownByte, PageChecksum, coerce_evidence
from .history import HistoryEntry, Rejection
from .report import (
    AMBIGUOUS,
    CONFIRMED,
    NO_WIRING,
    ByteState,
    ConfirmedRange,
    Interpretation,
    LostRange,
    ReadReport,
    ReportDiff,
    Segment,
    TraceReport,
    VersionJudgment,
)
from .reversal import transform_chip
from .wiring import ChipId, Wiring, normalize_wirings


def _normalize_chips(chips: Any) -> tuple[tuple[ChipId, ...], dict[ChipId, bytes]]:
    if isinstance(chips, (str, bytes, bytearray)):
        raise ChipValidationError(
            "chips must be a mapping or sequence of byte strings, not a string/bytes"
        )
    items: list[tuple[ChipId, Any]]
    if isinstance(chips, Mapping):
        items = list(chips.items())
    elif isinstance(chips, Sequence):
        looks_like_pairs = (
            len(chips) > 0
            and all(isinstance(item, tuple | list) and len(item) == 2 for item in chips)
        )
        if looks_like_pairs:
            items = [(item[0], item[1]) for item in chips]
        else:
            items = [(index, item) for index, item in enumerate(chips)]
    else:
        raise TypeError("chips must be a mapping or sequence of chip dumps")

    if not items:
        raise ChipValidationError("at least one chip dump is required")

    normalized: dict[ChipId, bytes] = {}
    for chip_id, data in items:
        if not isinstance(chip_id, Hashable):
            raise ChipValidationError(f"chip id must be hashable: {chip_id!r}")
        if isinstance(data, str):
            raise ChipValidationError(f"chip {chip_id!r} data must be bytes-like, not str")
        try:
            normalized[chip_id] = bytes(data)
        except TypeError as exc:
            raise ChipValidationError(f"chip {chip_id!r} data must be bytes-like") from exc

    lengths = {len(data) for data in normalized.values()}
    if len(lengths) != 1:
        raise ChipValidationError(
            f"all chip dumps must have equal length; found lengths {sorted(lengths)}"
        )
    return tuple(normalized.keys()), normalized


def compare_reports(old: ReadReport, new: ReadReport) -> ReportDiff:
    """Compare two reports for the same interval across evidence versions."""

    same_interval = old.start == new.start and old.end == new.end
    old_survivors = set(old.survivor_ids)
    new_survivors = set(new.survivor_ids)
    removed = tuple(wid for wid in old.survivor_ids if wid not in new_survivors)

    newly_confirmed: list[ConfirmedRange] = []
    lost: list[LostRange] = []
    if same_interval:
        address = old.start
        while address < old.end:
            old_state = old.byte_at(address)
            new_state = new.byte_at(address)
            if old_state.status != CONFIRMED and new_state.status == CONFIRMED:
                start = address
                values: list[int] = []
                while address < old.end:
                    current = new.byte_at(address)
                    if current.status != CONFIRMED:
                        break
                    values.append(current.value if current.value is not None else 0)
                    address += 1
                newly_confirmed.append(
                    ConfirmedRange(
                        start=start,
                        end=address,
                        data=bytes(values),
                        from_version=old.version,
                        to_version=new.version,
                    )
                )
                continue
            if old_state.status != NO_WIRING and new_state.status == NO_WIRING:
                start = address
                while address < old.end and new.byte_at(address).status == NO_WIRING:
                    address += 1
                lost.append(
                    LostRange(
                        start=start,
                        end=address,
                        from_version=old.version,
                        to_version=new.version,
                    )
                )
                continue
            address += 1

    return ReportDiff(
        old_version=old.version,
        new_version=new.version,
        same_interval=same_interval,
        removed_wiring_ids=removed,
        newly_confirmed=tuple(newly_confirmed),
        lost_ranges=tuple(lost),
        old_state=old.state,
        new_state=new.state,
    )


class ForensicReader:
    """Reconstruct candidate logical images and derive conservative byte judgments."""

    def __init__(
        self,
        chips: Mapping[ChipId, bytes] | Sequence[bytes] | Sequence[tuple[ChipId, bytes]],
        wirings: Sequence[Wiring | Mapping[str, Any] | Sequence[ChipId]] | Wiring | None = None,
        *,
        word_size: int = 1,
        word_length: int | None = None,
        permutations: Sequence[Sequence[ChipId]] | None = None,
        channel_permutations: Sequence[Sequence[ChipId]] | None = None,
        reversal_modes: Any = None,
        allowed_reversals: Any = None,
        known_bytes: Sequence[KnownByte | Mapping[str, Any] | tuple[int, int]] | None = None,
        pages: Sequence[PageChecksum | Mapping[str, Any]] | None = None,
        page_digests: Sequence[str | bytes | Mapping[str, Any]] | None = None,
        page_size: int | None = None,
        page_length: int | None = None,
        page_algorithm: str = "sha256",
        evidence: Sequence[Evidence | Mapping[str, Any]] | None = None,
    ) -> None:
        chosen_word_size = word_length if word_length is not None else word_size
        if (
            isinstance(chosen_word_size, bool)
            or not isinstance(chosen_word_size, int)
            or chosen_word_size <= 0
        ):
            raise ValueError("word_size/word_length must be a positive integer")
        self.word_size = chosen_word_size

        self.chip_ids, self.chips = _normalize_chips(chips)
        self.chip_length = len(next(iter(self.chips.values())))
        self.image_length = len(self.chip_ids) * self.chip_length

        if wirings is not None and isinstance(wirings, (Wiring, Mapping)):
            wiring_list: Any = [wirings]
        else:
            wiring_list = wirings
        self.wirings: dict[str, Wiring] = {}
        wiring_objects = normalize_wirings(
            wiring_list,
            self.chip_ids,
            permutations=permutations if permutations is not None else channel_permutations,
            reversal_modes=reversal_modes if reversal_modes is not None else allowed_reversals,
        )
        for wiring in wiring_objects:
            wid = wiring.id or f"W{len(self.wirings) + 1}"
            if wid in self.wirings:
                raise ValueError(f"duplicate wiring id: {wid}")
            if wiring.id is None:
                object.__setattr__(wiring, "id", wid)
            self.wirings[wid] = wiring
        if not self.wirings:
            raise ValueError("at least one candidate wiring is required")

        self.wiring_ids: tuple[str, ...] = tuple(self.wirings.keys())
        self._image_cache: dict[str, bytes] = {}
        self._survivor_versions: list[tuple[str, ...]] = [self.wiring_ids]
        self.history: list[HistoryEntry] = []
        self._answers: list[ReadReport] = []

        initial_evidence: list[Any] = []
        if known_bytes:
            initial_evidence.extend(known_bytes)
        if pages:
            initial_evidence.extend(pages)
        effective_page_size = page_size if page_size is not None else page_length
        if page_digests:
            for index, digest in enumerate(page_digests):
                if isinstance(digest, Mapping):
                    item = dict(digest)
                    item.setdefault("page", index)
                    if effective_page_size is not None:
                        item.setdefault("page_size", effective_page_size)
                    item.setdefault("algorithm", page_algorithm)
                else:
                    if effective_page_size is None:
                        raise ValueError("page_size/page_length is required with page_digests")
                    item = {
                        "page": index,
                        "page_size": effective_page_size,
                        "algorithm": page_algorithm,
                        "digest": digest,
                    }
                initial_evidence.append(item)
        if evidence:
            initial_evidence.extend(evidence)

        for item in initial_evidence:
            self.add_evidence(item)

    # ------------------------------------------------------------------
    # Candidate reconstruction
    # ------------------------------------------------------------------
    def image_for(self, wiring: str | Wiring) -> bytes:
        """Reconstruct and cache the full logical image for one wiring."""

        if isinstance(wiring, Wiring):
            wid = wiring.id
            if wid is None:
                raise ValueError("cannot cache a wiring without an id")
        else:
            wid = str(wiring)
        if wid in self._image_cache:
            return self._image_cache[wid]
        if wid not in self.wirings:
            raise KeyError(f"unknown wiring id: {wid!r}")
        candidate = self.wirings[wid]
        transformed = {
            chip_id: transform_chip(self.chips[chip_id], candidate.mode_for(chip_id), self.word_size)
            for chip_id in self.chip_ids
        }
        pieces: list[bytes] = []
        for base in range(0, self.chip_length, self.word_size):
            for chip_id in candidate.order:
                pieces.append(transformed[chip_id][base : base + self.word_size])
        image = b"".join(pieces)
        self._image_cache[wid] = image
        return image

    def possible_images(self) -> dict[str, bytes]:
        return {wid: self.image_for(wid) for wid in self.surviving_wiring_ids}

    # ------------------------------------------------------------------
    # Evidence and version state
    # ------------------------------------------------------------------
    @property
    def version(self) -> int:
        return len(self.history)

    @property
    def surviving_wiring_ids(self) -> tuple[str, ...]:
        return self._survivor_versions[-1]

    @property
    def survivors(self) -> tuple[Wiring, ...]:
        return tuple(self.wirings[wid] for wid in self.surviving_wiring_ids)

    @property
    def consistent(self) -> bool:
        return bool(self.surviving_wiring_ids)

    @property
    def candidate_count(self) -> int:
        return len(self.wirings)

    @property
    def answer_history_log(self) -> tuple[ReadReport, ...]:
        return tuple(self._answers)

    def add_evidence(self, evidence: Any = None, **kwargs: Any) -> HistoryEntry:
        """Apply one piece of evidence and record the resulting version."""

        if evidence is None:
            item = coerce_evidence(kwargs)
        else:
            item = coerce_evidence(evidence)
        item.validate(self.image_length)

        version = len(self.history) + 1
        before = self.surviving_wiring_ids
        after: list[str] = []
        rejections: list[Rejection] = []
        for wid in before:
            evaluation = item.verify(self.image_for(wid))
            if evaluation.ok:
                after.append(wid)
            else:
                rejections.append(
                    Rejection(
                        evidence_version=version,
                        wiring_id=wid,
                        reason=evaluation.reason or "evidence_mismatch",
                        message=evaluation.message or "evidence did not match candidate",
                        expected=evaluation.expected,
                        actual=evaluation.actual,
                        details=evaluation.details,
                    )
                )
        after_tuple = tuple(after)
        removed = tuple(wid for wid in before if wid not in after_tuple)
        entry = HistoryEntry(
            version=version,
            evidence=item,
            before_ids=before,
            after_ids=after_tuple,
            removed_ids=removed,
            rejections=tuple(rejections),
        )
        self.history.append(entry)
        self._survivor_versions.append(after_tuple)
        return entry

    def clone(self) -> "ForensicReader":
        """Return an independent reader with copied version/query ledgers.

        Chip bytes, reconstructions, evidence records, and reports are treated
        as immutable, so only the top-level mutable ledgers need copying.
        """

        clone = ForensicReader.__new__(ForensicReader)
        clone.__dict__.update(self.__dict__)
        clone.chips = dict(self.chips)
        clone.wirings = dict(self.wirings)
        clone._image_cache = dict(self._image_cache)
        clone._survivor_versions = list(self._survivor_versions)
        clone.history = list(self.history)
        clone._answers = list(self._answers)
        return clone

    def with_evidence(self, evidence: Any = None, **kwargs: Any) -> "ForensicReader":
        """Return a cloned reader after applying one additional piece of evidence."""

        clone = self.clone()
        clone.add_evidence(evidence, **kwargs)
        return clone

    def add_known_byte(
        self,
        address: int,
        value: int | bytes,
        source: str | None = None,
        note: str | None = None,
    ) -> HistoryEntry:
        return self.add_evidence(KnownByte(address, value, source=source, note=note))

    def add_page_checksum(
        self,
        digest: str | bytes | None = None,
        *,
        page: int | None = None,
        page_size: int | None = None,
        size: int | None = None,
        length: int | None = None,
        start: int | None = None,
        end: int | None = None,
        offset: int | None = None,
        algorithm: str | None = None,
        source: str | None = None,
        note: str | None = None,
        **kwargs: Any,
    ) -> HistoryEntry:
        expected = digest if digest is not None else kwargs.get("checksum", kwargs.get("expected_digest"))
        if expected is None:
            raise EvidenceFormatError("a digest/checksum is required")
        effective_size = page_size if page_size is not None else size if size is not None else length
        effective_start = start if start is not None else offset
        if page is not None:
            if effective_size is None:
                raise EvidenceFormatError("page_size/size/length is required with page")
            evidence = PageChecksum.for_page(
                page=page,
                page_size=effective_size,
                digest=expected,
                algorithm=algorithm,
                source=source,
                note=note,
            )
        else:
            if effective_start is None or end is None:
                raise EvidenceFormatError(
                    "provide page and page_size, or start/offset and end for a checksum interval"
                )
            evidence = PageChecksum.for_range(
                start=effective_start,
                end=end,
                digest=expected,
                algorithm=algorithm,
                source=source,
                note=note,
            )
        return self.add_evidence(evidence)

    # ------------------------------------------------------------------
    # Range handling and reading
    # ------------------------------------------------------------------
    def _resolve_range(
        self,
        start: int | slice | None = 0,
        end: int | None = None,
        *,
        length: int | None = None,
    ) -> tuple[int, int]:
        if isinstance(start, slice):
            if start.step is not None:
                raise LogicalRangeError("strided slices are not supported")
            slice_start = 0 if start.start is None else start.start
            slice_end = self.image_length if start.stop is None else start.stop
            return self._validate_range(slice_start, slice_end)
        if start is None:
            start = 0
        if end is not None and length is not None:
            raise LogicalRangeError("specify either end or length, not both")
        if length is not None:
            if isinstance(length, bool) or not isinstance(length, int) or length < 0:
                raise LogicalRangeError("length must be a non-negative integer")
            end = start + length
        elif end is None:
            end = self.image_length
        return self._validate_range(start, end)

    def _validate_range(self, start: Any, end: Any) -> tuple[int, int]:
        for value, name in ((start, "start"), (end, "end")):
            if isinstance(value, bool) or not isinstance(value, int):
                raise LogicalRangeError(f"{name} must be an integer")
        if start < 0:
            raise LogicalRangeError("start must be non-negative")
        if end < start:
            raise LogicalRangeError("end must be greater than or equal to start")
        if end > self.image_length:
            raise LogicalRangeError(
                f"interval end {end} exceeds logical image length {self.image_length}"
            )
        return start, end

    def read(
        self,
        start: int | slice = 0,
        end: int | None = None,
        *,
        length: int | None = None,
        address: int | None = None,
        offset: int | None = None,
        size: int | None = None,
    ) -> ReadReport:
        """Read a half-open logical interval and record the answer in the query log.

        The canonical form is ``read(start, end)``.  ``read(start, length=n)``,
        ``read(address=..., size=n)`` and slicing are also supported.
        """

        if address is not None or offset is not None:
            if not isinstance(start, int) or start != 0:
                raise LogicalRangeError("use either positional start or address/offset")
            start = address if address is not None else offset
        if length is None and size is not None:
            length = size
        norm_start, norm_end = self._resolve_range(start, end, length=length)
        report = self._build_report(
            norm_start,
            norm_end,
            version=self.version,
            survivor_ids=self.surviving_wiring_ids,
            answer_id=len(self._answers) + 1,
        )
        self._answers.append(report)
        return report

    def read_interval(self, start: int, end: int) -> ReadReport:
        return self.read(start, end)

    def read_range(self, start: int, end: int) -> ReadReport:
        return self.read(start, end)

    def read_length(self, start: int, length: int) -> ReadReport:
        return self.read(start, length=length)

    def read_bytes(self, start: int, length: int) -> bytes:
        """Return bytes only if the requested length is fully confirmed."""

        return self.read_length(start, length).confirmed_bytes()

    def read_at(self, address: int, size: int) -> ReadReport:
        """Read ``size`` bytes beginning at ``address``."""

        return self.read(address=address, size=size)

    def read_prefix(self, length: int) -> ReadReport:
        return self.read(0, length)

    def __getitem__(self, key: int | slice) -> ByteState | ReadReport:
        if isinstance(key, slice):
            return self.read(key)
        if isinstance(key, int):
            return self.read(key, key + 1).byte_at(key)
        raise TypeError("reader indices must be int or slice")

    # ------------------------------------------------------------------
    # Report construction
    # ------------------------------------------------------------------
    def _build_report(
        self,
        start: int,
        end: int,
        *,
        version: int,
        survivor_ids: tuple[str, ...],
        answer_id: int | None = None,
    ) -> ReadReport:
        states: list[ByteState] = []
        segments: list[Segment] = []

        if start == end:
            return ReadReport(
                start=start,
                end=end,
                image_length=self.image_length,
                version=version,
                evidence_count=version,
                survivor_ids=survivor_ids,
                segments=(),
                byte_states=(),
                answer_id=answer_id,
            )

        if not survivor_ids:
            for address in range(start, end):
                states.append(
                    ByteState(
                        address=address,
                        status=NO_WIRING,
                        value=None,
                        possible_values=(),
                        wiring_values=(),
                    )
                )
            segments.append(
                Segment(start=start, end=end, status=NO_WIRING, wiring_ids=())
            )
            return ReadReport(
                start=start,
                end=end,
                image_length=self.image_length,
                version=version,
                evidence_count=version,
                survivor_ids=(),
                segments=tuple(segments),
                byte_states=tuple(states),
                answer_id=answer_id,
            )

        images = {wid: self.image_for(wid) for wid in survivor_ids}

        for address in range(start, end):
            wiring_values = tuple((wid, images[wid][address]) for wid in survivor_ids)
            distinct = sorted({value for _, value in wiring_values})
            if len(distinct) == 1:
                states.append(
                    ByteState(
                        address=address,
                        status=CONFIRMED,
                        value=distinct[0],
                        possible_values=(distinct[0],),
                        wiring_values=wiring_values,
                    )
                )
            else:
                states.append(
                    ByteState(
                        address=address,
                        status=AMBIGUOUS,
                        value=None,
                        possible_values=tuple(distinct),
                        wiring_values=wiring_values,
                    )
                )

        position = start
        while position < end:
            first_value = images[survivor_ids[0]][position]
            is_confirmed = all(images[wid][position] == first_value for wid in survivor_ids)
            if is_confirmed:
                segment_end = position + 1
                while segment_end < end:
                    candidate_value = images[survivor_ids[0]][segment_end]
                    if not all(
                        images[wid][segment_end] == candidate_value for wid in survivor_ids
                    ):
                        break
                    segment_end += 1
                segments.append(
                    Segment(
                        start=position,
                        end=segment_end,
                        status=CONFIRMED,
                        wiring_ids=survivor_ids,
                        data=images[survivor_ids[0]][position:segment_end],
                    )
                )
                position = segment_end
                continue

            groups: dict[int, list[str]] = {}
            for wid in survivor_ids:
                groups.setdefault(images[wid][position], []).append(wid)
            relation = frozenset(frozenset(ids) for ids in groups.values())
            segment_end = position + 1
            while segment_end < end:
                next_groups: dict[int, list[str]] = {}
                for wid in survivor_ids:
                    next_groups.setdefault(images[wid][segment_end], []).append(wid)
                if len(next_groups) == 1:
                    break
                next_relation = frozenset(frozenset(ids) for ids in next_groups.values())
                if next_relation != relation:
                    break
                segment_end += 1

            interpretations: list[Interpretation] = []
            for value, wids in groups.items():
                interpretations.append(
                    Interpretation(
                        data=images[wids[0]][position:segment_end],
                        wiring_ids=tuple(wids),
                    )
                )
            segments.append(
                Segment(
                    start=position,
                    end=segment_end,
                    status=AMBIGUOUS,
                    wiring_ids=survivor_ids,
                    interpretations=tuple(interpretations),
                )
            )
            position = segment_end

        return ReadReport(
            start=start,
            end=end,
            image_length=self.image_length,
            version=version,
            evidence_count=version,
            survivor_ids=survivor_ids,
            segments=tuple(segments),
            byte_states=tuple(states),
            answer_id=answer_id,
        )

    # ------------------------------------------------------------------
    # Tracing and prior judgments
    # ------------------------------------------------------------------
    def trace(
        self,
        start: int | slice = 0,
        end: int | None = None,
        *,
        length: int | None = None,
    ) -> TraceReport:
        """Reconstruct the judgment for an interval at every evidence version."""

        norm_start, norm_end = self._resolve_range(start, end, length=length)
        entries: list[VersionJudgment] = []
        previous: ReadReport | None = None
        for version, survivor_ids in enumerate(self._survivor_versions):
            report = self._build_report(
                norm_start,
                norm_end,
                version=version,
                survivor_ids=survivor_ids,
            )
            diff = compare_reports(previous, report) if previous is not None else None
            summary = self.history[version - 1].evidence_summary if version > 0 else None
            entries.append(
                VersionJudgment(
                    version=version,
                    evidence_summary=summary,
                    report=report,
                    diff_from_previous=diff,
                )
            )
            previous = report
        return TraceReport(start=norm_start, end=norm_end, entries=tuple(entries))

    def trace_byte(self, address: int) -> TraceReport:
        return self.trace(address, address + 1)

    def report_at_version(
        self,
        version: int,
        start: int | slice = 0,
        end: int | None = None,
        *,
        length: int | None = None,
    ) -> ReadReport:
        """Reconstruct an interval judgment as it existed at evidence ``version``."""

        if isinstance(version, bool) or not isinstance(version, int):
            raise ForensicReaderError("version must be an integer")
        if not 0 <= version <= len(self.history):
            raise ForensicReaderError(f"version {version} is outside 0..{len(self.history)}")
        norm_start, norm_end = self._resolve_range(start, end, length=length)
        return self._build_report(
            norm_start,
            norm_end,
            version=version,
            survivor_ids=self._survivor_versions[version],
        )

    def answer_history_for(
        self,
        start: int | slice | None = None,
        end: int | None = None,
        *,
        length: int | None = None,
    ) -> tuple[ReadReport, ...]:
        """Return prior *actual answers* matching an interval."""

        if start is None:
            return tuple(self._answers)
        norm_start, norm_end = self._resolve_range(start, end, length=length)
        return tuple(
            report
            for report in self._answers
            if report.start == norm_start and report.end == norm_end
        )

    def wiring_report(self) -> list[dict[str, Any]]:
        current = set(self.surviving_wiring_ids)
        result = []
        for wid in self.wiring_ids:
            wiring = self.wirings[wid]
            result.append(
                {
                    "id": wid,
                    "surviving": wid in current,
                    "description": wiring.short_description(),
                    "wiring": wiring.to_dict(),
                }
            )
        return result
