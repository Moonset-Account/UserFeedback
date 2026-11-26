"""A small interval-based simulated GPU memory pool."""

from __future__ import annotations

from dataclasses import dataclass


def align_up(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


@dataclass
class Block:
    """An allocated interval in the simulated address space."""

    owner_id: str
    offset: int
    size: int
    alignment: int


@dataclass
class _Segment:
    start: int
    size: int
    free: bool
    owner_id: str | None = None

    @property
    def end(self) -> int:
        return self.start + self.size


class MemoryPool:
    """First-fit interval allocator with coalescing and inspection."""

    def __init__(self, capacity: int):
        if not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        self.capacity = capacity
        self._segments: list[_Segment] = [_Segment(0, capacity, True)]
        self._blocks: dict[str, Block] = {}

    @property
    def used_bytes(self) -> int:
        return sum(seg.size for seg in self._segments if not seg.free)

    @property
    def free_bytes(self) -> int:
        return sum(seg.size for seg in self._segments if seg.free)

    def get_block(self, owner_id: str) -> Block | None:
        return self._blocks.get(owner_id)

    def find_placement(self, size: int, alignment: int) -> int | None:
        if size <= 0 or alignment <= 0:
            return None
        for seg in self._segments:
            if not seg.free:
                continue
            start = align_up(seg.start, alignment)
            if start + size <= seg.end:
                return start
        return None

    def can_allocate(self, size: int, alignment: int = 1) -> bool:
        return self.find_placement(size, alignment) is not None

    def largest_free_block(self, alignment: int = 1) -> int:
        best = 0
        for seg in self._segments:
            if not seg.free:
                continue
            start = align_up(seg.start, alignment)
            best = max(best, seg.end - start)
        return best

    def allocate(self, owner_id: str, size: int, alignment: int) -> Block:
        if owner_id in self._blocks:
            raise ValueError(f"block owner already allocated: {owner_id!r}")
        if size <= 0:
            raise ValueError("allocation size must be positive")
        if alignment <= 0:
            raise ValueError("alignment must be positive")
        offset = self.find_placement(size, alignment)
        if offset is None:
            raise MemoryError(
                f"cannot place {size} bytes aligned to {alignment}; "
                f"free={self.free_bytes}, largest={self.largest_free_block(alignment)}"
            )

        for index, seg in enumerate(self._segments):
            if not seg.free:
                continue
            start = align_up(seg.start, alignment)
            if start + size > seg.end:
                continue

            replacement: list[_Segment] = []
            if start > seg.start:
                replacement.append(_Segment(seg.start, start - seg.start, True))
            replacement.append(_Segment(start, size, False, owner_id))
            if start + size < seg.end:
                replacement.append(_Segment(start + size, seg.end - (start + size), True))
            self._segments[index : index + 1] = replacement
            block = Block(owner_id, start, size, alignment)
            self._blocks[owner_id] = block
            return block

        raise AssertionError("placement disappeared after find_placement")  # pragma: no cover

    def free(self, owner_id: str) -> Block:
        block = self._blocks.pop(owner_id, None)
        if block is None:
            raise KeyError(f"no allocated block for owner {owner_id!r}")
        for seg in self._segments:
            if not seg.free and seg.owner_id == owner_id:
                seg.free = True
                seg.owner_id = None
                break
        else:  # pragma: no cover - defensive invariant
            raise AssertionError(f"block map/segments disagree for {owner_id!r}")
        self._coalesce()
        return block

    def _coalesce(self) -> None:
        merged: list[_Segment] = []
        for seg in self._segments:
            if (
                merged
                and merged[-1].free
                and seg.free
                and merged[-1].end == seg.start
            ):
                merged[-1].size += seg.size
            else:
                merged.append(
                    _Segment(seg.start, seg.size, seg.free, seg.owner_id)
                )
        self._segments = merged

    def snapshot(self) -> dict[str, int | list[dict[str, int | str | bool | None]]]:
        return {
            "capacity": self.capacity,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "largest_free_block": self.largest_free_block(),
            "segments": [
                {
                    "start": seg.start,
                    "end": seg.end,
                    "size": seg.size,
                    "free": seg.free,
                    "owner_id": seg.owner_id,
                }
                for seg in self._segments
            ],
        }
