//! Integration tests for the copy-on-write block reclamation module.
//!
//! Scenarios are grouped as: normal paths, boundary conditions,
//! interleaved write/reader/gc flows, and the required acceptance
//! sequence (snapshot withdrawn, old reader still open, shared child
//! re-referenced by a new write, reopened at two checkpoints).

use cow_gc::{Error, Reason, Store};

/// Build a store with one published snapshot:
/// block 0 = leaf "shared-child", block 1 = root "root-v1" -> [0].
fn store_with_snap1(capacity: usize) -> Store {
    let mut s = Store::new(capacity);
    let t = s.begin_write();
    let b0 = s.reserve(t).unwrap();
    s.write_block(t, b0, b"shared-child".to_vec(), vec![]).unwrap();
    let b1 = s.reserve(t).unwrap();
    s.write_block(t, b1, b"root-v1".to_vec(), vec![b0]).unwrap();
    s.publish(t, "snap1", b1).unwrap();
    s
}

// ---------------------------------------------------------------------
// normal paths
// ---------------------------------------------------------------------

#[test]
fn write_publish_read_roundtrip() {
    let mut s = store_with_snap1(4);
    let r = s.open_reader("snap1").unwrap();
    assert_eq!(s.reader_root(r), Ok(1));
    assert_eq!(s.read_block(r, 1).unwrap().data, b"root-v1");
    assert_eq!(s.read_block(r, 1).unwrap().refs, vec![0]);
    assert_eq!(s.read_block(r, 0).unwrap().data, b"shared-child");
    s.close_reader(r).unwrap();
    assert_eq!(s.read_block(r, 0), Err(Error::NoSuchReader(r)));
}

#[test]
fn reader_survives_snapshot_deletion() {
    let mut s = store_with_snap1(4);
    let r = s.open_reader("snap1").unwrap();
    s.delete_snapshot("snap1").unwrap();
    // Name is gone for new readers...
    assert_eq!(
        s.open_reader("snap1"),
        Err(Error::NoSuchSnapshot("snap1".to_string()))
    );
    // ...but the old reader still finishes its reads.
    assert_eq!(s.read_block(r, 1).unwrap().data, b"root-v1");
    assert_eq!(s.read_block(r, 0).unwrap().data, b"shared-child");
    // GC must not reclaim pinned blocks.
    let report = s.gc();
    assert!(report.reclaimed.is_empty());
    assert!(report.free.contains(&2) && report.free.contains(&3));
    // Only after the reader closes do the blocks become reclaimable.
    s.close_reader(r).unwrap();
    let report = s.gc();
    assert_eq!(report.reclaimed, vec![0, 1]);
    assert_eq!(report.free, vec![0, 1, 2, 3]);
}

#[test]
fn cancel_releases_only_own_reservations() {
    let mut s = store_with_snap1(6);
    let t1 = s.begin_write();
    let a = s.reserve(t1).unwrap(); // 2
    s.write_block(t1, a, b"t1".to_vec(), vec![]).unwrap();
    let t2 = s.begin_write();
    let b = s.reserve(t2).unwrap(); // 3
    s.write_block(t2, b, b"t2".to_vec(), vec![]).unwrap();

    s.cancel(t1).unwrap();
    // t1's block is free again, t2's is untouched, published blocks intact.
    let alloc = s.allocatable();
    assert!(alloc.free.contains(&a));
    assert!(!alloc.free.contains(&b));
    let held_b = alloc.held.iter().find(|(id, _)| *id == b).unwrap();
    assert!(held_b.1.contains(&Reason::ReservedByWrite(t2)));

    // t2 can still publish after t1 cancelled.
    s.publish(t2, "snap2", b).unwrap();
    assert_eq!(s.snapshots()["snap2"], b);
}

#[test]
fn duplicate_cancel_is_idempotent_no_double_free() {
    let mut s = Store::new(2);
    let t = s.begin_write();
    let b = s.reserve(t).unwrap();
    s.write_block(t, b, b"x".to_vec(), vec![]).unwrap();

    s.cancel(t).unwrap();
    assert_eq!(s.allocatable().free, vec![0, 1]);
    // Replaying the same cancel event must not free anything again.
    s.cancel(t).unwrap();
    s.cancel(t).unwrap();
    assert_eq!(s.allocatable().free, vec![0, 1]);

    // The freed block can be reserved exactly once by a later write.
    let t2 = s.begin_write();
    assert_eq!(s.reserve(t2).unwrap(), b);
    assert_eq!(s.reserve(t2).unwrap(), 1);
    assert_eq!(s.reserve(t2), Err(Error::OutOfSpace));
}

#[test]
fn gc_reports_held_reasons() {
    let mut s = store_with_snap1(5);
    let r = s.open_reader("snap1").unwrap();
    let t = s.begin_write();
    let staged = s.reserve(t).unwrap(); // 2
    s.write_block(t, staged, b"staged".to_vec(), vec![0]).unwrap();

    let alloc = s.allocatable();
    assert_eq!(alloc.free, vec![3, 4]);
    let reasons_of = |id: u32| alloc.held.iter().find(|(b, _)| *b == id).unwrap().1.clone();
    assert!(reasons_of(1).contains(&Reason::SnapshotRoot("snap1".to_string())));
    assert!(reasons_of(1).contains(&Reason::ReaderPin(r)));
    assert!(reasons_of(0).contains(&Reason::ReachableFromSnapshot("snap1".to_string())));
    assert!(reasons_of(0).contains(&Reason::ReachableFromReader(r)));
    assert!(reasons_of(2).contains(&Reason::ReservedByWrite(t)));
}

// ---------------------------------------------------------------------
// boundary conditions
// ---------------------------------------------------------------------

#[test]
fn zero_capacity_store() {
    let mut s = Store::new(0);
    let t = s.begin_write();
    assert_eq!(s.reserve(t), Err(Error::OutOfSpace));
    assert_eq!(s.gc().free, Vec::<u32>::new());
}

#[test]
fn exhausted_store_recovers_after_cancel() {
    let mut s = Store::new(2);
    let t1 = s.begin_write();
    let b0 = s.reserve(t1).unwrap();
    s.write_block(t1, b0, b"a".to_vec(), vec![]).unwrap();
    let b1 = s.reserve(t1).unwrap();
    s.write_block(t1, b1, b"b".to_vec(), vec![b0]).unwrap();

    // Store full: a second write cannot reserve.
    let t2 = s.begin_write();
    assert_eq!(s.reserve(t2), Err(Error::OutOfSpace));

    // Cancelling the first write frees exactly its two blocks.
    s.cancel(t1).unwrap();
    let c0 = s.reserve(t2).unwrap();
    let c1 = s.reserve(t2).unwrap();
    assert_eq!(vec![c0, c1], vec![0, 1]);
    assert_eq!(s.reserve(t2), Err(Error::OutOfSpace));
}

#[test]
fn invalid_operations_are_rejected() {
    let mut s = store_with_snap1(6);
    // Write into a block not reserved by this txn.
    let t = s.begin_write();
    assert_eq!(s.write_block(t, 0, b"x".to_vec(), vec![]), Err(Error::BlockNotReservedBy(0, t)));
    let b = s.reserve(t).unwrap(); // 2
    // Reference an out-of-range block.
    assert_eq!(s.write_block(t, b, b"x".to_vec(), vec![7]), Err(Error::InvalidRef(7)));
    // Reference another txn's reserved block.
    let t2 = s.begin_write();
    let other = s.reserve(t2).unwrap(); // 3
    assert_eq!(s.write_block(t, b, b"x".to_vec(), vec![other]), Err(Error::InvalidRef(other)));
    assert_eq!(s.write_block(t, b, b"x".to_vec(), vec![]), Ok(()));
    // Publish with an unwritten reserved block fails.
    let t3 = s.begin_write();
    let unwritten = s.reserve(t3).unwrap(); // 4
    assert_eq!(s.publish(t3, "bad", unwritten), Err(Error::EmptyWrite));
    // Cancel a committed write fails.
    assert_eq!(s.cancel(1), Err(Error::TxnCommitted(1)));
    // Unknown ids.
    assert_eq!(s.cancel(999), Err(Error::NoSuchTxn(999)));
    assert_eq!(s.delete_snapshot("nope"), Err(Error::NoSuchSnapshot("nope".to_string())));
    assert_eq!(s.close_reader(42), Err(Error::NoSuchReader(42)));
    // Bad snapshot name.
    let t4 = s.begin_write();
    let rb = s.reserve(t4).unwrap(); // 5
    s.write_block(t4, rb, b"x".to_vec(), vec![]).unwrap();
    assert_eq!(s.publish(t4, "has space", rb), Err(Error::BadName));
}

#[test]
fn corrupt_checkpoint_is_rejected() {
    assert!(matches!(Store::recover(b"garbage"), Err(Error::BadCheckpoint(_))));
    let s = store_with_snap1(2);
    let cp = s.checkpoint();
    // Truncated checkpoint (no END marker).
    let truncated = &cp[..cp.len() / 2];
    assert!(matches!(Store::recover(truncated), Err(Error::BadCheckpoint(_))));
}

// ---------------------------------------------------------------------
// persistence / recovery
// ---------------------------------------------------------------------

#[test]
fn recovery_classifies_published_staging_and_orphaned() {
    let mut s = store_with_snap1(5);
    let t = s.begin_write();
    let staged = s.reserve(t).unwrap(); // 2
    s.write_block(t, staged, b"staged".to_vec(), vec![0]).unwrap();

    let cp = s.checkpoint();
    let (s2, report) = Store::recover(&cp).unwrap();
    assert_eq!(report.published, vec!["snap1".to_string()]);
    assert_eq!(report.staging_writes, vec![t]);
    assert!(report.orphaned_reservations.is_empty());
    // The staging write can be continued after recovery.
    let mut s2 = s2;
    s2.publish(t, "snap2", staged).unwrap();
    assert_eq!(s2.snapshots()["snap2"], staged);
}

#[test]
fn recovery_returns_orphaned_reservations_to_free_pool() {
    let mut s = Store::new(3);
    let t = s.begin_write();
    let b = s.reserve(t).unwrap();
    s.write_block(t, b, b"x".to_vec(), vec![]).unwrap();
    let cp = String::from_utf8(s.checkpoint()).unwrap();
    // Simulate a crash window: the block stayed reserved on disk but the
    // owning transaction record says it was cancelled (or is missing).
    let cp_orphan = cp.replace(
        &format!("txn {t} active reserved {b} root -"),
        &format!("txn {t} cancelled reserved - root -"),
    );
    assert_ne!(cp, cp_orphan);
    let (s2, report) = Store::recover(cp_orphan.as_bytes()).unwrap();
    assert_eq!(report.orphaned_reservations, vec![b]);
    assert!(report.staging_writes.is_empty());
    assert_eq!(s2.allocatable().free, vec![0, 1, 2]);
}

#[test]
fn cancel_remains_idempotent_across_recovery() {
    let mut s = Store::new(2);
    let t = s.begin_write();
    let b = s.reserve(t).unwrap();
    s.write_block(t, b, b"x".to_vec(), vec![]).unwrap();
    s.cancel(t).unwrap();

    let cp = s.checkpoint();
    let (mut s2, report) = Store::recover(&cp).unwrap();
    assert!(report.orphaned_reservations.is_empty());
    assert_eq!(s2.allocatable().free, vec![0, 1]);
    // Replaying the cancel event after recovery frees nothing again.
    s2.cancel(t).unwrap();
    assert_eq!(s2.allocatable().free, vec![0, 1]);
}

// ---------------------------------------------------------------------
// interleaved scenario
// ---------------------------------------------------------------------

#[test]
fn interleaved_writes_and_gc() {
    let mut s = store_with_snap1(6);
    // Two concurrent writes; one publishes, one cancels mid-flight.
    let t1 = s.begin_write();
    let a = s.reserve(t1).unwrap(); // 2
    s.write_block(t1, a, b"new-leaf".to_vec(), vec![]).unwrap();
    let t2 = s.begin_write();
    let c = s.reserve(t2).unwrap(); // 3
    s.write_block(t2, c, b"abandoned".to_vec(), vec![]).unwrap();

    let root = s.reserve(t1).unwrap(); // 4
    s.write_block(t1, root, b"root-v2".to_vec(), vec![a, 0]).unwrap();
    s.publish(t1, "snap2", root).unwrap();
    s.cancel(t2).unwrap();

    // Old snapshot deleted; its now-unshared root is reclaimable, the
    // shared child is not.
    s.delete_snapshot("snap1").unwrap();
    let report = s.gc();
    assert_eq!(report.reclaimed, vec![1]);
    assert!(report.free.contains(&c)); // cancelled write's block
    let held0 = report.held.iter().find(|(b, _)| *b == 0).unwrap();
    assert!(held0.1.contains(&Reason::ReachableFromSnapshot("snap2".to_string())));
}

// ---------------------------------------------------------------------
// required acceptance sequence
// ---------------------------------------------------------------------

#[test]
fn acceptance_snapshot_withdrawn_reader_open_shared_child_two_checkpoints() {
    // --- setup: snap1 = root 1 -> child 0 ------------------------------
    let mut s = store_with_snap1(6);

    // Old reader opens snap1 and pins root 1.
    let reader = s.open_reader("snap1").unwrap();
    // Snapshot name is withdrawn while the reader is still open.
    s.delete_snapshot("snap1").unwrap();

    // A new write re-references the shared child block 0.
    let w = s.begin_write();
    let delta = s.reserve(w).unwrap(); // 2
    s.write_block(w, delta, b"delta".to_vec(), vec![0]).unwrap();
    let root2 = s.reserve(w).unwrap(); // 3
    s.write_block(w, root2, b"root-v2".to_vec(), vec![delta, 0]).unwrap();

    // --- persistence cut 1: staging write in flight, reader open ------
    let cp1 = s.checkpoint();
    let (mut s1, rep1) = Store::recover(&cp1).unwrap();
    assert!(rep1.published.is_empty(), "snap1 was deleted before cp1");
    assert_eq!(rep1.staging_writes, vec![w]);
    assert!(rep1.orphaned_reservations.is_empty());

    // Old reader still completes its reads with the original contents.
    assert_eq!(s1.read_block(reader, 1).unwrap().data, b"root-v1");
    assert_eq!(s1.read_block(reader, 0).unwrap().data, b"shared-child");

    // Nothing is reclaimable: 0,1 pinned by the reader; 2,3 staged.
    let g1 = s1.gc();
    assert!(g1.reclaimed.is_empty());
    assert_eq!(g1.free, vec![4, 5]);
    let reasons_of = |id: u32, rep: &cow_gc::GcReport| {
        rep.held.iter().find(|(b, _)| *b == id).unwrap().1.clone()
    };
    assert!(reasons_of(1, &g1).contains(&Reason::ReaderPin(reader)));
    assert!(reasons_of(0, &g1).contains(&Reason::ReachableFromReader(reader)));
    assert!(reasons_of(2, &g1).contains(&Reason::ReservedByWrite(w)));
    assert!(reasons_of(3, &g1).contains(&Reason::ReservedByWrite(w)));

    // The recovered staging write publishes; the old reader finishes.
    s1.publish(w, "snap2", root2).unwrap();
    s1.close_reader(reader).unwrap();

    // --- persistence cut 2: new root published, reader gone -----------
    let cp2 = s1.checkpoint();
    let (mut s2, rep2) = Store::recover(&cp2).unwrap();
    assert_eq!(rep2.published, vec!["snap2".to_string()]);
    assert!(rep2.staging_writes.is_empty());
    assert!(rep2.orphaned_reservations.is_empty());

    // First reusable block is the withdrawn snapshot's old root (1);
    // the shared child 0 stays held through snap2.
    let g2 = s2.gc();
    assert_eq!(g2.reclaimed, vec![1]);
    assert_eq!(g2.free, vec![1, 4, 5]);
    assert!(reasons_of(0, &g2).contains(&Reason::ReachableFromSnapshot("snap2".to_string())));
    assert!(reasons_of(3, &g2).contains(&Reason::SnapshotRoot("snap2".to_string())));

    // The next reservation reuses exactly the first reclaimed block.
    let w2 = s2.begin_write();
    assert_eq!(s2.reserve(w2).unwrap(), 1);
}
