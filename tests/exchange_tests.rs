//! Acceptance and regression tests for online partition exchange.

use online_partition_exchange::{
    Database, ExchangeError, PrepareState, StaleReason, Violation,
};

fn sorted(mut rows: Vec<(i64, String)>) -> Vec<(i64, String)> {
    rows.sort();
    rows
}

fn v(pairs: &[(i64, &str)]) -> Vec<(i64, String)> {
    pairs
        .iter()
        .map(|&(k, s)| (k, s.to_string()))
        .collect()
}

/// Acceptance scenario from the specification:
/// step-wise validation, an out-of-range row inserted mid-scan, the row is
/// fixed, the exchange commits, and the staging table, the active table and
/// two queries opened at different points in time are all verified.
#[test]
fn stepwise_scan_out_of_range_insert_fix_then_exchange() {
    let db = Database::new();
    let partitions = db.create_active_table(&[(0, 100), (100, 200)]).unwrap();
    let (p1, p2) = (partitions[0], partitions[1]);
    let staging = db.create_staging_table();

    // Seed the active table.
    db.insert_active(10, "a10").unwrap();
    db.insert_active(20, "a20").unwrap();
    db.insert_active(150, "a150").unwrap();

    // Seed the staging table.
    db.staging_insert(staging, 5, "s5").unwrap();
    db.staging_insert(staging, 60, "s60").unwrap();

    // Query Q1 opens before the exchange: it pins the old directory and the
    // old per-partition versions.
    let q1 = db.open_query();
    let epoch_before = q1.directory_epoch();

    // Prepare the exchange of partition [0,100) with the staging table.
    let prepare = db.prepare_exchange(p1, staging).unwrap();

    // Step-wise scan, batch size 1: first row (key 5) validates fine.
    let step1 = db.prepare_scan_step(prepare, 1).unwrap();
    assert_eq!(step1.scanned, 1);
    assert!(!step1.done);
    assert!(step1.violations.is_empty());

    // Mid-scan, a writer inserts an out-of-range row into the staging table
    // (key 150 belongs to partition [100,200), not [0,100)).
    db.staging_insert(staging, 150, "bad").unwrap();

    // The next step scans the remaining rows and must flag key 150.
    let step2 = db.prepare_scan_step(prepare, 100).unwrap();
    assert!(step2.done);
    assert!(step2
        .violations
        .contains(&Violation::OutOfRange {
            key: 150,
            range: online_partition_exchange::KeyRange { start: 0, end: 100 },
        }));
    assert_eq!(db.prepare_state(prepare).unwrap(), PrepareState::Failed);

    // Committing a failed prepare is rejected.
    assert!(matches!(
        db.commit_exchange(prepare),
        Err(ExchangeError::PrepareFailed(p, _)) if p == prepare
    ));

    // Fix the staging data: remove the out-of-range row, add a valid one.
    assert_eq!(db.staging_delete(staging, 150).unwrap(), 1);
    db.staging_insert(staging, 70, "s70").unwrap();

    // Retry the (retryable) prepare record: restart the scan and run it to
    // completion in steps of 2.
    db.prepare_restart(prepare).unwrap();
    loop {
        let step = db.prepare_scan_step(prepare, 2).unwrap();
        if step.done {
            assert!(step.violations.is_empty());
            break;
        }
    }
    assert_eq!(db.prepare_state(prepare).unwrap(), PrepareState::Validated);

    // Writes are still allowed on both sides after the validation baseline.
    db.insert_active(30, "a30").unwrap(); // active-side write
    db.staging_insert(staging, 80, "s80").unwrap(); // staging-side write (delta)

    // Commit: the post-baseline staging insert (key 80) is delta-validated,
    // then the swap happens atomically.
    let receipt = db.commit_exchange(prepare).unwrap();
    assert_eq!(receipt.partition, p1);
    assert_eq!(receipt.staging, staging);
    assert_eq!(receipt.rows_into_partition, 4); // 5, 60, 70, 80
    assert_eq!(receipt.rows_into_staging, 3); // 10, 20, 30
    assert!(receipt.directory_epoch > epoch_before);

    // The prepare record is retryable: committing it again returns the same
    // receipt instead of performing a second swap.
    assert_eq!(db.commit_exchange(prepare).unwrap(), receipt);
    assert_eq!(db.prepare_state(prepare).unwrap(), PrepareState::Committed);

    // Active table after the exchange: partition [0,100) holds the staging
    // rows (including the post-baseline insert), partition [100,200) is
    // untouched.
    assert_eq!(
        db.partition_rows(p1).unwrap(),
        v(&[(5, "s5"), (60, "s60"), (70, "s70"), (80, "s80")])
    );
    assert_eq!(db.partition_rows(p2).unwrap(), v(&[(150, "a150")]));

    // The old active partition became the staging table, including the row
    // written to it during the prepare.
    assert_eq!(
        sorted(db.staging_rows(staging).unwrap()),
        v(&[(10, "a10"), (20, "a20"), (30, "a30")])
    );

    // Q1 (opened before the exchange) still sees the old directory and old
    // versions, fetched in batches without mixing directories.
    let mut cursor = q1.cursor();
    let batch1 = q1.fetch_batch(&mut cursor, 2);
    assert_eq!(batch1, v(&[(10, "a10"), (20, "a20")]));
    // Q1 pinned the data at open time, i.e. before the mid-prepare insert
    // of key 30, so the second batch contains only key 150 of partition 2.
    let batch2 = q1.fetch_batch(&mut cursor, 2);
    assert_eq!(batch2, v(&[(150, "a150")]));
    let batch3 = q1.fetch_batch(&mut cursor, 2);
    assert!(batch3.is_empty());
    assert!(q1.is_exhausted(&cursor));

    // Q2 opens after the exchange: it sees the new directory at once.
    let q2 = db.open_query();
    assert_eq!(q2.directory_epoch(), receipt.directory_epoch);
    assert_eq!(q2.partition_count(), 2);
    assert_eq!(
        q2.all_rows(),
        v(&[
            (5, "s5"),
            (60, "s60"),
            (70, "s70"),
            (80, "s80"),
            (150, "a150"),
        ])
    );
    assert_eq!(q2.rows_in_range(0, 100), v(&[(5, "s5"), (60, "s60"), (70, "s70"), (80, "s80")]));
    assert_eq!(q2.rows_in_range(100, 200), v(&[(150, "a150")]));
}

/// A query opened before an exchange must keep reading the old pinned
/// versions across batches even while the exchange happens mid-fetch.
#[test]
fn batched_query_pinned_across_interleaved_exchange() {
    let db = Database::new();
    let partitions = db.create_active_table(&[(0, 100), (100, 200)]).unwrap();
    let (p1, p2) = (partitions[0], partitions[1]);
    let staging = db.create_staging_table();

    for k in [1, 2, 3, 101, 102] {
        db.insert_active(k, &format!("old{k}")).unwrap();
    }
    for k in [11, 12, 13] {
        db.staging_insert(staging, k, &format!("new{k}")).unwrap();
    }

    let query = db.open_query();
    let mut cursor = query.cursor();
    // First batch fetched before the exchange.
    assert_eq!(
        query.fetch_batch(&mut cursor, 2),
        v(&[(1, "old1"), (2, "old2")])
    );

    // The exchange happens between batches.
    let prepare = db.prepare_exchange(p1, staging).unwrap();
    loop {
        if db.prepare_scan_step(prepare, 4).unwrap().done {
            break;
        }
    }
    db.commit_exchange(prepare).unwrap();
    // Extra writes after the exchange must not leak into the pinned query.
    db.insert_active(4, "late4").unwrap();
    db.insert_active(103, "late103").unwrap();

    // Remaining batches still come from the pinned directory/versions.
    assert_eq!(
        query.fetch_batch(&mut cursor, 2),
        v(&[(3, "old3"), (101, "old101")])
    );
    assert_eq!(query.fetch_batch(&mut cursor, 2), v(&[(102, "old102")]));
    assert!(query.fetch_batch(&mut cursor, 2).is_empty());

    // A freshly opened query sees the exchanged partition and the late writes.
    let fresh = db.open_query();
    assert_eq!(
        fresh.all_rows(),
        v(&[
            (4, "late4"),
            (11, "new11"),
            (12, "new12"),
            (13, "new13"),
            (101, "old101"),
            (102, "old102"),
            (103, "late103"),
        ])
    );
    // The old partition content moved into the staging table.
    assert_eq!(
        sorted(db.staging_rows(staging).unwrap()),
        v(&[(1, "old1"), (2, "old2"), (3, "old3")])
    );
    assert_eq!(db.partition_rows(p2).unwrap(), v(&[(101, "old101"), (102, "old102"), (103, "late103")]));
}

/// A stale prepare must not commit after the partition boundaries changed.
#[test]
fn stale_prepare_rejected_after_boundary_change() {
    let db = Database::new();
    let partitions = db.create_active_table(&[(0, 100), (100, 200)]).unwrap();
    let p1 = partitions[0];
    let staging = db.create_staging_table();
    db.staging_insert(staging, 5, "s5").unwrap();

    let prepare = db.prepare_exchange(p1, staging).unwrap();
    loop {
        if db.prepare_scan_step(prepare, 8).unwrap().done {
            break;
        }
    }
    assert_eq!(db.prepare_state(prepare).unwrap(), PrepareState::Validated);

    // The boundaries move after the prepare was validated (shrink [0,100)
    // to [0,90) so the new range stays disjoint from partition [100,200)).
    db.redefine_partition_range(p1, 0, 90).unwrap();

    assert_eq!(
        db.commit_exchange(prepare),
        Err(ExchangeError::StalePrepare {
            prepare,
            reason: StaleReason::BoundaryChanged,
        })
    );
    // Nothing was exchanged.
    assert_eq!(db.partition_rows(p1).unwrap(), Vec::new());
    assert_eq!(db.staging_rows(staging).unwrap(), v(&[(5, "s5")]));
}

/// A stale prepare must not commit after another operation exchanged the
/// same partition; the committed prepare stays retryable.
#[test]
fn stale_prepare_rejected_after_other_exchange() {
    let db = Database::new();
    let partitions = db.create_active_table(&[(0, 100)]).unwrap();
    let p1 = partitions[0];
    let staging_a = db.create_staging_table();
    let staging_b = db.create_staging_table();
    db.staging_insert(staging_a, 1, "a1").unwrap();
    db.staging_insert(staging_b, 2, "b2").unwrap();

    let prepare_a = db.prepare_exchange(p1, staging_a).unwrap();
    let prepare_b = db.prepare_exchange(p1, staging_b).unwrap();
    for prepare in [prepare_a, prepare_b] {
        loop {
            if db.prepare_scan_step(prepare, 8).unwrap().done {
                break;
            }
        }
    }

    // The first prepare commits; retrying it is idempotent.
    let receipt = db.commit_exchange(prepare_a).unwrap();
    assert_eq!(db.commit_exchange(prepare_a).unwrap(), receipt);

    // The second prepare targeted the pre-exchange partition and is stale.
    assert_eq!(
        db.commit_exchange(prepare_b),
        Err(ExchangeError::StalePrepare {
            prepare: prepare_b,
            reason: StaleReason::PartitionExchangedByOther,
        })
    );
    assert_eq!(db.partition_rows(p1).unwrap(), v(&[(1, "a1")]));
    assert_eq!(db.staging_rows(staging_b).unwrap(), v(&[(2, "b2")]));
}

/// Staging changes after the validation baseline are delta-validated at
/// commit: an out-of-range insert and a duplicate insert are both rejected;
/// after fixing the data the same prepare commits successfully.
#[test]
fn commit_delta_validates_post_baseline_changes() {
    let db = Database::new();
    let partitions = db.create_active_table(&[(0, 100)]).unwrap();
    let p1 = partitions[0];
    let staging = db.create_staging_table();
    db.staging_insert(staging, 1, "s1").unwrap();
    db.staging_insert(staging, 2, "s2").unwrap();

    let prepare = db.prepare_exchange(p1, staging).unwrap();
    loop {
        if db.prepare_scan_step(prepare, 8).unwrap().done {
            break;
        }
    }
    assert_eq!(db.prepare_state(prepare).unwrap(), PrepareState::Validated);

    // Post-baseline out-of-range insert -> commit rejected, prepare stays
    // validated so it can be retried.
    db.staging_insert(staging, 500, "bad").unwrap();
    assert!(matches!(
        db.commit_exchange(prepare),
        Err(ExchangeError::DeltaValidationFailed { prepare: p, ref violations })
            if p == prepare && violations.contains(&Violation::OutOfRange {
                key: 500,
                range: online_partition_exchange::KeyRange { start: 0, end: 100 },
            })
    ));
    assert_eq!(db.prepare_state(prepare).unwrap(), PrepareState::Validated);

    // Fix that, then introduce a post-baseline duplicate of a key that was
    // already validated: the scan cannot see it, the delta check must.
    db.staging_delete(staging, 500).unwrap();
    db.staging_insert(staging, 1, "dup").unwrap();
    assert!(matches!(
        db.commit_exchange(prepare),
        Err(ExchangeError::DeltaValidationFailed { prepare: p, ref violations })
            if p == prepare && violations.contains(&Violation::DuplicateKey { key: 1 })
    ));

    // Fix the duplicate and add a valid post-baseline row; the retried
    // commit succeeds and the new row lands in the partition.
    db.staging_delete(staging, 1).unwrap(); // removes both copies of key 1
    db.staging_insert(staging, 1, "s1-fixed").unwrap();
    db.staging_insert(staging, 3, "s3").unwrap();
    let receipt = db.commit_exchange(prepare).unwrap();
    assert_eq!(receipt.rows_into_partition, 3);
    assert_eq!(
        db.partition_rows(p1).unwrap(),
        v(&[(1, "s1-fixed"), (2, "s2"), (3, "s3")])
    );
}

/// Duplicate keys physically present in the staging heap are detected by the
/// incremental scan.
#[test]
fn scan_detects_duplicate_keys() {
    let db = Database::new();
    let partitions = db.create_active_table(&[(0, 100)]).unwrap();
    let staging = db.create_staging_table();
    db.staging_insert(staging, 7, "first").unwrap();
    db.staging_insert(staging, 7, "second").unwrap();

    let prepare = db.prepare_exchange(partitions[0], staging).unwrap();
    let mut result = db.prepare_scan_step(prepare, 1).unwrap();
    assert!(result.violations.is_empty());
    result = db.prepare_scan_step(prepare, 1).unwrap();
    assert!(result.done);
    assert_eq!(
        result.violations,
        vec![Violation::DuplicateKey { key: 7 }]
    );
    assert_eq!(db.prepare_state(prepare).unwrap(), PrepareState::Failed);
}

/// Boundary cases: exchanging with an empty staging table empties the
/// partition and moves its rows into staging; committing a prepare whose
/// scan never finished is rejected.
#[test]
fn boundary_cases_empty_staging_and_unfinished_scan() {
    let db = Database::new();
    let partitions = db.create_active_table(&[(0, 100)]).unwrap();
    let p1 = partitions[0];
    db.insert_active(1, "a1").unwrap();
    db.insert_active(2, "a2").unwrap();

    // Unfinished scan cannot commit.
    let staging = db.create_staging_table();
    let prepare = db.prepare_exchange(p1, staging).unwrap();
    assert_eq!(
        db.commit_exchange(prepare),
        Err(ExchangeError::PrepareNotValidated(prepare))
    );

    // An empty staging table validates immediately (zero-row scan).
    let step = db.prepare_scan_step(prepare, 10).unwrap();
    assert!(step.done);
    assert_eq!(step.scanned, 0);
    assert_eq!(db.prepare_state(prepare).unwrap(), PrepareState::Validated);

    // Exchange with the empty staging table.
    let receipt = db.commit_exchange(prepare).unwrap();
    assert_eq!(receipt.rows_into_partition, 0);
    assert_eq!(receipt.rows_into_staging, 2);
    assert_eq!(db.partition_rows(p1).unwrap(), Vec::new());
    assert_eq!(sorted(db.staging_rows(staging).unwrap()), v(&[(1, "a1"), (2, "a2")]));

    // A query opened now sees an empty partition.
    assert!(db.open_query().all_rows().is_empty());
}

/// Invalid table definitions and out-of-table writes are rejected.
#[test]
fn invalid_ranges_and_orphan_keys_rejected() {
    let db = Database::new();
    assert!(matches!(
        db.create_active_table(&[(100, 100)]),
        Err(ExchangeError::InvalidRange { .. })
    ));
    assert!(matches!(
        db.create_active_table(&[(0, 100), (50, 150)]),
        Err(ExchangeError::RangeOverlaps { .. })
    ));

    let db = Database::new();
    db.create_active_table(&[(0, 100)]).unwrap();
    assert_eq!(
        db.insert_active(500, "orphan"),
        Err(ExchangeError::NoPartitionForKey(500))
    );
}
