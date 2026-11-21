//! Integration tests for copy-on-write snapshot volumes.
//!
//! Scenarios are grouped as:
//! * normal: byte I/O, inheritance, zeroing, reverting, snapshotting;
//! * boundary: empty ranges, exact block edges, misaligned reverts, overflow;
//! * interleaved: multi-version trees, parent/child divergence, sibling
//!   isolation, and the parent-ABCD scenario from the specification.

use cow_snapshot_volume::{BlockKind, Snapshot, Volume, VolumeError, DEFAULT_BLOCK_SIZE};

fn vol(block_size: usize) -> Volume {
    Volume::with_block_size(block_size).expect("valid power-of-two block size")
}

fn read(v: &Volume, offset: u64, len: usize) -> Vec<u8> {
    v.read(offset, len).expect("read succeeds")
}

fn read_snap(s: &Snapshot, offset: u64, len: usize) -> Vec<u8> {
    s.read(offset, len).expect("read succeeds")
}

// ---------------------------------------------------------------------------
// Normal operation
// ---------------------------------------------------------------------------

#[test]
fn default_block_size_and_zero_filled_unallocated_space() {
    let mut v = Volume::new();
    assert_eq!(v.block_size(), DEFAULT_BLOCK_SIZE);
    assert_eq!(read(&v, 0, 16), vec![0u8; 16]);

    v.write(0, b"hello").unwrap();
    assert_eq!(read(&v, 0, 8), b"hello\0\0\0");
    assert_eq!(v.len(), 5);
    assert_eq!(v.block_count(), 1);
    assert_eq!(v.block_kind(0), BlockKind::Local);

    // Untouched distant block is inherited/unallocated and reads as zeros.
    assert_eq!(v.block_kind(7), BlockKind::Inherited);
    assert_eq!(read(&v, 7 * DEFAULT_BLOCK_SIZE as u64, 4), vec![0u8; 4]);
}

#[test]
fn byte_granular_writes_across_block_boundaries() {
    let mut v = vol(8);
    v.write(6, b"abcdefghij").unwrap(); // spans block 0 tail + block 1 + block 2 head
    assert_eq!(read(&v, 0, 16), b"\0\0\0\0\0\0abcdefghij");
    assert_eq!(v.len(), 16);
    assert_eq!(v.block_count(), 2);

    // Overwrite one byte in the middle and verify siblings survive.
    v.write(9, b"Z").unwrap();
    assert_eq!(read(&v, 6, 10), b"abcZefghij");
}

#[test]
fn child_inherits_parent_bytes_without_copying_untouched_blocks() {
    let mut parent = vol(4);
    parent.write(0, b"AAAABBBB").unwrap();
    let snap = parent.snapshot();

    let mut child = snap.clone_volume();
    assert_eq!(read(&child, 0, 8), b"AAAABBBB");
    assert_eq!(child.block_kind(0), BlockKind::Inherited);
    assert_eq!(child.block_kind(1), BlockKind::Inherited);
    assert_eq!(child.len(), 8);

    // Partial write only touches block 1.
    child.write(4, b"XY").unwrap();
    assert_eq!(child.block_kind(0), BlockKind::Inherited);
    assert_eq!(child.block_kind(1), BlockKind::Local);
    assert_eq!(read(&child, 0, 8), b"AAAAXYBB");
    // Parent is unchanged by the child's write.
    assert_eq!(read(&parent, 0, 8), b"AAAABBBB");
}

#[test]
fn zero_range_crosses_blocks_and_hides_ancestor_data() {
    let mut parent = vol(4);
    parent.write(0, b"AAAABBBBCCCC").unwrap();
    let snap = parent.snapshot();
    let mut child = snap.clone_volume();

    // Covers block 0 tail (2..4), all of block 1, block 2 head (0..2).
    child.zero_range(2, 10).unwrap();
    assert_eq!(read(&child, 0, 12), b"AA\0\0\0\0\0\0\0\0CC");

    // Fully covered block becomes a zero mark; partial blocks become local data.
    assert_eq!(child.block_kind(0), BlockKind::Local);
    assert_eq!(child.block_kind(1), BlockKind::ZeroMark);
    assert_eq!(child.block_kind(2), BlockKind::Local);

    // Zeroed bytes must not resurrect parent data when read through children.
    let grandchild = child.snapshot().clone_volume();
    assert_eq!(read(&grandchild, 0, 12), b"AA\0\0\0\0\0\0\0\0CC");
    assert_eq!(read(&parent, 0, 12), b"AAAABBBBCCCC");
}

#[test]
fn zero_whole_block_records_zero_mark_even_when_unallocated() {
    let mut v = vol(4);
    v.write(0, b"AAAA").unwrap();
    v.zero_range(0, 4).unwrap();
    assert_eq!(v.block_kind(0), BlockKind::ZeroMark);
    assert_eq!(read(&v, 0, 4), b"\0\0\0\0");
}

#[test]
fn write_after_zero_mark_materialises_zeroed_block() {
    let mut parent = vol(4);
    parent.write(0, b"ABCD").unwrap();
    let mut child = parent.snapshot().clone_volume();
    child.zero_range(0, 4).unwrap();
    assert_eq!(child.block_kind(0), BlockKind::ZeroMark);

    child.write(1, b"xy").unwrap();
    assert_eq!(child.block_kind(0), BlockKind::Local);
    assert_eq!(read(&child, 0, 4), b"\0xy\0");
}

#[test]
fn revert_aligned_blocks_reexposes_creation_snapshot() {
    let mut parent = vol(4);
    parent.write(0, b"AAAABBBBCCCC").unwrap();
    let snap = parent.snapshot();

    let mut child = snap.clone_volume();
    child.write(0, b"zzzz").unwrap();
    child.zero_range(4, 8).unwrap();
    child.write(8, b"YYYY").unwrap();
    assert_eq!(read(&child, 0, 12), b"zzzz\0\0\0\0YYYY");

    child.revert_blocks(0, 8).unwrap();
    assert_eq!(read(&child, 0, 12), b"AAAABBBBYYYY");
    assert_eq!(child.block_kind(0), BlockKind::Inherited);
    assert_eq!(child.block_kind(1), BlockKind::Inherited);
    assert_eq!(child.block_kind(2), BlockKind::Local);
}

#[test]
fn snapshot_freezes_volume_view() {
    let mut v = vol(4);
    v.write(0, b"AAAA").unwrap();
    let snap = v.snapshot();
    assert_eq!(read_snap(&snap, 0, 4), b"AAAA");

    v.write(0, b"BBBB").unwrap();
    assert_eq!(read_snap(&snap, 0, 4), b"AAAA");
    assert_eq!(read(&v, 0, 4), b"BBBB");
}

#[test]
fn parent_writes_after_clone_do_not_leak_into_old_clone() {
    let mut parent = vol(4);
    parent.write(0, b"AAAA").unwrap();
    let snap = parent.snapshot();
    let child = snap.clone_volume();

    // Mutate the parent volume after the snapshot was taken.
    parent.write(0, b"BBBB").unwrap();
    parent.write(4, b"CCCC").unwrap();

    assert_eq!(read(&child, 0, 4), b"AAAA");
    assert_eq!(read(&child, 4, 4), b"\0\0\0\0");
    assert_eq!(read(&parent, 0, 8), b"BBBBCCCC");
    // A fresh clone of the old snapshot still sees the old bytes.
    assert_eq!(read(&snap.clone_volume(), 0, 4), b"AAAA");
}

#[test]
fn snapshot_from_any_version_can_be_cloned() {
    let mut v0 = vol(4);
    v0.write(0, b"v1v1").unwrap();
    let s1 = v0.snapshot();

    let mut v1 = s1.clone_volume();
    v1.write(4, b"v2v2").unwrap();
    let s2 = v1.snapshot();

    let mut v2 = s2.clone_volume();
    v2.zero_range(0, 4).unwrap();
    let s3 = v2.snapshot();

    assert_eq!(read_snap(&s1, 0, 8), b"v1v1\0\0\0\0");
    assert_eq!(read_snap(&s2, 0, 8), b"v1v1v2v2");
    assert_eq!(read_snap(&s3, 0, 8), b"\0\0\0\0v2v2");

    // Clone from the middle version and diverge independently.
    let mut branch = s2.clone_volume();
    branch.write(0, b"brnc").unwrap();
    assert_eq!(read(&branch, 0, 8), b"brncv2v2");
    assert_eq!(read_snap(&s2, 0, 8), b"v1v1v2v2");
}

// ---------------------------------------------------------------------------
// The specification scenario
// ---------------------------------------------------------------------------

#[test]
fn spec_abcd_parent_zero_middle_snapshot_then_revert() {
    let mut parent = vol(4);
    parent.write(0, b"ABCD").unwrap();
    let parent_snap = parent.snapshot();

    let mut child = parent_snap.clone_volume();
    child.zero_range(1, 3).unwrap(); // zero B and C
    assert_eq!(read(&child, 0, 4), b"A\0\0D");
    assert_eq!(child.block_kind(0), BlockKind::Local);

    let grandchild_snap = child.snapshot();
    let grandchild = grandchild_snap.clone_volume();
    assert_eq!(read(&grandchild, 0, 4), b"A\0\0D");

    // Undo the child's overlay for block 0.
    child.revert_blocks(0, 4).unwrap();
    assert_eq!(child.block_kind(0), BlockKind::Inherited);
    assert_eq!(read(&child, 0, 4), b"ABCD");

    // Both descendants read their own four bytes.
    assert_eq!(read(&child, 0, 4), b"ABCD");
    assert_eq!(read(&grandchild, 0, 4), b"A\0\0D");

    // And the immutable parent snapshot is still intact.
    assert_eq!(read_snap(&parent_snap, 0, 4), b"ABCD");
}

// ---------------------------------------------------------------------------
// Boundary conditions
// ---------------------------------------------------------------------------

#[test]
fn empty_operations_are_noops() {
    let mut v = vol(4);
    v.write(0, b"AAAA").unwrap();

    v.write(2, b"").unwrap();
    assert_eq!(v.len(), 4);

    v.zero_range(1, 1).unwrap();
    assert_eq!(read(&v, 0, 4), b"AAAA");

    v.revert_blocks(0, 0).unwrap();
    assert_eq!(read(&v, 0, 4), b"AAAA");
}

#[test]
fn revert_requires_block_alignment() {
    let mut v = vol(4);
    v.write(0, b"AAAABBBB").unwrap();
    let err = v.revert_blocks(1, 4).unwrap_err();
    assert_eq!(
        err,
        VolumeError::MisalignedRevert {
            start: 1,
            end: 4,
            block_size: 4
        }
    );
    let err = v.revert_blocks(0, 6).unwrap_err();
    assert_eq!(
        err,
        VolumeError::MisalignedRevert {
            start: 0,
            end: 6,
            block_size: 4
        }
    );
    // Rejected calls leave the overlay untouched.
    assert_eq!(read(&v, 0, 8), b"AAAABBBB");
    // Aligned range ending exactly at the end is accepted.
    v.revert_blocks(0, 8).unwrap();
    assert_eq!(read(&v, 0, 8), vec![0u8; 8]);
}

#[test]
fn zero_range_rejects_inverted_range() {
    let mut v = vol(4);
    let err = v.zero_range(9, 3).unwrap_err();
    assert_eq!(err, VolumeError::InvalidRange { start: 9, end: 3 });
    assert_eq!(v.len(), 0);
}

#[test]
fn zero_range_exactly_on_block_edges_marks_every_block() {
    let mut parent = vol(4);
    parent.write(0, b"AAAABBBBCCCC").unwrap();
    let mut child = parent.snapshot().clone_volume();
    child.zero_range(4, 12).unwrap();
    assert_eq!(child.block_kind(0), BlockKind::Inherited);
    assert_eq!(child.block_kind(1), BlockKind::ZeroMark);
    assert_eq!(child.block_kind(2), BlockKind::ZeroMark);
    assert_eq!(read(&child, 0, 12), b"AAAA\0\0\0\0\0\0\0\0");
}

#[test]
fn zero_then_revert_returns_to_ancestor_bytes() {
    let mut parent = vol(4);
    parent.write(0, b"DATA").unwrap();
    let mut child = parent.snapshot().clone_volume();
    child.zero_range(0, 4).unwrap();
    assert_eq!(read(&child, 0, 4), b"\0\0\0\0");
    child.revert_blocks(0, 4).unwrap();
    assert_eq!(read(&child, 0, 4), b"DATA");
}

#[test]
fn reads_and_writes_overflowing_u64_are_rejected() {
    let mut v = vol(4);
    assert_eq!(
        v.write(u64::MAX, b"ab").unwrap_err(),
        VolumeError::OffsetOverflow {
            offset: u64::MAX,
            len: 2
        }
    );
    assert_eq!(
        v.read(u64::MAX, 2).unwrap_err(),
        VolumeError::OffsetOverflow {
            offset: u64::MAX,
            len: 2
        }
    );
    // Nothing was registered by the failed write.
    assert!(v.is_empty());
}

#[test]
fn block_kind_is_inherited_past_logical_length() {
    let mut parent = vol(4);
    parent.write(0, b"AAAA").unwrap();
    let child = parent.snapshot().clone_volume();
    assert_eq!(child.block_kind(1000), BlockKind::Inherited);
    assert_eq!(read(&child, 4000, 4), vec![0u8; 4]);
}

// ---------------------------------------------------------------------------
// Interleaved scenarios
// ---------------------------------------------------------------------------

#[test]
fn interleaved_writes_zeroing_and_reverts_on_same_block() {
    let mut parent = vol(4);
    parent.write(0, b"p123").unwrap();
    let mut child = parent.snapshot().clone_volume();

    child.write(0, b"x").unwrap();
    assert_eq!(read(&child, 0, 4), b"x123");

    child.zero_range(0, 2).unwrap();
    assert_eq!(read(&child, 0, 4), [0u8, 0, b'2', b'3']);

    child.write(0, b"q").unwrap();
    assert_eq!(read(&child, 0, 4), [b'q', 0, b'2', b'3']);

    child.revert_blocks(0, 4).unwrap();
    assert_eq!(read(&child, 0, 4), b"p123");
    assert_eq!(read(&parent, 0, 4), b"p123");
}

#[test]
fn sibling_branches_from_same_snapshot_are_isolated() {
    let mut base = vol(4);
    base.write(0, b"root").unwrap();
    let snap = base.snapshot();

    let mut left = snap.clone_volume();
    let mut right = snap.clone_volume();
    left.write(0, b"LLLL").unwrap();
    right.zero_range(0, 4).unwrap();

    assert_eq!(read(&left, 0, 4), b"LLLL");
    assert_eq!(read(&right, 0, 4), b"\0\0\0\0");
    assert_eq!(read_snap(&snap, 0, 4), b"root");

    // Snapshots of each branch stay independent too.
    let left_snap = left.snapshot();
    let right_snap = right.snapshot();
    assert_eq!(read_snap(&left_snap, 0, 4), b"LLLL");
    assert_eq!(read_snap(&right_snap, 0, 4), b"\0\0\0\0");
}

#[test]
fn deep_chain_with_revert_in_the_middle() {
    let mut root = vol(4);
    root.write(0, b"0000").unwrap();
    let s0 = root.snapshot();

    let mut a = s0.clone_volume();
    a.write(0, b"1111").unwrap();
    a.write(4, b"aaaa").unwrap();
    let s1 = a.snapshot();

    let mut b = s1.clone_volume();
    b.zero_range(0, 8).unwrap();
    let s2 = b.snapshot();

    let mut c = s2.clone_volume();
    c.write(4, b"cccc").unwrap();

    // Revert A's overlay; it was frozen into s1 already, so descendants keep it.
    a.revert_blocks(0, 8).unwrap();
    assert_eq!(read(&a, 0, 8), b"0000\0\0\0\0");
    assert_eq!(read_snap(&s1, 0, 8), b"1111aaaa");
    assert_eq!(read_snap(&s2, 0, 8), b"\0\0\0\0\0\0\0\0");
    assert_eq!(read(&c, 0, 8), b"\0\0\0\0cccc");

    // Clone from every version and confirm each resolves to its own bytes.
    assert_eq!(read(&s0.clone_volume(), 0, 8), b"0000\0\0\0\0");
    assert_eq!(read(&s1.clone_volume(), 0, 8), b"1111aaaa");
    assert_eq!(read(&s2.clone_volume(), 0, 8), b"\0\0\0\0\0\0\0\0");
}

#[test]
fn writes_past_inherited_length_extend_correctly() {
    let mut parent = vol(4);
    parent.write(0, b"abcd").unwrap();
    let mut child = parent.snapshot().clone_volume();
    assert_eq!(child.len(), 4);

    child.write(10, b"xy").unwrap();
    assert_eq!(child.len(), 12);
    assert_eq!(child.block_count(), 3);
    assert_eq!(read(&child, 0, 12), b"abcd\0\0\0\0\0\0xy");
    // Only block 2 (bytes 8..12) was touched; block 1 stays inherited.
    assert_eq!(child.block_kind(1), BlockKind::Inherited);
    assert_eq!(child.block_kind(2), BlockKind::Local);
    assert_eq!(read(&parent, 0, 4), b"abcd");
}
