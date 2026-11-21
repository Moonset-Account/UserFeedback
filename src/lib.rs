//! Copy-on-write snapshot volumes.
//!
//! A [`Volume`] is a writable view over an immutable [`Snapshot`]. The volume
//! only stores the blocks it has actually touched; every other block is
//! inherited from the snapshot it was cloned from (and, transitively, from that
//! snapshot's own ancestors). Unallocated space reads as zero bytes.
//!
//! Semantics in one paragraph:
//!
//! * Reads and writes are byte-granular. A write to part of a block first
//!   materialises the block's inherited bytes into this volume's overlay and
//!   then patches them, so the parent snapshot is never modified.
//! * [`Volume::zero_range`] can span blocks. Fully covered blocks are recorded
//!   as a [`BlockKind::ZeroMark`] — a whole-block tombstone that hides ancestor
//!   data without allocating a data block. Partially covered blocks become
//!   [`BlockKind::Local`] data with the covered bytes zeroed.
//! * [`Volume::revert_blocks`] takes a block-aligned range and drops this
//!   volume's overlay entries in it, so those blocks once again read from the
//!   snapshot that existed when the volume was created. Ancestors are immutable,
//!   so later writes to the parent volume can never leak into an existing clone.
//! * [`Volume::snapshot`] freezes the volume's current view as a new immutable
//!   snapshot; cloning a volume from *any* snapshot is supported, so snapshot
//!   trees of arbitrary depth can be built.
//!
//! Blocks are addressed by index (`block_index = offset / block_size`). The
//! default block size is [`DEFAULT_BLOCK_SIZE`] (4096 bytes); a custom power of
//! two can be chosen with [`Volume::with_block_size`].

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;
use std::sync::Arc;

/// Default block size used by [`Volume::new`], in bytes.
pub const DEFAULT_BLOCK_SIZE: usize = 4096;

/// Errors reported by volume operations.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VolumeError {
    /// `end` is smaller than `start`.
    InvalidRange { start: u64, end: u64 },
    /// A revert range was not aligned to whole blocks.
    MisalignedRevert {
        start: u64,
        end: u64,
        block_size: usize,
    },
    /// The requested block size was zero or not a power of two.
    InvalidBlockSize(usize),
    /// `offset + len` does not fit in `u64`.
    OffsetOverflow { offset: u64, len: u64 },
}

impl fmt::Display for VolumeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            VolumeError::InvalidRange { start, end } => {
                write!(f, "invalid range: start {start} is greater than end {end}")
            }
            VolumeError::MisalignedRevert {
                start,
                end,
                block_size,
            } => write!(
                f,
                "revert range {start}..{end} is not aligned to blocks of {block_size} bytes"
            ),
            VolumeError::InvalidBlockSize(size) => write!(
                f,
                "invalid block size {size}: must be a non-zero power of two"
            ),
            VolumeError::OffsetOverflow { offset, len } => {
                write!(f, "offset {offset} plus length {len} overflows u64")
            }
        }
    }
}

impl Error for VolumeError {}

/// How a single block is represented in *this* volume's overlay.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BlockKind {
    /// No overlay entry: the block is read from the snapshot this volume was
    /// cloned from (and may resolve to zeros).
    Inherited,
    /// A whole-block zero tombstone. The block reads as zeros regardless of
    /// what ancestors hold.
    ZeroMark,
    /// Locally stored data (possibly a partially zeroed or partially written
    /// block).
    Local,
}

/// A block value in an overlay layer.
#[derive(Debug, Clone, PartialEq, Eq)]
enum BlockState {
    /// Whole block is logical zero.
    Zero,
    /// Full block of bytes; length always equals the volume block size.
    Data(Vec<u8>),
}

impl BlockState {
    fn as_bytes(&self) -> Option<&[u8]> {
        match self {
            BlockState::Zero => None,
            BlockState::Data(data) => Some(data),
        }
    }
}

/// Borrowed view of a resolved block: either all zeros or a data slice.
enum BlockView<'a> {
    Zero,
    Data(&'a [u8]),
}

/// One immutable layer of the snapshot chain.
#[derive(Debug)]
struct SnapState {
    parent: Option<Arc<SnapState>>,
    overlay: BTreeMap<u64, BlockState>,
    len: u64,
    block_size: usize,
}

impl SnapState {
    /// Resolve a block index through this layer and its ancestors.
    fn lookup(&self, index: u64) -> Option<&BlockState> {
        let mut current: Option<&SnapState> = Some(self);
        while let Some(layer) = current {
            if let Some(state) = layer.overlay.get(&index) {
                return Some(state);
            }
            current = layer.parent.as_deref();
        }
        None
    }
}

/// An immutable, frozen version of a volume.
///
/// Snapshots are cheap to clone ([`Arc`] bump) and can be read directly. A
/// snapshot never changes; writing to the volume it came from affects only that
/// volume's overlay, so snapshots taken earlier keep their bytes.
#[derive(Debug, Clone)]
pub struct Snapshot {
    state: Arc<SnapState>,
}

impl Snapshot {
    /// Block size of the snapshot family, in bytes.
    pub fn block_size(&self) -> usize {
        self.state.block_size
    }

    /// Logical length in bytes: the highest offset ever written or zeroed.
    ///
    /// Reads past this length are still allowed and return zeros (the space is
    /// unallocated); the length merely records how far the volume has grown.
    pub fn len(&self) -> u64 {
        self.state.len
    }

    /// Returns `true` when the snapshot's logical length is zero.
    pub fn is_empty(&self) -> bool {
        self.state.len == 0
    }

    /// Number of whole or partial blocks covering the logical length.
    pub fn block_count(&self) -> u64 {
        self.state.len.div_ceil(self.state.block_size as u64)
    }

    /// Read `len` bytes at `offset`, zero-filling unallocated space.
    pub fn read(&self, offset: u64, len: usize) -> Result<Vec<u8>, VolumeError> {
        let mut buf = vec![0u8; len];
        self.read_into(offset, &mut buf)?;
        Ok(buf)
    }

    /// Fill `buf` with bytes starting at `offset`, zero-filling unallocated
    /// space.
    pub fn read_into(&self, offset: u64, buf: &mut [u8]) -> Result<(), VolumeError> {
        let state = self.state.as_ref();
        read_into_generic(
            self.state.block_size as u64,
            offset,
            buf,
            |index| match state.lookup(index).and_then(BlockState::as_bytes) {
                Some(data) => BlockView::Data(data),
                None => BlockView::Zero,
            },
        )
    }

    /// Clone a fresh writable volume whose base is this snapshot.
    pub fn clone_volume(&self) -> Volume {
        Volume::from_snapshot(self)
    }
}

/// A writable volume with copy-on-write overlay blocks.
#[derive(Debug, Clone)]
pub struct Volume {
    base: Option<Arc<SnapState>>,
    overlay: BTreeMap<u64, BlockState>,
    len: u64,
    block_size: usize,
}

impl Default for Volume {
    fn default() -> Self {
        Volume::new()
    }
}

impl Volume {
    /// Create an empty volume using [`DEFAULT_BLOCK_SIZE`].
    pub fn new() -> Self {
        Volume {
            base: None,
            overlay: BTreeMap::new(),
            len: 0,
            block_size: DEFAULT_BLOCK_SIZE,
        }
    }

    /// Create an empty volume with a custom block size.
    ///
    /// The block size must be a non-zero power of two. It determines the
    /// granularity of overlay blocks and of [`Volume::revert_blocks`].
    pub fn with_block_size(block_size: usize) -> Result<Self, VolumeError> {
        if block_size == 0 || !block_size.is_power_of_two() {
            return Err(VolumeError::InvalidBlockSize(block_size));
        }
        Ok(Volume {
            base: None,
            overlay: BTreeMap::new(),
            len: 0,
            block_size,
        })
    }

    /// Clone a writable volume from a snapshot (of any version).
    ///
    /// The new volume starts with an empty overlay and inherits everything
    /// from `snapshot`.
    pub fn from_snapshot(snapshot: &Snapshot) -> Self {
        Volume {
            base: Some(Arc::clone(&snapshot.state)),
            overlay: BTreeMap::new(),
            len: snapshot.state.len,
            block_size: snapshot.state.block_size,
        }
    }

    /// Freeze the current view of the volume as a new immutable snapshot.
    ///
    /// The snapshot chains onto the base this volume was cloned from, plus a
    /// frozen copy of the volume's current overlay. Later writes to this volume
    /// do not affect the returned snapshot.
    pub fn snapshot(&self) -> Snapshot {
        Snapshot {
            state: Arc::new(SnapState {
                parent: self.base.clone(),
                overlay: self.overlay.clone(),
                len: self.len,
                block_size: self.block_size,
            }),
        }
    }

    /// Block size in bytes.
    pub fn block_size(&self) -> usize {
        self.block_size
    }

    /// Logical length in bytes (highest written or zeroed offset).
    pub fn len(&self) -> u64 {
        self.len
    }

    /// Returns `true` when nothing has been written or zeroed yet.
    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    /// Number of blocks covering the logical length.
    pub fn block_count(&self) -> u64 {
        self.len.div_ceil(self.block_size as u64)
    }

    /// How block `index` is represented in this volume's own overlay.
    ///
    /// This deliberately reports only the local layer: a block whose data comes
    /// from an ancestor snapshot is [`BlockKind::Inherited`] even if that
    /// ancestor stores concrete bytes. Use [`Volume::read`] to observe the
    /// resolved bytes.
    pub fn block_kind(&self, index: u64) -> BlockKind {
        match self.overlay.get(&index) {
            Some(BlockState::Zero) => BlockKind::ZeroMark,
            Some(BlockState::Data(_)) => BlockKind::Local,
            None => BlockKind::Inherited,
        }
    }

    /// Read `len` bytes at `offset`, zero-filling unallocated space.
    pub fn read(&self, offset: u64, len: usize) -> Result<Vec<u8>, VolumeError> {
        let mut buf = vec![0u8; len];
        self.read_into(offset, &mut buf)?;
        Ok(buf)
    }

    /// Fill `buf` with bytes starting at `offset`, zero-filling unallocated
    /// space. Reading past the logical end is allowed and yields zeros.
    pub fn read_into(&self, offset: u64, buf: &mut [u8]) -> Result<(), VolumeError> {
        read_into_generic(
            self.block_size as u64,
            offset,
            buf,
            |index| match self.lookup(index).and_then(BlockState::as_bytes) {
                Some(data) => BlockView::Data(data),
                None => BlockView::Zero,
            },
        )
    }

    /// Write `data` at byte `offset`, extending the volume if needed.
    ///
    /// Only touched blocks are materialised into the overlay; untouched blocks
    /// keep inheriting from the base snapshot. Writing an empty slice is a
    /// no-op.
    pub fn write(&mut self, offset: u64, data: &[u8]) -> Result<(), VolumeError> {
        if data.is_empty() {
            return Ok(());
        }
        let data_len = data.len() as u64;
        let end = offset
            .checked_add(data_len)
            .ok_or(VolumeError::OffsetOverflow {
                offset,
                len: data_len,
            })?;
        let block_size = self.block_size as u64;
        let mut index = offset / block_size;
        let end_index = (end - 1) / block_size;

        while index <= end_index {
            let block_start = index * block_size;
            let block_end =
                block_start
                    .checked_add(block_size)
                    .ok_or(VolumeError::OffsetOverflow {
                        offset: block_start,
                        len: block_size,
                    })?;
            let copy_start = offset.max(block_start);
            let copy_end = end.min(block_end);

            let mut block = self.materialize(index);
            let from = (copy_start - block_start) as usize;
            let to = (copy_end - block_start) as usize;
            let data_from = (copy_start - offset) as usize;
            let data_to = (copy_end - offset) as usize;
            block[from..to].copy_from_slice(&data[data_from..data_to]);
            self.overlay.insert(index, BlockState::Data(block));

            index += 1;
        }

        self.len = self.len.max(end);
        Ok(())
    }

    /// Zero the byte range `start..end`, extending the volume if needed.
    ///
    /// The range may start and end anywhere, including in the middle of a
    /// block. Blocks fully covered by the range are recorded as whole-block
    /// zero tombstones ([`BlockKind::ZeroMark`]) which hide any ancestor data
    /// without allocating a data block. Partially covered blocks become local
    /// data with the covered bytes zeroed. An empty range is a no-op.
    pub fn zero_range(&mut self, start: u64, end: u64) -> Result<(), VolumeError> {
        if end < start {
            return Err(VolumeError::InvalidRange { start, end });
        }
        if start == end {
            return Ok(());
        }
        let block_size = self.block_size as u64;
        let mut index = start / block_size;
        let end_index = (end - 1) / block_size;

        while index <= end_index {
            let block_start = index * block_size;
            let block_end =
                block_start
                    .checked_add(block_size)
                    .ok_or(VolumeError::OffsetOverflow {
                        offset: block_start,
                        len: block_size,
                    })?;

            if start <= block_start && block_end <= end {
                // Whole block covered: a tombstone is enough and shadows all
                // ancestor data for this block.
                self.overlay.insert(index, BlockState::Zero);
            } else {
                let mut block = self.materialize(index);
                let from = (start.max(block_start) - block_start) as usize;
                let to = (end.min(block_end) - block_start) as usize;
                block[from..to].fill(0);
                self.overlay.insert(index, BlockState::Data(block));
            }

            index += 1;
        }

        self.len = self.len.max(end);
        Ok(())
    }

    /// Discard this volume's overlay for whole blocks in `start..end`.
    ///
    /// `start` and `end` must both be multiples of the block size; otherwise
    /// [`VolumeError::MisalignedRevert`] is returned. After reverting, the range
    /// reads the snapshot the volume was cloned from at creation time — not the
    /// parent's current contents, because ancestors are immutable. An empty
    /// range is a no-op.
    ///
    /// The logical length is not shrunk.
    pub fn revert_blocks(&mut self, start: u64, end: u64) -> Result<(), VolumeError> {
        if end < start {
            return Err(VolumeError::InvalidRange { start, end });
        }
        let block_size = self.block_size as u64;
        if start % block_size != 0 || end % block_size != 0 {
            return Err(VolumeError::MisalignedRevert {
                start,
                end,
                block_size: self.block_size,
            });
        }
        if start == end {
            return Ok(());
        }
        let first = start / block_size;
        let last = end / block_size;
        self.overlay
            .retain(|&index, _| index < first || index >= last);
        Ok(())
    }

    /// Resolve a block index through the local overlay and the snapshot chain.
    fn lookup(&self, index: u64) -> Option<&BlockState> {
        if let Some(state) = self.overlay.get(&index) {
            return Some(state);
        }
        self.base.as_ref().and_then(|base| base.lookup(index))
    }

    /// Return a full copy of the block's current logical bytes, zeros when
    /// unallocated.
    fn materialize(&self, index: u64) -> Vec<u8> {
        match self.lookup(index) {
            Some(BlockState::Data(data)) => data.clone(),
            _ => vec![0u8; self.block_size],
        }
    }
}

/// Shared byte-wise read loop. `view` resolves a block index to either zeros or
/// a data slice; blocks past the written region resolve to zeros.
fn read_into_generic<'a, F>(
    block_size: u64,
    offset: u64,
    buf: &mut [u8],
    mut view: F,
) -> Result<(), VolumeError>
where
    F: FnMut(u64) -> BlockView<'a>,
{
    let total = buf.len() as u64;
    offset
        .checked_add(total)
        .ok_or(VolumeError::OffsetOverflow {
            offset,
            len: total,
        })?;

    let mut done: usize = 0;
    let mut position = offset;
    while done < buf.len() {
        let index = position / block_size;
        let within = (position % block_size) as usize;
        let take = (block_size as usize - within).min(buf.len() - done);
        match view(index) {
            BlockView::Zero => buf[done..done + take].fill(0),
            BlockView::Data(data) => {
                buf[done..done + take].copy_from_slice(&data[within..within + take]);
            }
        }
        done += take;
        position += take as u64;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn block_size_must_be_power_of_two() {
        assert_eq!(
            Volume::with_block_size(0).unwrap_err(),
            VolumeError::InvalidBlockSize(0)
        );
        assert_eq!(
            Volume::with_block_size(3).unwrap_err(),
            VolumeError::InvalidBlockSize(3)
        );
        assert!(Volume::with_block_size(64).is_ok());
    }

    #[test]
    fn new_volume_is_empty_and_reads_zeros() {
        let volume = Volume::new();
        assert!(volume.is_empty());
        assert_eq!(volume.len(), 0);
        assert_eq!(volume.block_count(), 0);
        assert_eq!(volume.read(0, 8).unwrap(), vec![0u8; 8]);
        assert_eq!(volume.read(1 << 20, 4).unwrap(), vec![0u8; 4]);
    }
}
