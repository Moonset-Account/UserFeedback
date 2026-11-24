"""Reference monitor enforcing label-based read/upgrade and write-back rules.

Policy implemented (defensive endpoint policy verification only):

* Reading an object merges the reader's label with the object label
  (component-wise max level, union of categories).  When the merged label would
  exceed the reader's clearance the *whole* read fails: no content is returned
  and the process label is untouched.
* Every accepted read that actually raises the process label immediately
  revokes every *writable shared* mapping of that process whose target label no
  longer dominates the new process label.  All handles duplicated from one such
  mapping share the mapping's state, so they are revoked together.
* A write-back through a revoked (or otherwise no-longer-satisfied) handle is
  refused and the target object's content is left unchanged.  Write-backs whose
  target still covers the new (higher) label complete normally.
* Write-backs may be queued before the upgrade and only arrive afterwards; they
  are re-checked against the live label/mapping state when delivered.

Callers learn the outcome from an explicit :class:`AccessStatus` carried by
:class:`ReadResult` / :class:`WriteResult` rather than from exceptions for the
policy decision itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .labels import Clearance, Label

__all__ = [
    "AccessStatus",
    "AccessControlError",
    "UnknownProcessError",
    "UnknownObjectError",
    "UnknownHandleError",
    "ForeignHandleError",
    "ReadResult",
    "WriteResult",
    "MemoryObject",
    "Mapping",
    "Handle",
    "Process",
    "AccessMonitor",
]


class AccessStatus(str, Enum):
    """Outcome of a monitored operation."""

    READ_OK = "read_ok"
    """The read was accepted and the process label did not change."""

    READ_OK_UPGRADED = "read_ok_upgraded"
    """The read was accepted and raised the process label (revocations ran)."""

    READ_DENIED_CLEARANCE = "read_denied_clearance"
    """The merged label would exceed the process clearance; nothing changed."""

    MAPPING_REVOKED = "mapping_revoked"
    """The write-back was refused because its mapping was revoked."""

    WRITE_OK = "write_ok"
    """The write-back was applied and the target now holds the new content."""


class AccessControlError(Exception):
    """Base class for API usage errors (distinct from a policy denial)."""


class UnknownProcessError(AccessControlError):
    pass


class UnknownObjectError(AccessControlError):
    pass


class UnknownHandleError(AccessControlError):
    pass


class ForeignHandleError(AccessControlError):
    """Raised when a handle is presented by a process that does not own it."""


@dataclass(frozen=True)
class ReadResult:
    status: AccessStatus
    process_id: str
    object_id: Optional[str]
    content: Optional[bytes]
    old_label: Label
    new_label: Label
    revoked_handles: tuple[str, ...] = ()
    reason: Optional[str] = None

    @property
    def accepted(self) -> bool:
        return self.status in (AccessStatus.READ_OK, AccessStatus.READ_OK_UPGRADED)

    @property
    def denied(self) -> bool:
        return self.status is AccessStatus.READ_DENIED_CLEARANCE

    @property
    def upgraded(self) -> bool:
        return self.status is AccessStatus.READ_OK_UPGRADED


@dataclass(frozen=True)
class WriteResult:
    status: AccessStatus
    process_id: str
    object_id: Optional[str]
    handle_id: str
    content: bytes
    reason: Optional[str] = None

    @property
    def applied(self) -> bool:
        return self.status is AccessStatus.WRITE_OK

    @property
    def refused(self) -> bool:
        return self.status is AccessStatus.MAPPING_REVOKED


class MemoryObject:
    """A shared memory object carrying a sensitivity label and content."""

    def __init__(self, object_id: str, label: Label, content: bytes = b"") -> None:
        if not isinstance(label, Label):
            raise TypeError("label must be a Label")
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError("content must be bytes")
        self.object_id = object_id
        self.label = label
        self._content = bytes(content)

    @property
    def content(self) -> bytes:
        return self._content

    def __repr__(self) -> str:
        return f"MemoryObject(id={self.object_id!r}, label={self.label})"


class Mapping:
    """A shared mapping from a process onto a memory object.

    ``writable`` mappings are the only channels that can write back.  Revocation
    is a property of the mapping itself, so every :class:`Handle` duplicated
    from it observes the same revoked state.
    """

    def __init__(
        self,
        mapping_id: str,
        process_id: str,
        obj: MemoryObject,
        writable: bool,
    ) -> None:
        self.mapping_id = mapping_id
        self.process_id = process_id
        self.obj = obj
        self.writable = writable
        self.revoked = False
        self.revoke_reason: Optional[str] = None
        self._handles: dict[str, "Handle"] = {}

    @property
    def object_id(self) -> str:
        return self.obj.object_id

    def attach(self, handle: "Handle") -> None:
        self._handles[handle.handle_id] = handle

    @property
    def handles(self) -> tuple["Handle", ...]:
        return tuple(self._handles.values())

    def revoke(self, reason: str) -> None:
        self.revoked = True
        self.revoke_reason = reason

    def satisfied_by(self, process_label: Label) -> bool:
        """A writable mapping stays valid while its target dominates the label."""
        return self.obj.label.dominates(process_label)

    def __repr__(self) -> str:
        mode = "rw" if self.writable else "ro"
        state = "REVOKED" if self.revoked else "active"
        return f"Mapping({self.mapping_id}, {self.object_id}, {mode}, {state})"


class Handle:
    """A user-visible handle onto a :class:`Mapping`; duplicates share state."""

    def __init__(self, handle_id: str, mapping: Mapping) -> None:
        self.handle_id = handle_id
        self._mapping = mapping
        mapping.attach(self)

    @property
    def mapping_id(self) -> str:
        return self._mapping.mapping_id

    @property
    def object_id(self) -> str:
        return self._mapping.object_id

    @property
    def writable(self) -> bool:
        return self._mapping.writable

    @property
    def revoked(self) -> bool:
        return self._mapping.revoked

    @property
    def revoke_reason(self) -> Optional[str]:
        return self._mapping.revoke_reason

    def __repr__(self) -> str:
        return f"Handle({self.handle_id!r}, mapping={self.mapping_id!r}, revoked={self.revoked})"


class Process:
    """A subject with a current label and a fixed clearance upper bound."""

    def __init__(
        self,
        process_id: str,
        label: Label,
        clearance: Clearance,
    ) -> None:
        if not isinstance(label, Label):
            raise TypeError("label must be a Label")
        if not isinstance(clearance, Clearance):
            raise TypeError("clearance must be a Clearance")
        if not clearance.permits(label):
            raise ValueError("initial label must be within the process clearance")
        self.process_id = process_id
        self.label = label
        self.clearance = clearance
        self.mappings: dict[str, Mapping] = {}

    @property
    def active_mappings(self) -> tuple[Mapping, ...]:
        return tuple(m for m in self.mappings.values() if not m.revoked)

    @property
    def writable_mappings(self) -> tuple[Mapping, ...]:
        return tuple(
            m for m in self.mappings.values() if m.writable and not m.revoked
        )

    def __repr__(self) -> str:
        return f"Process({self.process_id!r}, label={self.label})"


@dataclass
class _PendingWrite:
    sequence: int
    process_id: str
    handle_id: str
    content: bytes


class AccessMonitor:
    """Stateful reference monitor coordinating processes, objects and maps."""

    def __init__(self) -> None:
        self._processes: dict[str, Process] = {}
        self._objects: dict[str, MemoryObject] = {}
        self._handles: dict[str, Handle] = {}
        self._pending: list[_PendingWrite] = []
        self._mapping_seq = 0
        self._handle_seq = 0
        self._write_seq = 0

    # ------------------------------------------------------------------ setup

    def register_process(
        self,
        process_id: str,
        label: Label,
        clearance: Clearance,
    ) -> Process:
        if process_id in self._processes:
            raise ValueError(f"process {process_id!r} already registered")
        proc = Process(process_id, label, clearance)
        self._processes[process_id] = proc
        return proc

    def create_object(
        self,
        object_id: str,
        label: Label,
        content: bytes = b"",
    ) -> MemoryObject:
        if object_id in self._objects:
            raise ValueError(f"object {object_id!r} already exists")
        obj = MemoryObject(object_id, label, content)
        self._objects[object_id] = obj
        return obj

    def get_process(self, process_id: str) -> Process:
        try:
            return self._processes[process_id]
        except KeyError:
            raise UnknownProcessError(f"unknown process {process_id!r}") from None

    def get_object(self, object_id: str) -> MemoryObject:
        try:
            return self._objects[object_id]
        except KeyError:
            raise UnknownObjectError(f"unknown object {object_id!r}") from None

    def get_handle(self, handle_id: str) -> Handle:
        try:
            return self._handles[handle_id]
        except KeyError:
            raise UnknownHandleError(f"unknown handle {handle_id!r}") from None

    # --------------------------------------------------------------- mappings

    def open_mapping(
        self,
        process_id: str,
        object_id: str,
        *,
        writable: bool,
    ) -> Handle:
        """Open a shared mapping.

        Opening requires the object label to dominate the process label, i.e. the
        process may currently read (and, for writable maps, write back) the
        object.  A writable map opened while permitted can be revoked later when
        an upgrade invalidates it.
        """
        proc = self.get_process(process_id)
        obj = self.get_object(object_id)
        if not obj.label.dominates(proc.label):
            raise AccessControlError(
                f"object {object_id!r} label {obj.label} does not cover process "
                f"label {proc.label}; mapping refused"
            )
        self._mapping_seq += 1
        mapping = Mapping(
            mapping_id=f"map-{self._mapping_seq}",
            process_id=process_id,
            obj=obj,
            writable=writable,
        )
        proc.mappings[mapping.mapping_id] = mapping
        return self._mint_handle(mapping)

    def duplicate_handle(self, handle_id: str) -> Handle:
        """Duplicate a handle; the copy shares the mapping's revocation state."""
        original = self.get_handle(handle_id)
        return self._mint_handle(original._mapping)

    def _mint_handle(self, mapping: Mapping) -> Handle:
        self._handle_seq += 1
        handle = Handle(f"h-{self._handle_seq}", mapping)
        self._handles[handle.handle_id] = handle
        return handle

    def is_handle_revoked(self, handle_id: str) -> bool:
        return self.get_handle(handle_id).revoked

    def mapping_status(self, handle_id: str) -> AccessStatus:
        """Convenience: active writable handle vs. revoked handle."""
        handle = self.get_handle(handle_id)
        if handle.revoked:
            return AccessStatus.MAPPING_REVOKED
        return AccessStatus.WRITE_OK

    # ------------------------------------------------------------------- read

    def read(self, process_id: str, object_id: str) -> ReadResult:
        """Attempt a read, applying the upgrade-and-revoke policy atomically."""
        proc = self.get_process(process_id)
        obj = self.get_object(object_id)

        old_label = proc.label
        merged = old_label.merge(obj.label)

        if not proc.clearance.permits(merged):
            # Whole read fails: no content, no label change, no revocation.
            return ReadResult(
                status=AccessStatus.READ_DENIED_CLEARANCE,
                process_id=process_id,
                object_id=object_id,
                content=None,
                old_label=old_label,
                new_label=old_label,
                revoked_handles=(),
                reason=(
                    f"merged label {merged} exceeds clearance "
                    f"{proc.clearance.bound}"
                ),
            )

        proc.label = merged
        revoked: tuple[str, ...] = ()
        if merged != old_label:
            revoked = self._revoke_unsatisfied(proc, merged)
            status = AccessStatus.READ_OK_UPGRADED
        else:
            status = AccessStatus.READ_OK

        return ReadResult(
            status=status,
            process_id=process_id,
            object_id=object_id,
            content=obj.content,
            old_label=old_label,
            new_label=merged,
            revoked_handles=revoked,
        )

    def _revoke_unsatisfied(
        self, proc: Process, new_label: Label
    ) -> tuple[str, ...]:
        """Revoke writable shared maps whose target no longer dominates label."""
        revoked_handles: list[str] = []
        for mapping in list(proc.mappings.values()):
            if mapping.revoked or not mapping.writable:
                continue
            if not mapping.satisfied_by(new_label):
                reason = (
                    f"target label {mapping.obj.label} no longer dominates "
                    f"upgraded process label {new_label}"
                )
                mapping.revoke(reason)
                revoked_handles.extend(h.handle_id for h in mapping.handles)
        return tuple(revoked_handles)

    # ------------------------------------------------------------------ write

    def write_back(
        self,
        process_id: str,
        handle_id: str,
        content: bytes,
    ) -> WriteResult:
        """Deliver a write-back immediately, re-checking the live policy state."""
        proc = self.get_process(process_id)
        handle = self.get_handle(handle_id)
        mapping = self._require_owned_mapping(proc, handle)

        if not isinstance(content, (bytes, bytearray)):
            raise TypeError("content must be bytes")
        payload = bytes(content)

        if not mapping.writable:
            # Usage error, not a policy decision: read-only maps never change
            # state and can never accept write-backs.
            raise AccessControlError(
                f"mapping {mapping.mapping_id} is read-only"
            )

        refusal = self._revocation_reason(proc, mapping)
        if refusal is not None:
            return WriteResult(
                status=AccessStatus.MAPPING_REVOKED,
                process_id=process_id,
                object_id=mapping.object_id,
                handle_id=handle_id,
                content=payload,
                reason=refusal,
            )

        mapping.obj._content = payload
        return WriteResult(
            status=AccessStatus.WRITE_OK,
            process_id=process_id,
            object_id=mapping.object_id,
            handle_id=handle_id,
            content=payload,
        )

    def _require_owned_mapping(self, proc: Process, handle: Handle) -> Mapping:
        mapping = handle._mapping
        if mapping.process_id != proc.process_id:
            raise ForeignHandleError(
                f"handle {handle.handle_id!r} belongs to process "
                f"{mapping.process_id!r}, not {proc.process_id!r}"
            )
        if mapping.mapping_id not in proc.mappings:
            raise UnknownHandleError(
                f"handle {handle.handle_id!r} is not attached to process "
                f"{proc.process_id!r}"
            )
        return mapping

    def _revocation_reason(self, proc: Process, mapping: Mapping) -> Optional[str]:
        """Return a refusal reason, or ``None`` when the write may proceed."""
        if mapping.revoked:
            return mapping.revoke_reason or "mapping was revoked"
        if not mapping.satisfied_by(proc.label):
            # Defensive re-check: label moved without this mapping being revoked
            # should not happen, but never allow an unsatisfied write through.
            return (
                f"target label {mapping.obj.label} does not dominate current "
                f"process label {proc.label}"
            )
        return None

    # --------------------------------------------------------- queued writes

    def queue_write_back(
        self,
        process_id: str,
        handle_id: str,
        content: bytes,
    ) -> int:
        """Queue a write-back for later delivery; returns its queue sequence."""
        proc = self.get_process(process_id)
        handle = self.get_handle(handle_id)
        mapping = self._require_owned_mapping(proc, handle)
        if not mapping.writable:
            raise AccessControlError(
                f"mapping {mapping.mapping_id} is read-only"
            )
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError("content must be bytes")
        self._write_seq += 1
        self._pending.append(
            _PendingWrite(
                sequence=self._write_seq,
                process_id=process_id,
                handle_id=handle_id,
                content=bytes(content),
            )
        )
        return self._write_seq

    def flush_pending_writes(self) -> list[WriteResult]:
        """Deliver every queued write-back in FIFO order with live re-checks."""
        results: list[WriteResult] = []
        pending, self._pending = self._pending, []
        for item in pending:
            results.append(
                self.write_back(item.process_id, item.handle_id, item.content)
            )
        return results

    def pending_writes(self) -> tuple[tuple[int, str, str, bytes], ...]:
        """Inspect queued, not-yet-delivered writes (sequence, proc, handle, data)."""
        return tuple(
            (p.sequence, p.process_id, p.handle_id, p.content) for p in self._pending
        )
