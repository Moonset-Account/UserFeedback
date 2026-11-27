"""Sanitizing exporter for in-memory block devices.

Scenario: a test disk image is handed to a vendor.  Deleted-file remnants
sit in free sectors and volume snapshots share physical blocks.  Given a
logical volume's allocation map (extents), its valid byte count, sparse
regions and shared physical block references, this module produces exported
views with the following guarantees:

* Every approved view reads exactly its original allocated valid bytes;
  everything beyond the valid tail bound, in sparse regions and in
  unallocated holes reads as zeros.
* The same physical block may carry different valid tail bounds in two
  views; each view gets its own sanitized copy.
* Sanitized blocks are reused by their actual sanitized bytes (content
  addressed): identical sanitized content, including all-zero blocks,
  shares one exported physical block.
* Publishing is atomic.  An export first stages a complete new block store
  and a complete new set of block tables, then swaps them in with a single
  assignment.  If the run is interrupted, the previously published state
  is untouched, and a restart can only publish a full, consistent new
  block table -- never a partial one.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ExportInterrupted(Exception):
    """Raised when an export run is interrupted before commit."""


class InMemoryBlockDevice:
    """A trivial in-memory block device with fixed-size blocks."""

    def __init__(self, block_size: int):
        if block_size <= 0:
            raise ValueError("block_size must be positive")
        self.block_size = block_size
        self._blocks: dict[int, bytes] = {}

    def write_block(self, block_id: int, data: bytes) -> None:
        data = bytes(data)
        if len(data) != self.block_size:
            raise ValueError(
                f"block {block_id}: expected {self.block_size} bytes, "
                f"got {len(data)}"
            )
        self._blocks[block_id] = data

    def read_block(self, block_id: int) -> bytes:
        """Read a block; unwritten blocks read as zeros."""
        return self._blocks.get(block_id, bytes(self.block_size))

    def block_ids(self) -> set[int]:
        return set(self._blocks)


@dataclass(frozen=True)
class Extent:
    """Allocated run: logical blocks [logical_start, logical_start+count)

    are backed by physical blocks [physical_block, physical_block+count).
    """

    logical_start: int
    physical_block: int
    block_count: int


@dataclass(frozen=True)
class SparseRegion:
    """Logical range that reads as zeros and is not backed by storage."""

    logical_start: int
    block_count: int


@dataclass(frozen=True)
class VolumeView:
    """One approved export view of a logical volume.

    ``valid_bytes`` is the number of valid bytes counted from logical
    offset 0 across the whole view; bytes past it (including non-zero
    historical tails inside the last valid block) must read as zero.
    """

    name: str
    block_count: int
    valid_bytes: int
    extents: tuple[Extent, ...] = ()
    sparse: tuple[SparseRegion, ...] = ()


def _validate_view(view: VolumeView, block_size: int) -> None:
    if not view.name:
        raise ValueError("view name must be non-empty")
    if view.block_count < 0:
        raise ValueError(f"{view.name}: block_count must be >= 0")
    capacity = view.block_count * block_size
    if not 0 <= view.valid_bytes <= capacity:
        raise ValueError(
            f"{view.name}: valid_bytes {view.valid_bytes} outside "
            f"[0, {capacity}]"
        )
    intervals: list[tuple[int, int, str]] = []
    for ext in view.extents:
        if ext.block_count <= 0 or ext.logical_start < 0:
            raise ValueError(f"{view.name}: malformed extent {ext!r}")
        end = ext.logical_start + ext.block_count
        if end > view.block_count:
            raise ValueError(f"{view.name}: extent {ext!r} past end of view")
        intervals.append((ext.logical_start, end, "extent"))
    for sp in view.sparse:
        if sp.block_count <= 0 or sp.logical_start < 0:
            raise ValueError(f"{view.name}: malformed sparse region {sp!r}")
        end = sp.logical_start + sp.block_count
        if end > view.block_count:
            raise ValueError(
                f"{view.name}: sparse region {sp!r} past end of view"
            )
        intervals.append((sp.logical_start, end, "sparse"))
    intervals.sort()
    for (prev_start, prev_end, prev_kind), (start, end, kind) in zip(
        intervals, intervals[1:]
    ):
        if start < prev_end:
            raise ValueError(
                f"{view.name}: overlapping {prev_kind} "
                f"[{prev_start}, {prev_end}) and {kind} [{start}, {end})"
            )


class ExportSnapshot:
    """Immutable published export: sanitized block store + block tables."""

    def __init__(
        self,
        blocks: dict[int, bytes],
        tables: dict[str, dict[int, int]],
        view_blocks: dict[str, int],
        block_size: int,
    ):
        self._blocks = dict(blocks)
        self._tables = {name: dict(t) for name, t in tables.items()}
        self._view_blocks = dict(view_blocks)
        self.block_size = block_size

    # -- introspection -------------------------------------------------
    def view_names(self) -> tuple[str, ...]:
        return tuple(self._tables)

    def view_size(self, view: str) -> int:
        """Total logical size of a view in bytes."""
        return self._view_blocks[view] * self.block_size

    def block_table(self, view: str) -> dict[int, int]:
        """Copy of the logical -> exported-physical block table."""
        return dict(self._tables[view])

    def stored_blocks(self) -> set[int]:
        """All block ids present in the exported block store."""
        return set(self._blocks)

    def reachable_blocks(self, view: str | None = None) -> set[int]:
        """Block ids reachable from one view's table, or from all tables."""
        if view is not None:
            return set(self._tables[view].values())
        out: set[int] = set()
        for table in self._tables.values():
            out.update(table.values())
        return out

    def read_block(self, block_id: int) -> bytes:
        return self._blocks[block_id]

    # -- reading ---------------------------------------------------------
    def read(self, view: str, offset: int, length: int) -> bytes:
        """Read ``length`` bytes at ``offset`` from an exported view.

        Reads are clamped to the view's logical size.  Holes and sparse
        regions resolve to the shared zero block via the block table.
        """
        if offset < 0 or length < 0:
            raise ValueError("offset and length must be >= 0")
        table = self._tables[view]
        size = self._view_blocks[view] * self.block_size
        if length == 0 or offset >= size:
            return b""
        end = min(offset + length, size)
        bs = self.block_size
        out = bytearray()
        pos = offset
        while pos < end:
            lb = pos // bs
            within = pos - lb * bs
            take = min(bs - within, end - pos)
            chunk = self._blocks[table[lb]]
            out += chunk[within : within + take]
            pos += take
        return bytes(out)


class SanitizingExporter:
    """Builds sanitized, atomically published export snapshots."""

    def __init__(self, device: InMemoryBlockDevice):
        self._device = device
        bs = device.block_size
        self._snapshot = ExportSnapshot({}, {}, {}, bs)
        # Test/drill hook: after sanitizing this many logical blocks, raise
        # ExportInterrupted to simulate a crash mid-export.  None disables.
        self.interrupt_after: int | None = None

    @property
    def snapshot(self) -> ExportSnapshot:
        """The currently published snapshot (empty until first commit)."""
        return self._snapshot

    def export(self, views) -> ExportSnapshot:
        """Stage a full sanitized export and publish it atomically.

        On success returns and publishes the new snapshot.  On any failure
        (including ExportInterrupted) the previously published snapshot is
        left completely untouched.
        """
        views = tuple(views)
        names: set[str] = set()
        for view in views:
            _validate_view(view, self._device.block_size)
            if view.name in names:
                raise ValueError(f"duplicate view name {view.name!r}")
            names.add(view.name)

        bs = self._device.block_size
        zero = bytes(bs)
        staged_blocks: dict[int, bytes] = {}
        content_ids: dict[bytes, int] = {}
        tables: dict[str, dict[int, int]] = {}
        view_blocks: dict[str, int] = {}
        next_id = 0
        sanitized = 0

        for view in views:
            phys: dict[int, int] = {}
            for ext in view.extents:
                for i in range(ext.block_count):
                    phys[ext.logical_start + i] = ext.physical_block + i
            sparse_blocks: set[int] = set()
            for sp in view.sparse:
                sparse_blocks.update(
                    range(sp.logical_start, sp.logical_start + sp.block_count)
                )

            table: dict[int, int] = {}
            for lb in range(view.block_count):
                start = lb * bs
                if (
                    lb in sparse_blocks
                    or lb not in phys
                    or start >= view.valid_bytes
                ):
                    content = zero
                else:
                    tail = min(bs, view.valid_bytes - start)
                    raw = self._device.read_block(phys[lb])
                    # Keep the valid prefix, scrub the historical tail.
                    content = raw[:tail] + bytes(bs - tail)
                block_id = content_ids.get(content)
                if block_id is None:
                    block_id = next_id
                    next_id += 1
                    content_ids[content] = block_id
                    staged_blocks[block_id] = content
                table[lb] = block_id
                sanitized += 1
                if (
                    self.interrupt_after is not None
                    and sanitized >= self.interrupt_after
                ):
                    raise ExportInterrupted(
                        f"export interrupted after {sanitized} blocks; "
                        "staged state discarded, published snapshot unchanged"
                    )
            tables[view.name] = table
            view_blocks[view.name] = view.block_count

        # Single atomic commit: the published state is swapped only after
        # every view's full block table and every staged block exists.
        snapshot = ExportSnapshot(staged_blocks, tables, view_blocks, bs)
        self._snapshot = snapshot
        return snapshot
