"""Structured event schema and log loading.

An event is the smallest auditable record emitted by the file service.
Ordering information is carried explicitly by (thread, seq) and by
synchronization edges -- never by the position of the record in the input
stream, because records may arrive out of order.
"""

from dataclasses import dataclass, field
from typing import Any

HANDLE_CREATE = "HANDLE_CREATE"
BIND = "BIND"
VALIDATE_OK = "VALIDATE_OK"
WRITE_COMMIT = "WRITE_COMMIT"
CANCEL = "CANCEL"
RETRY = "RETRY"
SYNC = "SYNC"

EVENT_TYPES = frozenset(
    {HANDLE_CREATE, BIND, VALIDATE_OK, WRITE_COMMIT, CANCEL, RETRY, SYNC}
)

SYNC_SIGNAL = "signal"
SYNC_WAIT = "wait"


@dataclass(frozen=True)
class Event:
    event_id: str
    type: str
    thread: str | None = None
    seq: int | None = None
    task: str | None = None
    handle: str | None = None
    object_no: str | None = None
    object_gen: int | None = None
    sync_id: str | None = None
    sync_op: str | None = None
    depends_on: tuple[str, ...] = field(default_factory=tuple)

    def label(self) -> str:
        """Short human readable descriptor used in evidence lines."""
        pos = f"{self.thread}#{self.seq}" if self.thread else "?"
        parts = [self.event_id, f"({pos} {self.type}"]
        if self.task:
            parts.append(f" task={self.task}")
        if self.handle:
            parts.append(f" handle={self.handle}")
        if self.object_no is not None:
            gen = "?" if self.object_gen is None else self.object_gen
            parts.append(f" obj={self.object_no}@gen{gen}")
        if self.sync_id:
            parts.append(f" sync={self.sync_id}/{self.sync_op}")
        parts.append(")")
        return "".join(parts)


def load_events(data: Any) -> list[Event]:
    """Parse a raw JSON-decoded log into Events.

    Malformed records raise ValueError -- the input schema itself is a hard
    contract. Semantic contradictions (missing predecessors etc.) are handled
    downstream as findings, not load errors.
    """
    if not isinstance(data, list):
        raise ValueError("event log must be a JSON array of event objects")

    events: list[Event] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(data):
        if not isinstance(raw, dict):
            raise ValueError(f"record #{index} is not an object")
        eid = raw.get("event_id") or f"#{index}"
        eid = str(eid)
        if eid in seen_ids:
            raise ValueError(f"duplicate event_id: {eid!r}")
        seen_ids.add(eid)

        etype = raw.get("type")
        if etype not in EVENT_TYPES:
            raise ValueError(f"event {eid!r}: unknown type {etype!r}")

        seq = raw.get("seq")
        if seq is not None and not isinstance(seq, int):
            raise ValueError(f"event {eid!r}: seq must be an integer")
        gen = raw.get("object_gen")
        if gen is not None and not isinstance(gen, int):
            raise ValueError(f"event {eid!r}: object_gen must be an integer")

        deps = raw.get("depends_on", ())
        if not isinstance(deps, (list, tuple)):
            raise ValueError(f"event {eid!r}: depends_on must be a list")

        sync_op = raw.get("sync_op")
        if etype == SYNC and sync_op not in (SYNC_SIGNAL, SYNC_WAIT):
            raise ValueError(
                f"event {eid!r}: SYNC event needs sync_op "
                f"'{SYNC_SIGNAL}' or '{SYNC_WAIT}'"
            )

        events.append(
            Event(
                event_id=eid,
                type=etype,
                thread=raw.get("thread"),
                seq=seq,
                task=raw.get("task"),
                handle=raw.get("handle"),
                object_no=raw.get("object_no"),
                object_gen=gen,
                sync_id=raw.get("sync_id"),
                sync_op=sync_op,
                depends_on=tuple(str(d) for d in deps),
            )
        )
    return events
