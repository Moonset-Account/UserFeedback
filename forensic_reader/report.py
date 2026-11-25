"""Immutable read reports, ambiguity interpretations, and trace types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import AmbiguousReadError

CONFIRMED = "confirmed"
AMBIGUOUS = "ambiguous"
NO_WIRING = "no_consistent_wiring"
EMPTY = "empty"


@dataclass(frozen=True)
class Interpretation:
    """One possible byte string for an ambiguous segment and its supporting wirings."""

    data: bytes
    wiring_ids: tuple[str, ...]

    @property
    def hex(self) -> str:
        return self.data.hex()

    @property
    def length(self) -> int:
        return len(self.data)

    @property
    def wiring_count(self) -> int:
        return len(self.wiring_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hex": self.hex,
            "length": self.length,
            "wiring_ids": list(self.wiring_ids),
            "wiring_count": self.wiring_count,
        }


@dataclass(frozen=True)
class Segment:
    """A maximal confirmed run or a fixed-support-group ambiguous run."""

    start: int
    end: int
    status: str
    wiring_ids: tuple[str, ...] = ()
    data: bytes | None = None
    interpretations: tuple[Interpretation, ...] = ()

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def wiring_count(self) -> int:
        return len(self.wiring_ids)

    @property
    def hex(self) -> str | None:
        return self.data.hex() if self.data is not None else None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "start": self.start,
            "end": self.end,
            "length": self.length,
            "status": self.status,
            "wiring_ids": list(self.wiring_ids),
            "wiring_count": self.wiring_count,
        }
        if self.data is not None:
            result["hex"] = self.data.hex()
        if self.interpretations:
            result["interpretations"] = [item.to_dict() for item in self.interpretations]
        return result


@dataclass(frozen=True)
class ByteState:
    address: int
    status: str
    value: int | None = None
    possible_values: tuple[int, ...] = ()
    wiring_values: tuple[tuple[str, int], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "status": self.status,
            "value_hex": f"0x{self.value:02x}" if self.value is not None else None,
            "possible_values_hex": [f"0x{v:02x}" for v in self.possible_values],
            "wiring_values": [
                {"wiring_id": wid, "value_hex": f"0x{value:02x}"}
                for wid, value in self.wiring_values
            ],
        }


@dataclass(frozen=True)
class ReadReport:
    """The answer for one logical interval at one evidence version."""

    start: int
    end: int
    image_length: int
    version: int
    evidence_count: int
    survivor_ids: tuple[str, ...]
    segments: tuple[Segment, ...]
    byte_states: tuple[ByteState, ...]
    answer_id: int | None = None

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def consistent(self) -> bool:
        return bool(self.survivor_ids)

    @property
    def has_consistent_wiring(self) -> bool:
        return bool(self.survivor_ids)

    @property
    def survivor_count(self) -> int:
        return len(self.survivor_ids)

    @property
    def has_multiple_wirings(self) -> bool:
        return len(self.survivor_ids) > 1

    @property
    def fully_confirmed(self) -> bool:
        if self.length == 0:
            return True
        return bool(self.byte_states) and all(item.status == CONFIRMED for item in self.byte_states)

    @property
    def is_empty(self) -> bool:
        return self.length == 0

    @property
    def state(self) -> str:
        if self.length == 0:
            return EMPTY
        if not self.survivor_ids:
            return NO_WIRING
        if self.fully_confirmed:
            return CONFIRMED
        return AMBIGUOUS

    @property
    def confirmed_count(self) -> int:
        return sum(item.status == CONFIRMED for item in self.byte_states)

    @property
    def ambiguous_count(self) -> int:
        return sum(item.status == AMBIGUOUS for item in self.byte_states)

    @property
    def confirmed_segments(self) -> tuple[Segment, ...]:
        return tuple(item for item in self.segments if item.status == CONFIRMED)

    @property
    def ambiguous_segments(self) -> tuple[Segment, ...]:
        return tuple(item for item in self.segments if item.status == AMBIGUOUS)

    @property
    def no_wiring_segments(self) -> tuple[Segment, ...]:
        return tuple(item for item in self.segments if item.status == NO_WIRING)

    def confirmed_bytes(self) -> bytes:
        """Return the full interval bytes only when every byte is confirmed."""

        if not self.survivor_ids:
            raise AmbiguousReadError("no consistent wiring remains", report=self)
        if not self.fully_confirmed:
            raise AmbiguousReadError(
                f"interval [{self.start},{self.end}) contains ambiguous bytes",
                report=self,
            )
        return b"".join(segment.data or b"" for segment in self.confirmed_segments)

    def __bytes__(self) -> bytes:
        return self.confirmed_bytes()

    def byte_at(self, address: int) -> ByteState:
        for state in self.byte_states:
            if state.address == address:
                return state
        raise IndexError(f"address {address} is not in report interval [{self.start},{self.end})")

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer_id": self.answer_id,
            "start": self.start,
            "end": self.end,
            "length": self.length,
            "image_length": self.image_length,
            "version": self.version,
            "evidence_count": self.evidence_count,
            "state": self.state,
            "consistent": self.consistent,
            "fully_confirmed": self.fully_confirmed,
            "survivor_count": self.survivor_count,
            "has_multiple_wirings": self.has_multiple_wirings,
            "survivor_ids": list(self.survivor_ids),
            "confirmed_count": self.confirmed_count,
            "ambiguous_count": self.ambiguous_count,
            "segments": [segment.to_dict() for segment in self.segments],
            "byte_states": [state.to_dict() for state in self.byte_states],
        }


@dataclass(frozen=True)
class ConfirmedRange:
    start: int
    end: int
    data: bytes
    from_version: int
    to_version: int

    @property
    def hex(self) -> str:
        return self.data.hex()

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "length": self.end - self.start,
            "hex": self.hex,
            "from_version": self.from_version,
            "to_version": self.to_version,
        }


@dataclass(frozen=True)
class LostRange:
    start: int
    end: int
    from_version: int
    to_version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "length": self.end - self.start,
            "from_version": self.from_version,
            "to_version": self.to_version,
        }


@dataclass(frozen=True)
class ReportDiff:
    old_version: int
    new_version: int
    same_interval: bool
    removed_wiring_ids: tuple[str, ...] = ()
    newly_confirmed: tuple[ConfirmedRange, ...] = ()
    lost_ranges: tuple[LostRange, ...] = ()
    old_state: str | None = None
    new_state: str | None = None

    @property
    def confirmation_added(self) -> bool:
        return bool(self.newly_confirmed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_version": self.old_version,
            "new_version": self.new_version,
            "same_interval": self.same_interval,
            "old_state": self.old_state,
            "new_state": self.new_state,
            "removed_wiring_ids": list(self.removed_wiring_ids),
            "newly_confirmed": [item.to_dict() for item in self.newly_confirmed],
            "lost_ranges": [item.to_dict() for item in self.lost_ranges],
        }


@dataclass(frozen=True)
class VersionJudgment:
    version: int
    evidence_summary: str | None
    report: ReadReport
    diff_from_previous: ReportDiff | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "evidence_summary": self.evidence_summary,
            "report": self.report.to_dict(),
            "diff_from_previous": (
                self.diff_from_previous.to_dict() if self.diff_from_previous else None
            ),
        }


@dataclass(frozen=True)
class TraceReport:
    start: int
    end: int
    entries: tuple[VersionJudgment, ...] = field(default_factory=tuple)

    @property
    def initial(self) -> ReadReport:
        return self.entries[0].report

    @property
    def current(self) -> ReadReport:
        return self.entries[-1].report

    def __len__(self) -> int:
        return len(self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "entries": [entry.to_dict() for entry in self.entries],
        }
