use row_versioned_table::{Table, TableError};

/// Snapshot of every live column value for one row.
fn snapshot(table: &Table, key: i64) -> Vec<(String, Option<i64>)> {
    table
        .row_values(key)
        .expect("row must exist")
        .into_iter()
        .map(|(name, value)| (name.to_string(), value))
        .collect()
}

// --------------------------------------------------------------------------
// Normal path
// --------------------------------------------------------------------------

#[test]
fn added_column_does_not_rewrite_existing_rows() {
    let mut table = Table::new();
    table.add_column("id", None).unwrap();
    table.insert(1, &[("id", Some(10))]).unwrap();

    let before_slots = table.raw_slots(1).unwrap().to_vec();
    let before_version = table.encoded_version(1).unwrap();

    table.add_column("score", Some(7)).unwrap();

    // Adding is a metadata-only operation for existing rows.
    assert_eq!(table.raw_slots(1).unwrap(), before_slots.as_slice());
    assert_eq!(table.encoded_version(1).unwrap(), before_version);
    assert_eq!(before_slots.len(), 1);

    // The old row has never seen "score", so it reads the add-time default.
    assert_eq!(table.value(1, "score").unwrap(), Some(Some(7)));
}

#[test]
fn old_row_keeps_add_time_default_after_insert_default_changes() {
    let mut table = Table::new();
    table.add_column("id", None).unwrap();
    table.insert(1, &[("id", Some(1))]).unwrap();

    table.add_column("score", Some(7)).unwrap();
    assert_eq!(table.set_insert_default("score", Some(99)).unwrap(), Some(7));

    // Old row still reads the frozen add-time default.
    assert_eq!(table.value(1, "score").unwrap(), Some(Some(7)));

    // A new insert that omits the column uses the insertion-time default.
    table.insert(2, &[("id", Some(2))]).unwrap();
    assert_eq!(table.value(2, "score").unwrap(), Some(Some(99)));

    // An explicit NULL survives and does not turn into a default.
    table.insert(3, &[("id", Some(3)), ("score", None)]).unwrap();
    assert_eq!(table.value(3, "score").unwrap(), Some(None));
}

#[test]
fn insert_omitted_vs_explicit_null_are_distinct() {
    let mut table = Table::new();
    table.add_column("v", Some(5)).unwrap();

    table.insert(1, &[]).unwrap();
    table.insert(2, &[("v", None)]).unwrap();

    assert_eq!(table.value(1, "v").unwrap(), Some(Some(5)));
    assert_eq!(table.value(2, "v").unwrap(), Some(None));
    assert_ne!(table.value(1, "v").unwrap(), table.value(2, "v").unwrap());
}

#[test]
fn explicit_null_from_before_a_later_add_is_preserved() {
    let mut table = Table::new();
    table.add_column("id", None).unwrap();
    table.insert(1, &[("id", None)]).unwrap();

    // Adding a column with a non-null default must not overwrite the
    // pre-existing explicit NULL in the row's "id" slot.
    table.add_column("score", Some(7)).unwrap();
    assert_eq!(table.value(1, "id").unwrap(), Some(None));
    assert_eq!(table.value(1, "score").unwrap(), Some(Some(7)));
}

// --------------------------------------------------------------------------
// Drop / re-create
// --------------------------------------------------------------------------

#[test]
fn dropped_column_physical_value_never_feeds_same_named_replacement() {
    let mut table = Table::new();
    let old_id = table.add_column("tag", Some(1)).unwrap();
    table.insert(1, &[("tag", Some(42))]).unwrap();

    let dropped = table.drop_column("tag").unwrap();
    assert_eq!(dropped, old_id);
    assert!(table.column("tag").is_none());
    assert_eq!(
        table.value(1, "tag"),
        Err(TableError::UnknownColumn("tag".into()))
    );

    // Re-create under the same name; a new identity and a new physical slot.
    let new_id = table.add_column("tag", Some(777)).unwrap();
    assert_ne!(new_id, old_id);
    assert_eq!(table.column("tag").unwrap().slot(), 1);

    // The old physical value 42 is still in slot 0 but must not leak.
    assert_eq!(table.raw_slots(1).unwrap(), &[Some(42)]);
    assert_eq!(table.value(1, "tag").unwrap(), Some(Some(777)));

    // Compacting the old row must still yield the new column's add default,
    // not the retired physical value.
    table.compact_row(1).unwrap();
    assert_eq!(table.value(1, "tag").unwrap(), Some(Some(777)));
    assert_eq!(table.raw_slots(1).unwrap(), &[None, Some(777)]);
}

#[test]
fn dropped_column_rows_inserted_while_absent_use_fresh_schema() {
    let mut table = Table::new();
    table.add_column("a", Some(1)).unwrap();
    table.insert(1, &[("a", Some(11))]).unwrap();
    table.drop_column("a").unwrap();
    table.add_column("b", Some(2)).unwrap();
    table.insert(2, &[("b", Some(22))]).unwrap();

    // Row 2 was written after the drop; retired slot stays NULL.
    assert_eq!(table.raw_slots(2).unwrap(), &[None, Some(22)]);
    assert_eq!(table.value(2, "a"), Err(TableError::UnknownColumn("a".into())));
}

// --------------------------------------------------------------------------
// Compaction
// --------------------------------------------------------------------------

#[test]
fn compacting_one_row_only_changes_that_row() {
    let mut table = Table::new();
    table.add_column("id", None).unwrap();
    table.insert(1, &[("id", Some(1))]).unwrap();
    table.insert(2, &[("id", Some(2))]).unwrap();

    table.add_column("a", Some(10)).unwrap();
    table.add_column("b", Some(20)).unwrap();

    let other_slots = table.raw_slots(2).unwrap().to_vec();
    let other_version = table.encoded_version(2).unwrap();
    let other_values = snapshot(&table, 2);

    let version_before = table.encoded_version(1).unwrap();
    let target_values = snapshot(&table, 1);

    let new_version = table.compact_row(1).unwrap();

    // Only the target row moved to the current layout.
    assert_ne!(version_before, new_version);
    assert_eq!(new_version, table.schema_version());
    assert_eq!(table.encoded_version(1).unwrap(), new_version);
    assert_eq!(table.raw_slots(1).unwrap().len(), table.slot_count());
    assert_eq!(table.raw_slots(1).unwrap(), &[Some(1), Some(10), Some(20)]);

    assert_eq!(table.encoded_version(2).unwrap(), other_version);
    assert_eq!(table.raw_slots(2).unwrap(), other_slots.as_slice());
    assert_eq!(snapshot(&table, 2), other_values);

    // Every column value of the compacted row is unchanged.
    assert_eq!(snapshot(&table, 1), target_values);
}

#[test]
fn compaction_preserves_explicit_null_and_is_idempotent() {
    let mut table = Table::new();
    table.add_column("a", Some(1)).unwrap();
    table.insert(1, &[("a", None)]).unwrap();
    table.add_column("b", Some(2)).unwrap();

    let before = snapshot(&table, 1);
    let v1 = table.compact_row(1).unwrap();
    assert_eq!(snapshot(&table, 1), before);
    assert_eq!(table.raw_slots(1).unwrap(), &[None, Some(2)]);

    // A second compaction at the same schema version is a no-op in value
    // space and keeps the version current.
    let v2 = table.compact_row(1).unwrap();
    assert_eq!(v1, v2);
    assert_eq!(snapshot(&table, 1), before);
}

#[test]
fn interleaved_schema_changes_and_targeted_compactions() {
    let mut table = Table::new();
    table.add_column("id", None).unwrap();
    table.insert(1, &[("id", Some(1))]).unwrap();
    table.insert(2, &[("id", Some(2))]).unwrap();

    table.add_column("x", Some(100)).unwrap();
    table.compact_row(1).unwrap(); // row 1 now knows x = 100

    table.set_insert_default("x", Some(-1)).unwrap();
    table.add_column("y", Some(200)).unwrap();

    // Row 1 (compacted before x's insert-default change) still sees 100.
    // Row 2 (never compacted) still falls back to x's add-time default 100.
    assert_eq!(table.value(1, "x").unwrap(), Some(Some(100)));
    assert_eq!(table.value(2, "x").unwrap(), Some(Some(100)));
    assert_eq!(table.value(1, "y").unwrap(), Some(Some(200)));

    // New insert uses insertion defaults: x = -1, y = 200.
    table.insert(3, &[("id", Some(3))]).unwrap();
    assert_eq!(table.value(3, "x").unwrap(), Some(Some(-1)));
    assert_eq!(table.value(3, "y").unwrap(), Some(Some(200)));

    // Compact rows 2 and 3; values are column-wise equal across compaction.
    for key in [2, 3] {
        let before = snapshot(&table, key);
        table.compact_row(key).unwrap();
        assert_eq!(snapshot(&table, key), before);
    }

    assert_eq!(table.value(2, "x").unwrap(), Some(Some(100)));
    assert_eq!(table.value(3, "x").unwrap(), Some(Some(-1)));
}

#[test]
fn compaction_uses_add_time_default_even_after_insert_default_changes() {
    let mut table = Table::new();
    table.add_column("v", Some(1)).unwrap();
    table.insert(1, &[]).unwrap();
    table.set_insert_default("v", Some(2)).unwrap();

    // Row 1 was inserted at version 1 with a full slot holding 1, so it is
    // already materialised; compaction must keep 1 (not 2).
    table.compact_row(1).unwrap();
    assert_eq!(table.value(1, "v").unwrap(), Some(Some(1)));
}

// --------------------------------------------------------------------------
// Boundary conditions and errors
// --------------------------------------------------------------------------

#[test]
fn empty_table_queries_are_safe() {
    let mut table = Table::new();
    assert_eq!(table.row_count(), 0);
    assert_eq!(table.slot_count(), 0);
    assert_eq!(table.schema_version(), 0);
    assert!(table.get_row(42).is_none());
    assert_eq!(table.raw_slots(42), None);
    assert_eq!(table.encoded_version(42), None);
    assert_eq!(table.row_values(42), None);
    assert_eq!(
        table.compact_row(42),
        Err(TableError::UnknownKey(42))
    );
    assert_eq!(
        table.value(42, "nope"),
        Err(TableError::UnknownColumn("nope".into()))
    );
}

#[test]
fn duplicate_and_unknown_operations_are_rejected() {
    let mut table = Table::new();
    table.add_column("a", None).unwrap();
    assert_eq!(
        table.add_column("a", None),
        Err(TableError::DuplicateColumn("a".into()))
    );
    assert_eq!(
        table.drop_column("missing"),
        Err(TableError::UnknownColumn("missing".into()))
    );
    assert_eq!(
        table.set_insert_default("missing", None),
        Err(TableError::UnknownColumn("missing".into()))
    );

    table.insert(1, &[("a", Some(1))]).unwrap();
    assert_eq!(
        table.insert(1, &[("a", Some(2))]),
        Err(TableError::DuplicateKey(1))
    );
    assert_eq!(
        table.insert(2, &[("missing", Some(1))]),
        Err(TableError::UnknownColumn("missing".into()))
    );
    assert_eq!(
        table.insert(2, &[("a", Some(1)), ("a", Some(2))]),
        Err(TableError::DuplicateValue("a".into()))
    );

    // Failed inserts left the table unchanged.
    assert_eq!(table.row_count(), 1);
    assert_eq!(table.value(1, "a").unwrap(), Some(Some(1)));
}

#[test]
fn explicit_null_survives_insert_default_change_and_compaction() {
    let mut table = Table::new();
    table.add_column("v", Some(1)).unwrap();
    table.insert(1, &[("v", None)]).unwrap();
    table.set_insert_default("v", Some(2)).unwrap();

    assert_eq!(table.value(1, "v").unwrap(), Some(None));
    table.compact_row(1).unwrap();
    assert_eq!(table.value(1, "v").unwrap(), Some(None));
    assert_eq!(table.raw_slots(1).unwrap(), &[None]);
}

#[test]
fn default_of_null_is_frozen_too() {
    let mut table = Table::new();
    table.add_column("a", None).unwrap();
    table.insert(1, &[]).unwrap();

    table.add_column("b", None).unwrap();
    table.set_insert_default("b", Some(8)).unwrap();

    // Row 1 sees b's add-time default (NULL), not the later insertion default.
    assert_eq!(table.value(1, "b").unwrap(), Some(None));

    table.insert(2, &[]).unwrap();
    assert_eq!(table.value(2, "b").unwrap(), Some(Some(8)));
}

#[test]
fn metadata_reports_live_and_retired_columns() {
    let mut table = Table::new();
    let a = table.add_column("a", Some(1)).unwrap();
    let b = table.add_column("b", Some(2)).unwrap();
    table.drop_column("a").unwrap();
    let c = table.add_column("a", Some(3)).unwrap();

    let live: Vec<_> = table.columns().map(|column| column.name().to_string()).collect();
    assert_eq!(live, vec!["b", "a"]);
    assert_eq!(table.column("a").unwrap().id(), c);
    assert_ne!(a, b);
    assert_ne!(a, c);

    let all: Vec<_> = table.all_columns().collect();
    assert_eq!(all.len(), 3);
    assert_eq!(all[0].slot(), 0);
    assert_eq!(all[1].slot(), 1);
    assert_eq!(all[2].slot(), 2);
    assert!(!all[0].is_live());
    assert!(all[2].is_live());
    assert_eq!(all[2].added_default(), Some(3));
    assert_eq!(all[2].added_version(), table.schema_version());
}

#[test]
fn row_view_exposes_version_and_raw_slots() {
    let mut table = Table::new();
    table.add_column("a", Some(1)).unwrap();
    table.insert(7, &[("a", Some(9))]).unwrap();
    table.add_column("b", Some(2)).unwrap();

    let row = table.get_row(7).unwrap();
    assert_eq!(row.key(), 7);
    assert_eq!(row.encoded_version(), 1);
    assert_eq!(row.raw_slots(), &[Some(9)]);
    assert_eq!(row.value("a").unwrap(), Some(9));
    assert_eq!(row.value("b").unwrap(), Some(2));
    assert_eq!(
        row.values(),
        vec![("a", Some(9)), ("b", Some(2))]
    );
}

#[test]
fn multi_column_round_trip_matches_insert() {
    let mut table = Table::new();
    table.add_column("a", Some(-5)).unwrap();
    table.add_column("b", Some(0)).unwrap();
    table.add_column("c", None).unwrap();
    table
        .insert(
            1,
            &[("a", Some(i64::MIN)), ("b", Some(i64::MAX)), ("c", None)],
        )
        .unwrap();

    assert_eq!(
        snapshot(&table, 1),
        vec![
            ("a".to_string(), Some(i64::MIN)),
            ("b".to_string(), Some(i64::MAX)),
            ("c".to_string(), None),
        ]
    );

    table.compact_row(1).unwrap();
    assert_eq!(
        table.raw_slots(1).unwrap(),
        &[Some(i64::MIN), Some(i64::MAX), None]
    );
}
