"""Parsing, idempotent de-duplication, and sequence-gap detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .model import Event, EventValidationError, Issue, parse_events


@dataclass
class DuplicateReport:
    reason: str
    kept_event_id: str | None
    duplicate_event_id: str | None
    fingerprint: str
    kind: str
    timestamp: float
    client_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "kept_event_id": self.kept_event_id,
            "duplicate_event_id": self.duplicate_event_id,
            "fingerprint": self.fingerprint,
            "kind": self.kind,
            "timestamp": self.timestamp,
            "client_id": self.client_id,
        }


@dataclass
class SequenceGap:
    client_id: str
    stream: str
    before_seq: int
    after_seq: int
    missing_event_reports: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "stream": self.stream,
            "before_seq": self.before_seq,
            "after_seq": self.after_seq,
            "missing_event_reports": self.missing_event_reports,
        }


@dataclass
class IngestionResult:
    events: list[Event] = field(default_factory=list)
    duplicates: list[DuplicateReport] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    gaps: list[SequenceGap] = field(default_factory=list)

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicates)


def ingest_records(
    records: Any,
    *,
    detect_sequence_gaps: bool = True,
    assume_seq_starts_at_one: bool = False,
) -> IngestionResult:
    """Parse and de-duplicate event records.

    Invalid records become structured issues; they do not abort analysis of the
    remaining valid records.
    """

    result = IngestionResult()
    raw_records = list(records)

    parsed: list[Event] = []
    for index, record in enumerate(raw_records):
        try:
            if isinstance(record, Event):
                parsed.append(record)
            elif isinstance(record, Mapping):
                parsed.append(Event.from_mapping(record))
            else:
                raise EventValidationError(
                    f"record at index {index} must be a mapping or Event, got {type(record)!r}"
                )
        except EventValidationError as exc:
            result.issues.append(
                Issue(
                    code="invalid_event",
                    message=str(exc),
                    details={"record_index": index},
                )
            )
        except Exception as exc:  # Defensive: never let one bad row kill a report.
            result.issues.append(
                Issue(
                    code="invalid_event",
                    message=f"{type(exc).__name__}: {exc}",
                    details={"record_index": index},
                )
            )

    seen_fingerprints: dict[str, Event] = {}
    seen_event_ids: dict[str, Event] = {}

    for event in parsed:
        fingerprint = event.payload_fingerprint()
        existing_fp = seen_fingerprints.get(fingerprint)
        if existing_fp is not None:
            result.duplicates.append(
                DuplicateReport(
                    reason="identical_payload",
                    kept_event_id=existing_fp.event_id,
                    duplicate_event_id=event.event_id,
                    fingerprint=fingerprint,
                    kind=event.kind,
                    timestamp=event.timestamp,
                    client_id=event.client_id,
                )
            )
            continue

        if event.event_id and event.event_id in seen_event_ids:
            kept = seen_event_ids[event.event_id]
            result.issues.append(
                Issue(
                    code="event_id_conflict",
                    message=(
                        f"event_id {event.event_id!r} was reused with a different payload; "
                        "keeping the first report"
                    ),
                    event_id=event.event_id,
                    client_id=event.client_id,
                    details={
                        "kept_fingerprint": kept.payload_fingerprint(),
                        "rejected_fingerprint": fingerprint,
                    },
                )
            )
            result.duplicates.append(
                DuplicateReport(
                    reason="conflicting_event_id_kept_first",
                    kept_event_id=kept.event_id,
                    duplicate_event_id=event.event_id,
                    fingerprint=fingerprint,
                    kind=event.kind,
                    timestamp=event.timestamp,
                    client_id=event.client_id,
                )
            )
            continue

        result.events.append(event)
        seen_fingerprints[fingerprint] = event
        if event.event_id:
            seen_event_ids[event.event_id] = event

    if detect_sequence_gaps:
        result.gaps.extend(
            detect_gaps(
                result.events,
                assume_starts_at_one=assume_seq_starts_at_one,
            )
        )
        for gap in result.gaps:
            result.issues.append(
                Issue(
                    code="sequence_gap",
                    message=(
                        f"missing {gap.missing_event_reports} sequence report(s) between "
                        f"{gap.before_seq} and {gap.after_seq} on {gap.client_id}/"
                        f"{gap.stream}"
                    ),
                    client_id=gap.client_id,
                    details=gap.to_dict(),
                )
            )

    return result


def detect_gaps(
    events: list[Event],
    *,
    assume_starts_at_one: bool = False,
) -> list[SequenceGap]:
    by_stream: dict[tuple[str, str], set[int]] = {}
    for event in events:
        if event.seq is None:
            continue
        stream = event.seq_stream or event.metadata.get("sequence_stream") or "client"
        key = (event.client_id, str(stream))
        by_stream.setdefault(key, set()).add(event.seq)

    gaps: list[SequenceGap] = []
    for (client, stream), seq_set in sorted(by_stream.items()):
        seqs = sorted(seq_set)
        if assume_starts_at_one and seqs and seqs[0] > 1:
            gaps.append(
                SequenceGap(
                    client_id=client,
                    stream=stream,
                    before_seq=0,
                    after_seq=seqs[0],
                    missing_event_reports=seqs[0] - 1,
                )
            )
        for left, right in zip(seqs, seqs[1:]):
            if right > left + 1:
                gaps.append(
                    SequenceGap(
                        client_id=client,
                        stream=stream,
                        before_seq=left,
                        after_seq=right,
                        missing_event_reports=right - left - 1,
                    )
                )
    return gaps
