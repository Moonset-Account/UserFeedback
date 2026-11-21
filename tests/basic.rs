//! Basic compaction behaviour: holes, relocation maps, reference rewriting.

use ephemeron_compactor::{collect, Entry, Heap, Object, Table};

/// Build a heap from a list of `Option<Object>` and install roots.
fn heap_of(slots: Vec<Option<Object>>, roots: &[usize]) -> Heap {
    let mut heap = Heap::from_slots(slots);
    heap.set_roots(roots.iter().copied());
    heap
}

#[test]
fn empty_heap_collects_to_empty_heap() {
    let heap = Heap::new();
    let result = collect(&heap).expect("empty heap is valid");

    assert!(result.heap.is_empty());
    assert!(result.heap.roots().is_empty());
    assert!(result.relocation.is_empty());
    assert!(result.reverse_relocation.is_empty());
    assert_eq!(result.stats.live_objects, 0);
    assert_eq!(result.stats.slots_before, 0);
}

#[test]
fn heap_of_only_holes_compacts_to_nothing() {
    let heap = heap_of(vec![None, None, None], &[]);
    let result = collect(&heap).expect("valid heap");

    assert_eq!(result.stats.holes_before, 3);
    assert_eq!(result.relocation, vec![None, None, None]);
    assert!(result.heap.is_empty());
    assert_eq!(result.stats.live_objects, 0);
    assert_eq!(result.stats.reclaimed_objects, 0);
}

#[test]
fn holes_between_live_objects_are_closed_keeping_old_order() {
    // slot 0: live A -> old slot 3
    // slot 1: hole
    // slot 2: dead B
    // slot 3: live C
    // slot 4: hole
    // slot 5: live D
    let heap = heap_of(
        vec![
            Some(Object::new().with_strong(3)),
            None,
            Some(Object::new()),
            Some(Object::new()),
            None,
            Some(Object::new()),
        ],
        &[0, 5],
    );

    let result = collect(&heap).expect("valid heap");

    assert_eq!(result.relocation, vec![Some(0), None, None, Some(1), None, Some(2)]);
    assert_eq!(result.reverse_relocation, vec![0, 3, 5]);
    assert_eq!(result.heap.len(), 3);
    assert_eq!(result.heap.roots(), &[0, 2]);
    assert_eq!(result.stats.live_objects, 3);
    assert_eq!(result.stats.reclaimed_objects, 1);
    assert_eq!(result.stats.holes_before, 2);

    // The surviving strong reference was rewritten through the relocation map.
    assert_eq!(result.heap.object(0).unwrap().strong, vec![1]);
    // Surviving objects must never reference a hole or an out-of-range new slot.
    for object in result.heap.slots().flatten() {
        for &target in &object.strong {
            assert!(result.heap.is_occupied(target));
        }
    }
    assert!(result.heap.dangling_reference_sites().is_empty());
}

#[test]
fn dead_references_to_live_objects_are_erased_and_reported() {
    let heap = heap_of(
        vec![
            Some(Object::new()),
            Some(Object::new().with_strong(0)),
            Some(Object::new()),
        ],
        &[0],
    );

    let result = collect(&heap).expect("valid heap");

    // Slot 1 is dead but points at live slot 0; slot 2 is dead and unreferenced.
    assert_eq!(result.relocation, vec![Some(0), None, None]);
    assert_eq!(result.heap.len(), 1);
    assert_eq!(result.heap.object(0).unwrap().strong, Vec::<usize>::new());
    assert_eq!(
        result.stats.live_objects + result.stats.reclaimed_objects,
        result.stats.slots_before - result.stats.holes_before
    );
}

#[test]
fn roots_that_name_dead_or_vacant_slots_are_dropped_and_counted() {
    let heap = heap_of(
        vec![Some(Object::new()), None, Some(Object::new())],
        &[1, 0, 2, 2],
    );

    let result = collect(&heap).expect("valid heap");

    // Root 1 named a hole; roots 0,2,2 survive; duplicates are preserved.
    assert_eq!(result.stats.roots_dropped, 1);
    assert_eq!(result.heap.roots(), &[0, 1, 1]);
    assert_eq!(result.relocated(2), Some(1));
}

#[test]
fn dangling_references_are_erased_and_counted() {
    let heap = heap_of(
        vec![
            Some(Object::new().with_strong(1)),
            None,
            Some(Object::new().with_table(Table::new().with_entry(1, 2))),
        ],
        &[0, 2],
    );

    assert!(!heap.dangling_reference_sites().is_empty());
    let result = collect(&heap).expect("valid heap");

    // Dangling: strong ref 0 -> hole 1, and the table key pointing at hole 1.
    // (The entry's value is slot 2, which is occupied.)
    assert_eq!(result.stats.dangling_references, 2);
    assert_eq!(result.heap.object(0).unwrap().strong, Vec::<usize>::new());
    // The only table entry's key was a hole, so the entry is removed.
    assert_eq!(result.heap.object(1).unwrap().tables[0].entries, vec![]);
    assert_eq!(result.stats.entries_dropped, 1);
}

#[test]
fn duplicate_strong_references_are_preserved_after_rewriting() {
    let heap = heap_of(
        vec![Some(Object::new().with_strongs([2, 2, 2])), None, Some(Object::new())],
        &[0, 2],
    );

    let result = collect(&heap).expect("valid heap");

    assert_eq!(result.relocation, vec![Some(0), None, Some(1)]);
    assert_eq!(result.heap.object(0).unwrap().strong, vec![1, 1, 1]);
}

#[test]
fn rooted_cycle_survives_and_dead_cycle_is_reclaimed() {
    let heap = heap_of(
        vec![
            // live pair 0 <-> 2
            Some(Object::new().with_strong(2)),
            None,
            Some(Object::new().with_strong(0)),
            // dead pair 3 <-> 4
            Some(Object::new().with_strong(4)),
            Some(Object::new().with_strong(3)),
        ],
        &[0],
    );

    let result = collect(&heap).expect("valid heap");

    assert_eq!(result.relocation, vec![Some(0), None, Some(1), None, None]);
    assert_eq!(result.heap.object(0).unwrap().strong, vec![1]);
    assert_eq!(result.heap.object(1).unwrap().strong, vec![0]);
    assert_eq!(result.stats.reclaimed_objects, 2);
}

#[test]
fn self_reference_is_preserved() {
    let heap = heap_of(vec![Some(Object::new().with_strong(0))], &[0]);
    let result = collect(&heap).expect("valid heap");

    assert_eq!(result.relocation, vec![Some(0)]);
    assert_eq!(result.heap.object(0).unwrap().strong, vec![0]);
}

#[test]
fn relocation_helpers_agree_with_maps() {
    let heap = heap_of(
        vec![Some(Object::new()), None, Some(Object::new()), None],
        &[2],
    );
    let result = collect(&heap).expect("valid heap");

    assert_eq!(result.relocated(0), None);
    assert_eq!(result.relocated(2), Some(0));
    assert_eq!(result.relocated(4), None);
    assert_eq!(result.original(0), Some(2));
    assert_eq!(result.original(1), None);
}

#[test]
fn out_of_bounds_reference_is_an_error() {
    let heap = heap_of(vec![Some(Object::new().with_strong(7))], &[0]);
    let err = collect(&heap).unwrap_err();
    assert_eq!(
        err,
        ephemeron_compactor::HeapError::ReferenceOutOfBounds {
            site: ephemeron_compactor::RefSite::Strong { from: 0, index: 0 },
            len: 1,
        }
    );
    assert!(err.to_string().contains("out of bounds"));
}

#[test]
fn out_of_bounds_table_key_and_value_are_errors() {
    let key_only = heap_of(
        vec![Some(Object::new().with_table(Table::new().with_entry(9, 0)))],
        &[0],
    );
    assert!(matches!(
        collect(&key_only),
        Err(ephemeron_compactor::HeapError::ReferenceOutOfBounds { site, .. })
            if matches!(site, ephemeron_compactor::RefSite::TableKey { .. })
    ));

    let value_only = heap_of(
        vec![Some(Object::new().with_table(Table::new().with_entry(0, 9)))],
        &[0],
    );
    assert!(matches!(
        collect(&value_only),
        Err(ephemeron_compactor::HeapError::ReferenceOutOfBounds { site, .. })
            if matches!(site, ephemeron_compactor::RefSite::TableValue { .. })
    ));
}

#[test]
fn out_of_bounds_root_is_an_error() {
    let heap = heap_of(vec![Some(Object::new())], &[0, 3]);
    assert_eq!(
        collect(&heap).unwrap_err(),
        ephemeron_compactor::HeapError::RootOutOfBounds { slot: 3, len: 1 }
    );
}

#[test]
fn dead_reference_sites_report_dead_holders_pointing_at_live_objects() {
    use std::collections::BTreeSet;

    let heap = heap_of(
        vec![
            Some(Object::new().with_strong(2)),                                  // 0 dead, -> live 2
            Some(Object::new().with_table(Table::new().with_entry(2, 2))),       // 1 dead holder
            Some(Object::new()),                                                 // 2 live
        ],
        &[2],
    );

    let live: BTreeSet<usize> = collect(&heap).unwrap().reverse_relocation.into_iter().collect();
    assert_eq!(live, BTreeSet::from([2]));
    let sites = heap.dead_reference_sites(&live);
    assert!(sites.contains(&ephemeron_compactor::RefSite::Strong { from: 0, index: 0 }));
    assert!(sites.contains(&ephemeron_compactor::RefSite::TableValue {
        from: 1,
        table: 0,
        index: 0,
    }));
    // Keys of dead holders are not reported.
    assert!(!sites.iter().any(|s| matches!(
        s,
        ephemeron_compactor::RefSite::TableKey { .. }
    )));
}

#[test]
fn retain_live_keys_filters_by_key() {
    let mut table = Table::new()
        .with_entry(0, 5)
        .with_entry(1, 6)
        .with_entry(2, 7);
    table.retain_live_keys(|key| key != 1);
    assert_eq!(table.entries, vec![Entry::new(0, 5), Entry::new(2, 7)]);
}

#[test]
fn visit_value_targets_visits_strong_refs_and_entry_values_not_keys() {
    let object = Object::new()
        .with_strong(9)
        .with_table(Table::new().with_entry(3, 4));
    let mut visited = Vec::new();
    object.visit_value_targets(|slot| visited.push(slot));
    assert_eq!(visited, vec![9, 4]);
}

#[test]
fn heap_builder_extends_with_holes() {
    let mut heap = Heap::new();
    heap.set_slot(2, Some(Object::new()));
    assert_eq!(heap.len(), 3);
    assert!(!heap.is_occupied(0));
    assert!(heap.is_occupied(2));

    let hole = heap.push_hole();
    assert_eq!(hole, 3);
    assert_eq!(heap.push(Object::new()), 4);

    heap.set_roots([2, 4]);
    assert_eq!(heap.roots(), &[2, 4]);
    let result = collect(&heap).expect("valid heap");
    assert_eq!(result.relocation, vec![None, None, Some(0), None, Some(1)]);
}

#[test]
fn table_entry_with_duplicate_pairs_is_preserved_when_live() {
    let table = Table::new()
        .with_entry(1, 2)
        .with_entry(1, 2)
        .with_entries([Entry::new(3, 2)]);
    let heap = heap_of(
        vec![
            Some(Object::new().with_table(table)),
            Some(Object::new()),
            Some(Object::new()),
            Some(Object::new()),
        ],
        &[0, 1, 3],
    );

    let result = collect(&heap).expect("valid heap");
    // All three entries have live keys (1, 1, 3), so all three survive; the
    // duplicated pair is not deduplicated by the collector.
    assert_eq!(
        result.heap.object(0).unwrap().tables[0].entries,
        vec![Entry::new(1, 2), Entry::new(1, 2), Entry::new(3, 2)]
    );
    assert_eq!(result.stats.entries_dropped, 0);
    assert_eq!(result.heap.len(), 4);
}
