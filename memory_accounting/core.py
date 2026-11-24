"""Core memory-frame accounting primitives.

The important identity of a physical frame is ``(page-frame-number, reuse-id)``.
A bare PFN is not sufficient because the same PFN can be freed and handed to a
new generation of data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Hashable, Iterable, Mapping


class AccountingError(ValueError):
    """Base class for invalid observations or replay requests."""


class MappingError(AccountingError):
    """A resident observation is inconsistent with its mapping range."""


def scalar_id(value: Any) -> Hashable:
    """Normalize common JSON identifiers without conflating distinct values.

    Numeric strings are normalized to integers so that ``100`` and ``"100"``
    identify the same PFN when decoded by different receipts.  Other strings are
    retained verbatim.  Containers are converted recursively to hashable tuples.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            # Reject forms such as "1.0"; only integer identifiers are normalized.
            if text and (text.isdigit() or (text[0] in "+-" and text[1:].isdigit())):
                return int(text)
        except ValueError:  # pragma: no cover - isdigit guard makes this unlikely
            pass
        return text
    if isinstance(value, Mapping):
        return tuple(sorted((str(k), scalar_id(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(scalar_id(v) for v in value)
    if value is None:
        return 0
    return value


@dataclass(frozen=True, slots=True)
class FrameRef:
    """A reusable physical frame generation."""

    number: Hashable
    reuse_id: Hashable = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "number", scalar_id(self.number))
        object.__setattr__(self, "reuse_id", scalar_id(self.reuse_id))

    @classmethod
    def coerce(
        cls,
        value: Any = None,
        *,
        pfn: Any = None,
        number: Any = None,
        frame_number: Any = None,
        reuse_id: Any = None,
        reuse: Any = None,
        generation: Any = None,
    ) -> "FrameRef":
        """Coerce a frame reference from objects, tuples, dicts, or keywords."""

        explicit_number = pfn if pfn is not None else number
        explicit_number = (
            explicit_number if explicit_number is not None else frame_number
        )
        explicit_reuse = reuse_id if reuse_id is not None else reuse
        explicit_reuse = (
            explicit_reuse if explicit_reuse is not None else generation
        )

        if isinstance(value, FrameRef):
            if explicit_number is None and explicit_reuse is None:
                return value
            return cls(
                explicit_number if explicit_number is not None else value.number,
                explicit_reuse if explicit_reuse is not None else value.reuse_id,
            )

        if explicit_number is not None:
            return cls(explicit_number, 0 if explicit_reuse is None else explicit_reuse)

        if value is None:
            raise AccountingError("a page frame number is required")

        if isinstance(value, Mapping):
            num = _first_present(
                value,
                "pfn",
                "page_frame_number",
                "frame_number",
                "number",
                "frame",
                "id",
            )
            if num is None:
                raise AccountingError(f"frame reference is missing a number: {value!r}")
            gen = _first_present(
                value,
                "reuse_id",
                "reuse_number",
                "reuse",
                "generation",
                "generation_id",
                "epoch",
            )
            return cls(num, 0 if gen is None else gen)

        if isinstance(value, (list, tuple)):
            if len(value) == 1:
                return cls(value[0])
            if len(value) >= 2:
                return cls(value[0], value[1])

        if isinstance(value, int):
            return cls(value)

        if isinstance(value, str):
            text = value.strip()
            for separator in ("#", "@", ":"):
                if separator in text:
                    left, right = text.split(separator, 1)
                    return cls(left, right)
            return cls(text)

        return cls(value)

    @property
    def key(self) -> "FrameRef":
        return self

    def to_dict(self) -> dict[str, Any]:
        return {"pfn": self.number, "reuse_id": self.reuse_id}


def _first_present(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


@dataclass(frozen=True, slots=True)
class MappingRange:
    """A process mapping.  Addresses are byte addresses and ``end`` is exclusive."""

    pid: Hashable
    mapping_id: str
    start: int = 0
    end: int = 0
    page_size: int = 4096

    def __post_init__(self) -> None:
        if self.page_size <= 0:
            raise AccountingError("page_size must be positive")
        if self.end < self.start:
            raise AccountingError(
                f"mapping {self.mapping_id!r} has end {self.end} before start {self.start}"
            )

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any], *, pid: Hashable | None = None, page_size: int = 4096
    ) -> "MappingRange":
        mapping_pid = scalar_id(
            pid if pid is not None else _first_present(data, "pid", "process", "process_id")
        )
        mapping_id = str(
            _first_present(data, "mapping_id", "id", "mapping", "name", "vma")
            or f"mapping-{mapping_pid}-{_first_present(data, 'start', 'start_addr', 'base') or 0}"
        )
        size = int(_first_present(data, "page_size") or page_size)

        unit = str(data.get("unit", "bytes")).lower()
        start = _first_present(data, "start", "start_addr", "base", "begin")
        end = _first_present(data, "end", "end_addr", "stop", "finish")
        length = _first_present(data, "length", "size", "length_bytes", "span")

        first_page = _first_present(data, "first_page", "start_page", "first_vpn")
        page_count = _first_present(data, "page_count", "pages", "nr_pages")
        if first_page is not None:
            start = int(first_page) * size
            if page_count is not None:
                end = start + int(page_count) * size
            elif end is not None:
                # Accepted as an exclusive page index when start_page is used.
                end = int(end) * size
            else:
                end = start + size
        else:
            start = 0 if start is None else int(start)
            if unit in {"page", "pages"}:
                start *= size
                if end is not None:
                    end = int(end) * size
                elif length is not None:
                    length = int(length) * size
            if end is None:
                if length is None:
                    # A zero-length range is still useful as a declared identifier.
                    end = start
                else:
                    end = start + int(length)
            else:
                end = int(end)
            if length is not None and end is None:  # pragma: no cover - handled above
                end = start + int(length)

        return cls(mapping_pid, mapping_id, int(start), int(end), size)

    def contains(self, vaddr: int) -> bool:
        return self.start <= int(vaddr) < self.end

    def validate_address(self, vaddr: int | None) -> int | None:
        if vaddr is None:
            return None
        vaddr = int(vaddr)
        if not self.contains(vaddr):
            raise MappingError(
                f"address {vaddr} is outside mapping {self.mapping_id!r} "
                f"[{self.start}, {self.end}) for pid {self.pid!r}"
            )
        return (vaddr // self.page_size) * self.page_size


EdgeKey = tuple[Hashable, str, Hashable]


@dataclass(frozen=True, slots=True)
class FrameShare:
    """Holder information for one physical frame generation."""

    frame: FrameRef
    holders: tuple[Hashable, ...]
    occurrence_count: int

    @property
    def holder_count(self) -> int:
        return len(self.holders)

    @property
    def exclusive(self) -> bool:
        return self.holder_count == 1

    @property
    def each_share_pages(self) -> Fraction:
        return Fraction(1, self.holder_count) if self.holder_count else Fraction(0, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.frame.to_dict(),
            "holders": [str(p) for p in self.holders],
            "holder_count": self.holder_count,
            "occurrence_count": self.occurrence_count,
            "exclusive": self.exclusive,
            "share_pages_per_holder": fraction_to_dict(self.each_share_pages),
        }


@dataclass(frozen=True, slots=True)
class ProcessReport:
    pid: Hashable
    resident_frame_count: int
    resident_bytes: int
    mapping_occurrence_count: int
    mapping_occurrence_bytes: int
    exclusive_frame_count: int
    exclusive_bytes: int
    proportional_pages: Fraction
    proportional_bytes: Fraction
    share_of_physical: Fraction
    exclusive_frames: tuple[FrameRef, ...] = ()
    shared_frames: tuple[FrameRef, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": str(self.pid),
            "resident_frame_count": self.resident_frame_count,
            "resident_bytes": self.resident_bytes,
            "mapping_occurrence_count": self.mapping_occurrence_count,
            "mapping_occurrence_bytes": self.mapping_occurrence_bytes,
            "exclusive_frame_count": self.exclusive_frame_count,
            "exclusive_bytes": self.exclusive_bytes,
            "proportional_pages": fraction_to_dict(self.proportional_pages),
            "proportional_bytes": fraction_to_dict(self.proportional_bytes),
            "share_of_physical": fraction_to_dict(self.share_of_physical),
            "exclusive_frames": [f.to_dict() for f in self.exclusive_frames],
            "shared_frames": [f.to_dict() for f in self.shared_frames],
        }


@dataclass(frozen=True, slots=True)
class AccountingReport:
    page_size: int
    physical_frame_count: int
    physical_resident_bytes: int
    processes: dict[Hashable, ProcessReport]
    frames: tuple[FrameShare, ...] = ()
    stale_removal_count: int = 0
    audit: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def total_proportional_bytes(self) -> Fraction:
        return sum(
            (p.proportional_bytes for p in self.processes.values()), Fraction(0, 1)
        )

    @property
    def process_rss_sum_bytes(self) -> int:
        """Sum of each process's within-process distinct resident frames."""

        return sum(p.resident_bytes for p in self.processes.values())

    @property
    def mapping_occurrence_sum_bytes(self) -> int:
        """Sum over mapped resident occurrences, including duplicate mappings."""

        return sum(p.mapping_occurrence_bytes for p in self.processes.values())

    @property
    def shared_frame_overcount_bytes(self) -> int:
        """Amount by which per-process RSS sum exceeds physical occupancy."""

        return max(0, self.process_rss_sum_bytes - self.physical_resident_bytes)

    @property
    def duplicate_mapping_overcount_bytes(self) -> int:
        """Extra occurrence count caused by repeated mappings inside processes."""

        return max(
            0,
            self.mapping_occurrence_sum_bytes - self.process_rss_sum_bytes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_size": self.page_size,
            "physical_frame_count": self.physical_frame_count,
            "physical_resident_bytes": self.physical_resident_bytes,
            "process_rss_sum_bytes": self.process_rss_sum_bytes,
            "mapping_occurrence_sum_bytes": self.mapping_occurrence_sum_bytes,
            "shared_frame_overcount_bytes": self.shared_frame_overcount_bytes,
            "duplicate_mapping_overcount_bytes": self.duplicate_mapping_overcount_bytes,
            "total_proportional_bytes": fraction_to_dict(self.total_proportional_bytes),
            "processes": {
                str(pid): report.to_dict() for pid, report in self.processes.items()
            },
            "frames": [frame.to_dict() for frame in self.frames],
            "stale_removal_count": self.stale_removal_count,
            "audit": list(self.audit),
        }


def fraction_to_dict(value: Fraction) -> dict[str, Any]:
    return {
        "numerator": value.numerator,
        "denominator": value.denominator,
        "exact": f"{value.numerator}/{value.denominator}",
        "float": float(value),
    }


class MemoryAccount:
    """Mutable holder graph and physical-frame account."""

    def __init__(self, *, page_size: int = 4096, strict_mappings: bool = False) -> None:
        if page_size <= 0:
            raise AccountingError("page_size must be positive")
        self.page_size = int(page_size)
        self.strict_mappings = strict_mappings
        self.mappings: dict[tuple[Hashable, str], MappingRange] = {}
        self.frames: dict[FrameRef, set[EdgeKey]] = {}
        self.audit: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Mapping declarations
    # ------------------------------------------------------------------
    def declare_mapping(
        self,
        pid: Hashable | Mapping[str, Any],
        mapping_id: str | None = None,
        start: int | None = None,
        end: int | None = None,
        length: int | None = None,
        mapping: Mapping[str, Any] | MappingRange | None = None,
    ) -> MappingRange:
        if isinstance(pid, MappingRange):
            item = pid
        elif isinstance(pid, Mapping):
            item = MappingRange.from_dict(pid, page_size=self.page_size)
        elif mapping is not None:
            if isinstance(mapping, MappingRange):
                item = mapping
            else:
                item = MappingRange.from_dict(
                    mapping, pid=pid, page_size=self.page_size
                )
        else:
            if mapping_id is None:
                raise AccountingError("mapping_id is required")
            pid_value = scalar_id(pid)
            if end is None and length is not None and start is not None:
                end = start + length
            item = MappingRange(
                pid_value,
                str(mapping_id),
                0 if start is None else int(start),
                0 if end is None else int(end),
                self.page_size,
            )
        self.mappings[(item.pid, item.mapping_id)] = item
        return item

    def remove_mapping(self, pid: Hashable, mapping_id: str) -> int:
        pid = scalar_id(pid)
        prefix: EdgeKey = (pid, str(mapping_id), "")
        removed = 0
        for ref, edges in list(self.frames.items()):
            doomed = {
                edge
                for edge in edges
                if edge[0] == prefix[0] and edge[1] == prefix[1]
            }
            if doomed:
                removed += len(doomed)
                edges.difference_update(doomed)
                if not edges:
                    del self.frames[ref]
        self.mappings.pop((pid, str(mapping_id)), None)
        self.audit.append(
            {"action": "remove_mapping", "pid": str(pid), "mapping_id": mapping_id,
             "removed_occurrences": removed}
        )
        return removed

    def _edge_key(
        self,
        pid: Hashable,
        mapping_id: str | None,
        vaddr: int | None,
        offset: int | None = None,
        slot: Hashable | None = None,
        known_mappings: Mapping[tuple[Hashable, str], MappingRange] | None = None,
    ) -> EdgeKey:
        pid = scalar_id(pid)
        mid = "" if mapping_id is None else str(mapping_id)
        mappings = self.mappings if known_mappings is None else known_mappings

        if slot is not None:
            return (pid, mid, scalar_id(slot))

        mapping = mappings.get((pid, mid)) if mid else None
        address = vaddr
        if address is None and offset is not None:
            if mapping is not None:
                address = mapping.start + int(offset)
            else:
                address = int(offset)

        if address is not None:
            address = int(address)
            if mapping is not None:
                mapping.validate_address(address)
            elif self.strict_mappings and mid:
                raise MappingError(f"mapping {mid!r} is not declared for pid {pid!r}")
            return (pid, mid, ("vpage", (address // self.page_size) * self.page_size))

        if mid:
            # One observation for a mapping without a virtual address represents
            # that mapping's single relevant resident page/slot.
            return (pid, mid, ("mapping", mid))
        return (pid, "", ("frame-only",))

    # ------------------------------------------------------------------
    # Resident frame mutations
    # ------------------------------------------------------------------
    def add_resident(
        self,
        pid: Hashable | Mapping[str, Any] | None = None,
        frame: FrameRef | Mapping[str, Any] | Iterable[Any] | int | str | None = None,
        *,
        pfn: Any = None,
        reuse_id: Any = None,
        mapping_id: str | None = None,
        vaddr: int | None = None,
        offset: int | None = None,
        slot: Hashable | None = None,
        **extra: Any,
    ) -> FrameRef:
        record: Mapping[str, Any] | None = pid if isinstance(pid, Mapping) else None
        if record is not None:
            rec = record
            pid_value = _first_present(rec, "pid", "process", "process_id")
            pfn_value = _first_present(
                rec, "pfn", "page_frame_number", "frame_number"
            )
            frame = FrameRef.coerce(rec.get("frame"), pfn=pfn_value)
            if frame.reuse_id == 0:
                gen = _first_present(
                    rec, "reuse_id", "reuse_number", "reuse", "generation"
                )
                if gen is not None:
                    frame = FrameRef(frame.number, gen)
            mapping_id = _first_present(rec, "mapping_id", "mapping", "vma", "id")
            vaddr = _first_present(rec, "vaddr", "virtual_address", "address", "va")
            offset = _first_present(rec, "offset", "page_offset")
            slot = _first_present(rec, "slot", "virtual_page", "vpn")
        else:
            if pid is None:
                pid_value = extra.get("pid")
            else:
                pid_value = pid
            frame = FrameRef.coerce(frame, pfn=pfn, reuse_id=reuse_id)

        if pid_value is None:
            raise AccountingError("pid is required for a resident observation")
        pid_value = scalar_id(pid_value)

        if mapping_id is not None:
            mapping_id = str(mapping_id)
            mapping = self.mappings.get((pid_value, mapping_id))
            if mapping is None and self.strict_mappings:
                raise MappingError(
                    f"mapping {mapping_id!r} is not declared for pid {pid_value!r}"
                )
            if mapping is not None and vaddr is not None:
                mapping.validate_address(int(vaddr))

        edge = self._edge_key(pid_value, mapping_id, vaddr, offset, slot)
        self.frames.setdefault(frame, set()).add(edge)
        return frame

    def remove_resident(
        self,
        pid: Hashable | Mapping[str, Any] | None = None,
        frame: FrameRef | Mapping[str, Any] | Iterable[Any] | int | str | None = None,
        *,
        pfn: Any = None,
        reuse_id: Any = None,
        mapping_id: str | None = None,
        vaddr: int | None = None,
        offset: int | None = None,
        slot: Hashable | None = None,
        **_extra: Any,
    ) -> int:
        record = pid if isinstance(pid, Mapping) else None
        if record is not None:
            rec = record
            pid_value = _first_present(rec, "pid", "process", "process_id")
            pfn_value = _first_present(
                rec, "pfn", "page_frame_number", "frame_number"
            )
            frame = FrameRef.coerce(rec.get("frame"), pfn=pfn_value)
            if frame.reuse_id == 0:
                gen = _first_present(
                    rec, "reuse_id", "reuse_number", "reuse", "generation"
                )
                if gen is not None:
                    frame = FrameRef(frame.number, gen)
            mapping_id = _first_present(rec, "mapping_id", "mapping", "vma", "id")
            vaddr = _first_present(rec, "vaddr", "virtual_address", "address", "va")
            offset = _first_present(rec, "offset", "page_offset")
            slot = _first_present(rec, "slot", "virtual_page", "vpn")
        else:
            pid_value = pid
            frame = FrameRef.coerce(frame, pfn=pfn, reuse_id=reuse_id)

        if pid_value is None:
            raise AccountingError("pid is required to remove a resident occurrence")
        pid_value = scalar_id(pid_value)
        mid = None if mapping_id is None else str(mapping_id)

        edges = self.frames.get(frame)
        if not edges:
            self._stale("remove_resident", frame, pid_value, mid, 0,
                        reason="unknown_frame_generation")
            return 0

        exact: EdgeKey | None = None
        if slot is not None or vaddr is not None or offset is not None:
            exact = self._edge_key(pid_value, mid, vaddr, offset, slot)
            doomed = {exact} if exact in edges else set()
        elif mid is not None:
            doomed = {e for e in edges if e[0] == pid_value and e[1] == mid}
        else:
            doomed = {e for e in edges if e[0] == pid_value}

        if not doomed:
            self._stale("remove_resident", frame, pid_value, mid, 0,
                        reason="no_matching_holder")
            return 0

        edges.difference_update(doomed)
        if not edges:
            del self.frames[frame]
        self.audit.append(
            {
                "action": "remove_resident",
                "pfn": frame.number,
                "reuse_id": frame.reuse_id,
                "pid": str(pid_value),
                "mapping_id": mid,
                "removed_occurrences": len(doomed),
            }
        )
        return len(doomed)

    def revoke_frame(
        self,
        frame: FrameRef | Mapping[str, Any] | Iterable[Any] | int | str | None = None,
        *,
        pfn: Any = None,
        reuse_id: Any = None,
        reason: str = "revoke_frame",
    ) -> int:
        ref = FrameRef.coerce(frame, pfn=pfn, reuse_id=reuse_id)
        edges = self.frames.pop(ref, None)
        count = 0 if edges is None else len(edges)
        if edges is None:
            self._stale(reason, ref, None, None, 0, reason="old_generation_not_active")
        else:
            self.audit.append(
                {
                    "action": reason,
                    "pfn": ref.number,
                    "reuse_id": ref.reuse_id,
                    "removed_occurrences": count,
                }
            )
        return count

    def copy_on_write(
        self,
        pid: Hashable | Mapping[str, Any] | None = None,
        old_frame: FrameRef | Mapping[str, Any] | Iterable[Any] | int | str | None = None,
        new_frame: FrameRef | Mapping[str, Any] | Iterable[Any] | int | str | None = None,
        *,
        old_pfn: Any = None,
        old_reuse_id: Any = None,
        new_pfn: Any = None,
        new_reuse_id: Any = None,
        mapping_id: str | None = None,
        vaddr: int | None = None,
        offset: int | None = None,
        slot: Hashable | None = None,
    ) -> FrameRef:
        if isinstance(pid, Mapping):
            rec = pid
            pid_value = _first_present(rec, "pid", "process", "process_id")
            old_pfn_value = _first_present(
                rec, "old_pfn", "old_page_frame_number", "from_pfn"
            )
            new_pfn_value = _first_present(
                rec, "new_pfn", "new_page_frame_number", "to_pfn"
            )
            old_gen_value = _first_present(
                rec, "old_reuse_id", "old_reuse", "from_reuse_id", "from_generation"
            )
            new_gen_value = _first_present(
                rec, "new_reuse_id", "new_reuse", "to_reuse_id", "to_generation"
            )
            old_frame = FrameRef.coerce(
                rec.get("old_frame") or rec.get("from_frame"),
                pfn=old_pfn_value,
                reuse_id=old_gen_value,
            )
            new_frame = FrameRef.coerce(
                rec.get("new_frame") or rec.get("to_frame"),
                pfn=new_pfn_value,
                reuse_id=new_gen_value,
            )
            mapping_id = _first_present(rec, "mapping_id", "mapping", "vma")
            vaddr = _first_present(rec, "vaddr", "virtual_address", "address", "va")
            offset = _first_present(rec, "offset")
            slot = _first_present(rec, "slot", "virtual_page", "vpn")
        else:
            pid_value = pid
            old_frame = FrameRef.coerce(old_frame, pfn=old_pfn, reuse_id=old_reuse_id)
            new_frame = FrameRef.coerce(new_frame, pfn=new_pfn, reuse_id=new_reuse_id)

        # Move one exact mapped virtual page.  If no address/slot is supplied,
        # remove the process's frame-level claim.  Duplicate mappings can therefore
        # be handled explicitly by passing mapping_id/vaddr.
        self.remove_resident(
            pid_value,
            old_frame,
            mapping_id=mapping_id,
            vaddr=vaddr,
            offset=offset,
            slot=slot,
        )
        self.add_resident(
            pid_value,
            new_frame,
            mapping_id=mapping_id,
            vaddr=vaddr,
            offset=offset,
            slot=slot,
        )
        self.audit.append(
            {
                "action": "copy_on_write",
                "pid": str(scalar_id(pid_value)),
                "mapping_id": None if mapping_id is None else str(mapping_id),
                "old_pfn": old_frame.number,
                "old_reuse_id": old_frame.reuse_id,
                "new_pfn": new_frame.number,
                "new_reuse_id": new_frame.reuse_id,
            }
        )
        return new_frame

    def _stale(
        self,
        action: str,
        frame: FrameRef,
        pid: Hashable | None,
        mapping_id: str | None,
        removed: int,
        *,
        reason: str,
    ) -> None:
        self.audit.append(
            {
                "action": action,
                "stale": True,
                "reason": reason,
                "pfn": frame.number,
                "reuse_id": frame.reuse_id,
                "pid": None if pid is None else str(pid),
                "mapping_id": mapping_id,
                "removed_occurrences": removed,
            }
        )

    # ------------------------------------------------------------------
    # Snapshots and reporting
    # ------------------------------------------------------------------
    def snapshot_process(
        self,
        pid: Hashable | Mapping[str, Any],
        mappings: Iterable[Mapping[str, Any] | MappingRange] | None = None,
        pages: Iterable[Mapping[str, Any]] | None = None,
    ) -> None:
        if isinstance(pid, Mapping):
            rec = pid
            pid_value = _first_present(rec, "pid", "process", "process_id")
            mappings = _first_present(rec, "mappings", "maps", "vmas") or []
            pages = _first_present(
                rec, "pages", "resident_pages", "resident_frames", "frames"
            ) or []
        else:
            pid_value = pid

        if pid_value is None:
            raise AccountingError("snapshot requires a pid")
        pid_value = scalar_id(pid_value)

        desired_mappings: dict[tuple[Hashable, str], MappingRange] = {}
        for item in mappings or []:
            mapping = (
                item
                if isinstance(item, MappingRange)
                else MappingRange.from_dict(item, pid=pid_value, page_size=self.page_size)
            )
            desired_mappings[(pid_value, mapping.mapping_id)] = mapping

        desired_edges: set[EdgeKey] = set()
        desired_adds: list[tuple[FrameRef, EdgeKey]] = []
        for page in pages or []:
            if not isinstance(page, Mapping):
                raise AccountingError(f"page observation must be an object: {page!r}")
            frame = FrameRef.coerce(
                page.get("frame"),
                pfn=page.get("pfn") or page.get("page_frame_number"),
                reuse_id=_first_present(page, "reuse_id", "reuse", "generation"),
            )
            mid = _first_present(page, "mapping_id", "mapping", "vma")
            vaddr = _first_present(page, "vaddr", "virtual_address", "address", "va")
            offset = _first_present(page, "offset", "page_offset")
            slot = _first_present(page, "slot", "virtual_page", "vpn")
            edge = self._edge_key(
                pid_value,
                mid,
                vaddr,
                offset,
                slot,
                known_mappings=desired_mappings,
            )
            desired_edges.add(edge)
            desired_adds.append((frame, edge))

        # Reconcile only this process's claims; other holder processes remain.
        for ref, edges in list(self.frames.items()):
            obsolete = {
                edge
                for edge in edges
                if edge[0] == pid_value and edge not in desired_edges
            }
            if obsolete:
                edges.difference_update(obsolete)
                if not edges:
                    del self.frames[ref]

        for key in list(self.mappings):
            if key[0] == pid_value and key not in desired_mappings:
                del self.mappings[key]
        self.mappings.update(desired_mappings)

        for frame, edge in desired_adds:
            self.frames.setdefault(frame, set()).add(edge)

        self.audit.append(
            {
                "action": "snapshot_process",
                "pid": str(pid_value),
                "mappings": len(desired_mappings),
                "resident_occurrences": len(desired_edges),
            }
        )

    def report(self) -> AccountingReport:
        frame_shares: list[FrameShare] = []
        process_frames: dict[Hashable, set[FrameRef]] = {}
        process_occurrences: dict[Hashable, int] = {}

        for ref in sorted(self.frames.keys(), key=_sort_frame):
            edges = self.frames[ref]
            holders = tuple(sorted({edge[0] for edge in edges}, key=str))
            share = FrameShare(
                frame=ref,
                holders=holders,
                occurrence_count=len(edges),
            )
            frame_shares.append(share)
            for pid in holders:
                process_frames.setdefault(pid, set()).add(ref)
            for edge in edges:
                process_occurrences[edge[0]] = (
                    process_occurrences.get(edge[0], 0) + 1
                )

        process_reports: dict[Hashable, ProcessReport] = {}
        total_frames = len(frame_shares)
        for pid in sorted(process_frames.keys(), key=str):
            refs = process_frames[pid]
            exclusive = tuple(
                sorted(
                    (
                        ref
                        for ref in refs
                        if {edge[0] for edge in self.frames[ref]} == {pid}
                    ),
                    key=_sort_frame,
                )
            )
            shared = tuple(sorted(refs.difference(exclusive), key=_sort_frame))
            proportional_pages = Fraction(0, 1)
            for ref in refs:
                holder_count = len({edge[0] for edge in self.frames[ref]})
                proportional_pages += Fraction(1, holder_count)

            prop_bytes = proportional_pages * self.page_size
            share_total = (
                proportional_pages / total_frames
                if total_frames
                else Fraction(0, 1)
            )
            occurrences = process_occurrences.get(pid, 0)
            process_reports[pid] = ProcessReport(
                pid=pid,
                resident_frame_count=len(refs),
                resident_bytes=len(refs) * self.page_size,
                mapping_occurrence_count=occurrences,
                mapping_occurrence_bytes=occurrences * self.page_size,
                exclusive_frame_count=len(exclusive),
                exclusive_bytes=len(exclusive) * self.page_size,
                proportional_pages=proportional_pages,
                proportional_bytes=prop_bytes,
                share_of_physical=share_total,
                exclusive_frames=exclusive,
                shared_frames=shared,
            )

        stale_count = sum(1 for item in self.audit if item.get("stale"))
        return AccountingReport(
            page_size=self.page_size,
            physical_frame_count=total_frames,
            physical_resident_bytes=total_frames * self.page_size,
            processes=process_reports,
            frames=tuple(frame_shares),
            stale_removal_count=stale_count,
            audit=tuple(self.audit),
        )


def _sort_frame(frame: FrameRef) -> tuple[str, str]:
    return (str(frame.number), str(frame.reuse_id))


def account_observations(
    processes: Iterable[Mapping[str, Any]] | Mapping[str, Any],
    *,
    page_size: int = 4096,
    strict_mappings: bool = False,
) -> AccountingReport:
    """Build an account directly from process observation objects."""

    account = MemoryAccount(page_size=page_size, strict_mappings=strict_mappings)
    if isinstance(processes, Mapping) and "processes" in processes:
        items = processes["processes"]
        if "page_size" in processes:
            account.page_size = int(processes["page_size"])
    elif isinstance(processes, Mapping):
        items = [processes]
    else:
        items = processes

    for process in items or []:
        account.snapshot_process(process)
    return account.report()
