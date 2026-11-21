# cow-snapshot-volume

Copy-on-write snapshot volumes in safe Rust. A volume is a writable byte
address space layered over an immutable snapshot; only blocks the volume has
actually touched are stored locally, and everything else is inherited from the
snapshot it was cloned from.

- **Toolchain:** Rust 1.95.0, standard library only.
- **Dependencies:** none (no third-party crates).
- **Build/test:** `cargo test --offline`

## Semantics

- Unallocated space reads as zero bytes. Reads may go past the logical length;
  those bytes are zeros.
- **Byte-granular I/O.** `read` / `write` work on arbitrary byte offsets and
  lengths. Writing part of a block materialises that block into the volume's
  overlay and patches it; the parent snapshot is never modified.
- **Range zeroing.** `zero_range(start, end)` may cross block boundaries and may
  start/end mid-block. Every block *fully* covered by the range is recorded as a
  whole-block **zero mark** (`BlockKind::ZeroMark`) that shadows ancestor data
  without allocating a data block. Partially covered blocks become local data
  with the covered bytes zeroed.
- **Aligned revert.** `revert_blocks(start, end)` takes a range whose ends are
  multiples of the block size and drops this volume's overlay for those blocks.
  They read again from the snapshot captured when the volume was created. Because
  snapshots are immutable, later writes to the parent volume never leak into an
  existing clone.
- **Snapshots at any version.** `Volume::snapshot()` freezes the current view as
  a new immutable `Snapshot`; `Snapshot::clone_volume()` opens a writable volume
  over it. Any snapshot in the tree can be cloned, so version trees of arbitrary
  depth (and sibling branches) are supported.

Blocks are addressed by `index = offset / block_size`. The default block size is
`DEFAULT_BLOCK_SIZE` (4096); use `Volume::with_block_size` (power of two) to
change it.

## Public API

```rust
pub const DEFAULT_BLOCK_SIZE: usize = 4096;

pub enum VolumeError {
    InvalidRange { start: u64, end: u64 },
    MisalignedRevert { start: u64, end: u64, block_size: usize },
    InvalidBlockSize(usize),
    OffsetOverflow { offset: u64, len: u64 },
}

pub enum BlockKind { Inherited, ZeroMark, Local }

pub struct Snapshot { /* immutable version */ }
impl Snapshot {
    pub fn block_size(&self) -> usize;
    pub fn len(&self) -> u64;
    pub fn is_empty(&self) -> bool;
    pub fn block_count(&self) -> u64;
    pub fn read(&self, offset: u64, len: usize) -> Result<Vec<u8>, VolumeError>;
    pub fn read_into(&self, offset: u64, buf: &mut [u8]) -> Result<(), VolumeError>;
    pub fn clone_volume(&self) -> Volume;
}

pub struct Volume { /* writable CoW view */ }
impl Volume {
    pub fn new() -> Self;                                  // default block size
    pub fn with_block_size(block_size: usize) -> Result<Self, VolumeError>;
    pub fn from_snapshot(snapshot: &Snapshot) -> Self;
    pub fn snapshot(&self) -> Snapshot;
    pub fn block_size(&self) -> usize;
    pub fn len(&self) -> u64;
    pub fn is_empty(&self) -> bool;
    pub fn block_count(&self) -> u64;
    pub fn block_kind(&self, index: u64) -> BlockKind;     // local overlay state
    pub fn read(&self, offset: u64, len: usize) -> Result<Vec<u8>, VolumeError>;
    pub fn read_into(&self, offset: u64, buf: &mut [u8]) -> Result<(), VolumeError>;
    pub fn write(&mut self, offset: u64, data: &[u8]) -> Result<(), VolumeError>;
    pub fn zero_range(&mut self, start: u64, end: u64) -> Result<(), VolumeError>;
    pub fn revert_blocks(&mut self, start: u64, end: u64) -> Result<(), VolumeError>;
}
```

`block_kind` reports what *this volume's own overlay* holds for a block:
`Local` (data written or partially zeroed here), `ZeroMark` (whole-block zero
tombstone), or `Inherited` (no local entry; resolves through the snapshot chain,
possibly to zeros).

## Reproduction

```sh
cargo test --offline
```

The test suite holds no pre-computed fixtures or canned demo output; every
assertion reads bytes back through the public API after performing real writes,
zeroing, reverts and snapshot/clone operations.

## Test coverage

Unit tests live in `src/lib.rs` (block-size validation, empty-volume reads).
Integration tests in `tests/volume.rs` cover three groups.

**Normal**
- default block size, zero-filled unallocated space, `block_count`/`len`;
- byte-granular writes spanning three blocks and a mid-block overwrite;
- child inherits parent bytes, only touched blocks become `Local`;
- `zero_range` crossing blocks hides ancestor data, full blocks become
  `ZeroMark`, partial blocks become `Local`;
- reverting aligned blocks re-exposes the creation snapshot;
- snapshot freezes the volume view against later writes;
- parent writes after a clone do not leak into the old clone;
- cloning from any version and diverging independently.

**Boundary**
- empty `write` / `zero_range` / `revert_blocks` are no-ops;
- misaligned revert is rejected without mutating the overlay;
- inverted `zero_range` is rejected;
- zeroing exactly on block edges marks every covered block;
- zero-then-revert returns to ancestor bytes;
- `u64` overflow in `read`/`write` is rejected and leaves the volume empty;
- `block_kind` past the logical length is `Inherited` and reads zeros.

**Interleaved**
- write → zero → write → revert on the same block in sequence;
- sibling branches cloned from one snapshot stay isolated;
- a six-version deep chain with a mid-chain revert, cloning from every version;
- writes past the inherited length extend correctly while untouched blocks stay
  inherited.

**Specification scenario** (`spec_abcd_parent_zero_middle_snapshot_then_revert`)
follows the prompt exactly on 4-byte blocks: parent holds `ABCD`; the child zeros
the middle two bytes (block becomes `Local`, reads `A\0\0D`); a grandchild is
snapshotted and cloned from the child (reads `A\0\0D`); the child then reverts its
block-0 overlay and reads `ABCD` again while the grandchild still reads `A\0\0D`;
the parent snapshot is unaffected.

## Verification status

`cargo test --offline` was executed on Rust 1.95.0: **24 tests passed, 0 failed**
(2 unit + 22 integration, plus 0 doc-tests). No tests are marked ignored or
unexecuted. No network access, external services, third-party crates or
credentials are used; all data is synthetic and created inside the tests.
