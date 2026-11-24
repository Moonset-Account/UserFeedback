"""Out-of-order receipt replay and observation-position completeness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .core import (
    AccountingReport,
    AccountingError,
    FrameRef,
    MemoryAccount,
    _first_present,
)


class GapError(AccountingError):
    """Raised when confirmation would cross an incomplete observation gap."""


@dataclass(frozen=True, slots=True)
class GapInfo:
    position: int
    state: str
    missing_sources: tuple[str, ...] = ()
    received_sources: tuple[str, ...] = ()
    sealed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "state": self.state,
            "missing_sources": list(self.missing_sources),
            "received_sources": list(self.received_sources),
            "sealed": self.sealed,
        }


@dataclass(slots=True)
class Receipt:
    position: int
    source: str
    payload: dict[str, Any]
    receipt_id: str | None = None
    sealed_requested: bool = False
    ingest_index: int = 0

    @classmethod
    def from_object(cls, obj: Receipt | Mapping[str, Any], index: int) -> "Receipt":
        if isinstance(obj, Receipt):
            obj.ingest_index = index
            return obj
        if not isinstance(obj, Mapping):
            raise AccountingError(f"receipt must be an object, got {type(obj).__name__}")

        position = _first_present(
            obj, "position", "observation_position", "pos", "offset", "seq", "sequence"
        )
        if position is None:
            raise AccountingError(f"receipt is missing an observation position: {obj!r}")

        source = _first_present(
            obj, "source", "source_id", "collector", "collector_id", "origin", "shard"
        )
        receipt_id = _first_present(obj, "receipt_id", "id", "ack_id")
        sealed = bool(
            _first_present(obj, "sealed", "confirmed", "committed", "complete", "completed")
            or False
        )
        return cls(
            position=int(position),
            source="default" if source is None else str(source),
            payload=dict(obj),
            receipt_id=None if receipt_id is None else str(receipt_id),
            sealed_requested=sealed,
            ingest_index=index,
        )


@dataclass(slots=True)
class PositionStatus:
    position: int
    known: bool
    complete: bool
    sealed: bool
    received_sources: set[str] = field(default_factory=set)
    expected_sources: set[str] = field(default_factory=set)
    receipt_count: int = 0

    @property
    def missing_sources(self) -> set[str]:
        return set(self.expected_sources - self.received_sources)

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "known": self.known,
            "complete": self.complete,
            "sealed": self.sealed,
            "received_sources": sorted(self.received_sources),
            "expected_sources": sorted(self.expected_sources),
            "missing_sources": sorted(self.missing_sources),
            "receipt_count": self.receipt_count,
        }


@dataclass(frozen=True, slots=True)
class ReplayResult:
    page_size: int
    first_position: int
    latest_accountable_position: int | None
    watermark: int | None
    latest_complete_position: int | None
    complete_positions: tuple[int, ...]
    gaps: tuple[GapInfo, ...]
    report: AccountingReport
    reports: dict[int, AccountingReport]
    isolated_reports: dict[int, AccountingReport] = field(default_factory=dict)
    duplicate_receipt_ids: tuple[str, ...] = ()

    @property
    def latest_report(self) -> AccountingReport:
        return self.report

    @property
    def latest_isolated_report(self) -> AccountingReport | None:
        if not self.isolated_reports:
            return None
        latest = max(self.isolated_reports)
        return self.isolated_reports[latest]

    @property
    def stale_removal_count(self) -> int:
        return self.report.stale_removal_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_size": self.page_size,
            "first_position": self.first_position,
            "latest_accountable_position": self.latest_accountable_position,
            "watermark": self.watermark,
            "latest_complete_position": self.latest_complete_position,
            "complete_positions": list(self.complete_positions),
            "gaps": [gap.to_dict() for gap in self.gaps],
            "report": self.report.to_dict(),
            "reports": {
                str(position): item.to_dict() for position, item in self.reports.items()
            },
            "isolated_reports": {
                str(position): item.to_dict()
                for position, item in self.isolated_reports.items()
            },
            "duplicate_receipt_ids": list(self.duplicate_receipt_ids),
        }


class ReplaySession:
    """Ingest receipts in any order and replay confirmed prefixes."""

    def __init__(
        self,
        *,
        page_size: int = 4096,
        first_position: int = 1,
        expected_sources: Sequence[str] | Mapping[int, Sequence[str]] | None = None,
        expected_positions: Iterable[int] | None = None,
        require_seal: bool = False,
        strict_mappings: bool = False,
    ) -> None:
        self.page_size = int(page_size)
        self.first_position = int(first_position)
        self.require_seal = require_seal
        self.strict_mappings = strict_mappings

        self._global_sources: set[str] | None = None
        self._position_sources: dict[int, set[str]] = {}
        if expected_sources is not None:
            if isinstance(expected_sources, Mapping):
                for pos, sources in expected_sources.items():
                    self._position_sources[int(pos)] = {str(s) for s in sources}
            else:
                self._global_sources = {str(s) for s in expected_sources}

        self.expected_positions: set[int] | None = (
            {int(p) for p in expected_positions}
            if expected_positions is not None
            else None
        )
        self._receipts: dict[int, list[Receipt]] = {}
        self._sealed: set[int] = set()
        self._receipt_ids: set[str] = set()
        self.duplicate_receipt_ids: list[str] = []
        self._ingest_count = 0

    # ------------------------------------------------------------------
    # Ingestion and confirmation
    # ------------------------------------------------------------------
    def ingest(self, receipt: Receipt | Mapping[str, Any]) -> PositionStatus:
        item = Receipt.from_object(receipt, self._ingest_count)
        self._ingest_count += 1

        if item.receipt_id is not None and item.receipt_id in self._receipt_ids:
            if item.receipt_id not in self.duplicate_receipt_ids:
                self.duplicate_receipt_ids.append(item.receipt_id)
            return self.status(item.position)

        self._receipts.setdefault(item.position, []).append(item)
        if item.receipt_id is not None:
            self._receipt_ids.add(item.receipt_id)
        if item.sealed_requested:
            self.seal_position(item.position, force=True)
        return self.status(item.position)

    ingest_receipt = ingest

    def ingest_many(self, receipts: Iterable[Receipt | Mapping[str, Any]]) -> "ReplaySession":
        for receipt in receipts:
            self.ingest(receipt)
        return self

    def add_expected_sources(self, position: int, sources: Iterable[str]) -> None:
        self._position_sources.setdefault(int(position), set()).update(
            str(s) for s in sources
        )

    def seal_position(self, position: int, *, force: bool = False) -> PositionStatus:
        position = int(position)
        status = self.status(position)
        if not force and status.missing_sources:
            raise GapError(
                f"position {position} is missing sources: "
                f"{sorted(status.missing_sources)}"
            )
        self._sealed.add(position)
        return self.status(position)

    confirm_position = seal_position
    mark_complete = lambda self, position: self.seal_position(position, force=True)  # noqa: E731

    # ------------------------------------------------------------------
    # Completeness
    # ------------------------------------------------------------------
    def expected_sources_for(self, position: int) -> set[str]:
        if position in self._position_sources:
            return set(self._position_sources[position])
        if self._global_sources is not None:
            return set(self._global_sources)
        return set()

    def status(self, position: int) -> PositionStatus:
        position = int(position)
        receipts = self._receipts.get(position, [])
        received = {r.source for r in receipts}
        expected = self.expected_sources_for(position)
        sealed = position in self._sealed
        known = bool(receipts) or sealed or position in self._position_sources
        if self.expected_positions is not None and position in self.expected_positions:
            known = True

        sources_complete = expected.issubset(received) if expected else bool(receipts)
        if not expected and not receipts and sealed:
            sources_complete = True
        # An explicit forced seal is an authoritative completion marker and can
        # override a stale expected-source configuration.  In normal operation
        # seal_position() refuses to create that state without force=True.
        complete = sealed or (sources_complete and not self.require_seal)
        return PositionStatus(
            position=position,
            known=known,
            complete=complete,
            sealed=sealed,
            received_sources=received,
            expected_sources=expected,
            receipt_count=len(receipts),
        )

    def known_positions(self) -> list[int]:
        positions = set(self._receipts) | self._sealed | set(self._position_sources)
        if self.expected_positions is not None:
            positions.update(self.expected_positions)
        return sorted(positions)

    def universe_positions(self) -> list[int]:
        known = self.known_positions()
        if not known:
            return []
        upper = max(known)
        lower = min(self.first_position, min(known))
        if self.expected_positions is not None:
            return sorted(
                set(range(lower, upper + 1)) | set(self.expected_positions) | set(known)
            )
        return list(range(lower, upper + 1))

    def complete_positions(self) -> list[int]:
        return [p for p in self.universe_positions() if self.status(p).complete]

    def gaps(self) -> list[GapInfo]:
        result: list[GapInfo] = []
        for position in self.universe_positions():
            status = self.status(position)
            if status.complete:
                continue
            result.append(
                GapInfo(
                    position=position,
                    state="missing_position" if status.receipt_count == 0 else "incomplete",
                    missing_sources=tuple(sorted(status.missing_sources)),
                    received_sources=tuple(sorted(status.received_sources)),
                    sealed=status.sealed,
                )
            )
        return result

    def watermark(self) -> int | None:
        position = self.first_position
        if not self.status(position).complete:
            return None
        while self.status(position).complete:
            position += 1
        return position - 1

    latest_accountable_position = watermark

    def latest_complete_position(self) -> int | None:
        complete = self.complete_positions()
        return complete[-1] if complete else None

    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------
    def receipts_at(self, position: int) -> list[Receipt]:
        return sorted(self._receipts.get(int(position), []), key=lambda r: r.ingest_index)

    def _new_account(self) -> MemoryAccount:
        return MemoryAccount(
            page_size=self.page_size, strict_mappings=self.strict_mappings
        )

    def replay_through(self, position: int) -> AccountingReport:
        """Replay the contiguous confirmed prefix up to ``position``."""

        position = int(position)
        account = self._new_account()
        current = self.first_position
        while current <= position:
            status = self.status(current)
            if not status.complete:
                raise GapError(
                    f"cannot replay through {position}; position {current} is not complete"
                )
            for receipt in self.receipts_at(current):
                apply_receipt(account, receipt.payload)
            current += 1
        return account.report()

    def isolated_position_report(self, position: int) -> AccountingReport:
        """Build only from receipts at one complete position.

        This is appropriate when each receipt/process snapshot is authoritative
        for that observation point.  It does not replay events from earlier
        positions.  Use :meth:`replay_through` for cumulative event semantics.
        """

        position = int(position)
        status = self.status(position)
        if not status.complete:
            raise GapError(f"position {position} is not complete")
        account = self._new_account()
        for receipt in self.receipts_at(position):
            apply_receipt(account, receipt.payload)
        return account.report()

    def result(self) -> ReplayResult:
        watermark = self.watermark()
        reports: dict[int, AccountingReport] = {}
        account = self._new_account()

        if watermark is not None:
            for position in range(self.first_position, watermark + 1):
                for receipt in self.receipts_at(position):
                    apply_receipt(account, receipt.payload)
                reports[position] = account.report()
            final_report = reports[watermark]
        else:
            final_report = account.report()

        complete = self.complete_positions()
        latest_complete = complete[-1] if complete else None
        isolated_reports = {
            position: self.isolated_position_report(position)
            for position in complete
        }
        return ReplayResult(
            page_size=self.page_size,
            first_position=self.first_position,
            latest_accountable_position=watermark,
            watermark=watermark,
            latest_complete_position=latest_complete,
            complete_positions=tuple(complete),
            gaps=tuple(self.gaps()),
            report=final_report,
            reports=reports,
            isolated_reports=isolated_reports,
            duplicate_receipt_ids=tuple(self.duplicate_receipt_ids),
        )


# Make the alias readable after class definition.
setattr(ReplaySession, "latest_accountable_position", ReplaySession.watermark)


def _frame_from_record(record: Mapping[str, Any]) -> FrameRef:
    nested = record.get("frame")
    pfn = _first_present(record, "pfn", "page_frame_number", "frame_number")
    generation = _first_present(
        record, "reuse_id", "reuse_number", "reuse", "generation"
    )

    if isinstance(nested, (Mapping, FrameRef)):
        return FrameRef.coerce(nested, pfn=pfn, reuse_id=generation)
    if pfn is not None:
        return FrameRef.coerce(pfn=pfn, reuse_id=generation)
    if nested is not None:
        return FrameRef.coerce(nested, reuse_id=generation)
    return FrameRef.coerce(record)


def apply_receipt(account: MemoryAccount, receipt: Mapping[str, Any] | Receipt) -> None:
    """Apply all process snapshots and records contained in a receipt."""

    payload = receipt.payload if isinstance(receipt, Receipt) else receipt
    if not isinstance(payload, Mapping):
        raise AccountingError("receipt payload must be an object")

    # A receipt may itself be one record.
    kind = _first_present(payload, "kind", "type", "op", "event", "action")
    envelope_kinds = {
        "receipt",
        "collection_receipt",
        "observation_position",
    }
    snapshot_kinds = {
        "snapshot",
        "process_snapshot",
        "observation",
        "process",
        "collect",
        "collected",
    }
    if kind is not None and kind not in envelope_kinds:
        apply_record(account, payload)

    # Snapshot-kind payloads were fully handled by apply_record(); avoid
    # reconciling the same processes twice.
    if str(kind).lower() not in snapshot_kinds:
        for process in payload.get("processes", []) or []:
            account.snapshot_process(process)

    # A receipt can itself be one process snapshot.
    if (
        not payload.get("processes")
        and kind is None
        and _first_present(payload, "pid", "process", "process_id") is not None
        and (
            "pages" in payload
            or "resident_pages" in payload
            or "resident_frames" in payload
            or "mappings" in payload
            or "maps" in payload
        )
    ):
        account.snapshot_process(payload)

    records = payload.get("events")
    if records is None:
        records = payload.get("records", payload.get("changes", []))
    for record in records or []:
        apply_record(account, record)


def apply_record(account: MemoryAccount, record: Mapping[str, Any]) -> None:
    if not isinstance(record, Mapping):
        raise AccountingError(f"event record must be an object: {record!r}")

    kind = str(
        _first_present(record, "kind", "type", "op", "event", "action") or "snapshot"
    ).lower()

    if kind in {
        "snapshot",
        "process_snapshot",
        "observation",
        "process",
        "collect",
        "collected",
    }:
        if "processes" in record:
            for process in record["processes"]:
                account.snapshot_process(process)
        else:
            account.snapshot_process(record)
        return

    if kind in {"mapping", "upsert_mapping", "add_mapping", "declare_mapping"}:
        mapping = record.get("mapping", record)
        pid = _first_present(record, "pid", "process", "process_id")
        if isinstance(mapping, Mapping) and pid is not None and "pid" not in mapping:
            mapping = dict(mapping)
            mapping["pid"] = pid
        account.declare_mapping(mapping) if isinstance(mapping, Mapping) else account.declare_mapping(record)
        return

    if kind in {"remove_mapping", "delete_mapping", "unmap_mapping"}:
        pid = _first_present(record, "pid", "process", "process_id")
        mid = _first_present(record, "mapping_id", "mapping", "id", "vma")
        account.remove_mapping(pid, str(mid))
        return

    if kind in {
        "resident",
        "add",
        "map",
        "add_resident",
        "page",
        "resident_page",
        "observe_page",
    }:
        account.add_resident(record)
        return

    if kind in {
        "unmap",
        "remove",
        "remove_resident",
        "delete",
        "unmap_page",
        "remove_page",
    }:
        account.remove_resident(record)
        return

    if kind in {
        "revoke",
        "revoke_frame",
        "frame_revoked",
        "free",
        "free_frame",
        "release_frame",
        "tombstone",
    }:
        account.revoke_frame(_frame_from_record(record), reason=kind)
        return

    if kind in {"cow", "copy_on_write", "write_fault", "cow_fault"}:
        account.copy_on_write(record)
        return

    raise AccountingError(f"unknown observation record kind: {kind!r}")


def replay_receipts(
    data: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    **options: Any,
) -> ReplayResult:
    """Replay a JSON-like observation document or an iterable of receipts."""

    receipts: Iterable[Mapping[str, Any] | Receipt]
    if isinstance(data, Mapping):
        if "page_size" in data and "page_size" not in options:
            options["page_size"] = int(data["page_size"])
        if "first_position" in data and "first_position" not in options:
            options["first_position"] = int(data["first_position"])
        if "require_seal" in data and "require_seal" not in options:
            options["require_seal"] = bool(data["require_seal"])
        if "strict_mappings" in data and "strict_mappings" not in options:
            options["strict_mappings"] = bool(data["strict_mappings"])
        if "expected_sources" in data and "expected_sources" not in options:
            options["expected_sources"] = data["expected_sources"]
        if "expected_positions" in data and "expected_positions" not in options:
            options["expected_positions"] = data["expected_positions"]

        if "receipts" in data:
            receipts = data["receipts"]
        elif "observations" in data:
            receipts = data["observations"]
        elif "position" in data or "processes" in data or "source" in data:
            # A single receipt object was passed directly.
            receipts = [data]
        elif any(key in data for key in ("events", "records", "changes")):
            records = data.get("events", data.get("records", data.get("changes", [])))
            if all(isinstance(item, Mapping) and "position" in item for item in records):
                receipts = records
            else:
                receipts = [
                    {
                        "position": options.get("first_position", data.get("first_position", 1)),
                        "source": "events",
                        "events": records,
                    }
                ]
        else:
            receipts = []
    else:
        receipts = data

    session = ReplaySession(**options)
    session.ingest_many(receipts)
    return session.result()


def account_receipts(
    data: Mapping[str, Any] | Iterable[Mapping[str, Any]], **options: Any
) -> AccountingReport:
    """Convenience wrapper returning the report at the replay watermark."""

    return replay_receipts(data, **options).report
