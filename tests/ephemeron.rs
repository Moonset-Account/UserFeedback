//! Ephemeron semantics: weak keys, value-driven key promotion, cycles.

use ephemeron_compactor::{collect, Entry, Heap, Object, Table};

fn heap_of(slots: Vec<Option<Object>>, roots: &[usize]) -> Heap {
    let mut heap = Heap::from_slots(slots);
    heap.set_roots(roots.iter().copied());
    heap
}

fn hole() -> Option<Object> {
    None
}

#[test]
fn holes_inside_a_table_and_between_holders_are_handled() {
    // 0: hole;  1: holder (rooted), entries (2 -> 5), (3 -> 6)
    // 2: hole;  3: key (rooted);  4: hole
    // 5: value (-> 3);  6: value whose key is a hole
    let heap = heap_of(
        vec![
            hole(),
            Some(holder(&[(2, 5), (3, 6)])),
            hole(),
            Some(Object::new()),
            hole(),
            Some(Object::new().with_strong(3)),
            Some(Object::new()),
        ],
        &[1, 3],
    );

    let result = collect(&heap).expect("valid heap");

    // Survivors: holder 1, key 3 and value 6, in that old-slot order.
    assert_eq!(result.relocated(1), Some(0));
    assert_eq!(result.relocated(3), Some(1));
    // Entry (2 -> 5) loses its key (a hole); entry (3 -> 6) survives and is
    // rewritten key 3 -> 1, value 6 -> 2.
    assert_eq!(result.stats.live_objects, 3);
    assert_eq!(
        result.heap.object(0).unwrap().tables[0].entries,
        vec![Entry::new(1, 2)]
    );
    assert_eq!(result.stats.entries_dropped, 1);
    // Value 5 is not reachable; value 6 is.
    assert_eq!(result.relocated(5), None);
    assert_eq!(result.relocated(6), Some(2));
}

fn holder(entries: &[(usize, usize)]) -> Object {
    Object::new().with_table(
        Table::new().with_entries(entries.iter().map(|&(k, v)| Entry::new(k, v))),
    )
}

/// Sorted copy of the only table's entries, for order-insensitive comparison.
fn sorted_entries(heap: &Heap, slot: usize) -> Vec<Entry> {
    let mut entries = heap.object(slot).unwrap().tables[0].entries.clone();
    entries.sort();
    entries
}

#[test]
fn value_survives_when_holder_and_key_are_live() {
    // 0: holder, 1: key (rooted), 2: value
    let heap = heap_of(
        vec![Some(holder(&[(1, 2)])), Some(Object::new()), Some(Object::new())],
        &[0, 1],
    );

    let result = collect(&heap).expect("valid heap");

    assert_eq!(result.stats.live_objects, 3);
    assert_eq!(result.relocation, vec![Some(0), Some(1), Some(2)]);
    assert_eq!(
        result.heap.object(0).unwrap().tables[0].entries,
        vec![Entry::new(1, 2)]
    );
    assert_eq!(result.stats.entries_dropped, 0);
}

#[test]
fn value_is_dead_when_key_is_unreachable() {
    // 0: holder (rooted), 1: unrooted key, 2: value
    let heap = heap_of(
        vec![Some(holder(&[(1, 2)])), Some(Object::new()), Some(Object::new())],
        &[0],
    );

    let result = collect(&heap).expect("valid heap");

    assert_eq!(result.relocation, vec![Some(0), None, None]);
    assert_eq!(result.heap.len(), 1);
    // Dead key => entry dropped from the surviving holder.
    assert_eq!(result.heap.object(0).unwrap().tables[0].entries, vec![]);
    assert_eq!(result.stats.entries_dropped, 1);
    assert_eq!(result.stats.reclaimed_objects, 2);
}

#[test]
fn value_is_dead_when_holder_is_unreachable_even_if_key_is_live() {
    // 0: unrooted holder, 1: rooted key, 2: value
    let heap = heap_of(
        vec![Some(holder(&[(1, 2)])), Some(Object::new()), Some(Object::new())],
        &[1],
    );

    let result = collect(&heap).expect("valid heap");

    assert_eq!(result.relocation, vec![None, Some(0), None]);
    assert_eq!(result.heap.len(), 1);
    assert!(result.heap.object(0).unwrap().tables.is_empty());
    assert_eq!(result.stats.reclaimed_objects, 2);
}

#[test]
fn value_strong_reference_keeps_another_tables_key_alive() {
    // 0: rooted holder H1 with entry (1 -> 3)
    // 1: key K1, rooted
    // 2: holder H2, rooted, entry (4 -> 5)
    // 3: value V1, strongly references 4 (K2)
    // 4: key K2, otherwise unreachable
    // 5: value V2
    let heap = heap_of(
        vec![
            Some(holder(&[(1, 3)])),
            Some(Object::new()),
            Some(holder(&[(4, 5)])),
            Some(Object::new().with_strong(4)),
            Some(Object::new()),
            Some(Object::new()),
        ],
        &[0, 1, 2],
    );

    let result = collect(&heap).expect("valid heap");

    assert_eq!(result.stats.live_objects, 6);
    assert_eq!(result.relocation, vec![Some(0), Some(1), Some(2), Some(3), Some(4), Some(5)]);
    // Both entries survived and were rewritten (identity here).
    assert_eq!(result.heap.object(0).unwrap().tables[0].entries, vec![Entry::new(1, 3)]);
    assert_eq!(result.heap.object(2).unwrap().tables[0].entries, vec![Entry::new(4, 5)]);
    assert_eq!(result.heap.object(3).unwrap().strong, vec![4]);
}

#[test]
fn value_references_are_rewritten_through_the_relocation_map() {
    // Same as above but with a dead hole in front so relocation is non-trivial.
    // Slot 0 is a hole; the structure shifts by one.
    let heap = heap_of(
        vec![
            None,
            Some(holder(&[(2, 4)])), // old 1 -> new 0
            Some(Object::new()),     // old 2 -> new 1, key
            Some(holder(&[(5, 6)])), // old 3 -> new 2
            Some(Object::new().with_strong(5)), // old 4 -> new 3
            Some(Object::new()),     // old 5 -> new 4
            Some(Object::new()),     // old 6 -> new 5
        ],
        &[1, 2, 3],
    );

    let result = collect(&heap).expect("valid heap");

    assert_eq!(
        result.relocation,
        vec![None, Some(0), Some(1), Some(2), Some(3), Some(4), Some(5)]
    );
    assert_eq!(result.heap.object(0).unwrap().tables[0].entries, vec![Entry::new(1, 3)]);
    assert_eq!(result.heap.object(2).unwrap().tables[0].entries, vec![Entry::new(4, 5)]);
    assert_eq!(result.heap.object(3).unwrap().strong, vec![4]);
}

#[test]
fn three_link_chain_propagates_regardless_of_slot_order() {
    // H0 at 0: (1 -> 6);  6 strongly refs 3
    // H1 at 2: (3 -> 7);  7 strongly refs 5
    // H2 at 4: (5 -> 8)
    // Only the first key (1) is rooted; the chain must pull the rest in.
    let heap = heap_of(
        vec![
            Some(holder(&[(1, 6)])),
            Some(Object::new()),
            Some(holder(&[(3, 7)])),
            Some(Object::new()),
            Some(holder(&[(5, 8)])),
            Some(Object::new()),
            Some(Object::new().with_strong(3)),
            Some(Object::new().with_strong(5)),
            Some(Object::new()),
        ],
        &[0, 1, 2, 4],
    );

    let result = collect(&heap).expect("valid heap");

    assert_eq!(result.stats.live_objects, 9);
    assert_eq!(
        result.relocation,
        vec![Some(0), Some(1), Some(2), Some(3), Some(4), Some(5), Some(6), Some(7), Some(8)]
    );
    assert_eq!(result.stats.entries_dropped, 0);
}

#[test]
fn two_keys_supporting_each_other_only_through_values_are_both_collected() {
    // Table at 0: entries (1 -> 3) and (2 -> 4)
    // Slot 0: holder, rooted.
    // Slot 1: key K1 (dead on its own)
    // Slot 2: key K2 (dead on its own)
    // Slot 3: value V1 strongly references K2 (slot 2)
    // Slot 4: value V2 strongly references K1 (slot 1)
    let heap = heap_of(
        vec![
            Some(holder(&[(1, 3), (2, 4)])),
            Some(Object::new()),
            Some(Object::new()),
            Some(Object::new().with_strong(2)),
            Some(Object::new().with_strong(1)),
        ],
        &[0],
    );

    let result = collect(&heap).expect("valid heap");

    // Neither key is rooted, so neither value can be promoted; the mutual
    // support of the two values must not keep anything alive.
    assert_eq!(result.relocation, vec![Some(0), None, None, None, None]);
    assert_eq!(result.heap.len(), 1);
    assert_eq!(result.heap.object(0).unwrap().tables[0].entries, vec![]);
    assert_eq!(result.stats.entries_dropped, 2);
    assert_eq!(result.stats.reclaimed_objects, 4);
}

#[test]
fn two_tables_whose_values_support_each_others_keys_are_both_collected() {
    // Cross-table version of the mutual-support case.
    // 0: holder H1 (rooted), entry (1 -> 3)
    // 2: holder H2 (rooted), entry (4 -> 5)
    // 1: key K1, dead;  4: key K2, dead
    // 3: V1 strong-> 4 (K2);  5: V2 strong-> 1 (K1)
    let heap = heap_of(
        vec![
            Some(holder(&[(1, 3)])),
            Some(Object::new()),
            Some(holder(&[(4, 5)])),
            Some(Object::new().with_strong(4)),
            Some(Object::new()),
            Some(Object::new().with_strong(1)),
        ],
        &[0, 2],
    );

    let result = collect(&heap).expect("valid heap");

    assert_eq!(result.relocation, vec![Some(0), None, Some(1), None, None, None]);
    assert_eq!(result.heap.len(), 2);
    assert_eq!(result.heap.object(0).unwrap().tables[0].entries, vec![]);
    assert_eq!(result.heap.object(1).unwrap().tables[0].entries, vec![]);
    assert_eq!(result.stats.reclaimed_objects, 4);
}

#[test]
fn weak_key_table_does_not_keep_its_key_alive_via_value_cycle() {
    // A weak table must not retain its key even when the value points back.
    let heap = heap_of(
        vec![
            Some(holder(&[(1, 2)])),
            Some(Object::new()),
            Some(Object::new().with_strong(1)),
        ],
        &[0],
    );

    let result = collect(&heap).expect("valid heap");

    assert_eq!(result.relocation, vec![Some(0), None, None]);
    assert_eq!(result.heap.object(0).unwrap().tables[0].entries, vec![]);
}

#[test]
fn self_keyed_entry_is_collected() {
    // Holder 0 has an entry whose key is itself (slot 0) and value slot 1.
    // The holder is live, so the key is live, so the value survives.
    let heap = heap_of(vec![Some(holder(&[(0, 1)])), Some(Object::new())], &[0]);
    let result = collect(&heap).expect("valid heap");

    assert_eq!(result.relocation, vec![Some(0), Some(1)]);
    assert_eq!(result.heap.object(0).unwrap().tables[0].entries, vec![Entry::new(0, 1)]);
}

#[test]
fn entry_pointing_at_its_own_holder_keeps_holder_only_if_keyed() {
    // Holder 0 rooted, entry (1 -> 0) with key 1 dead: value 0 already live.
    let heap = heap_of(vec![Some(holder(&[(1, 0)])), Some(Object::new())], &[0]);
    let result = collect(&heap).expect("valid heap");

    assert_eq!(result.relocation, vec![Some(0), None]);
    assert_eq!(result.stats.entries_dropped, 1);
    assert_eq!(result.heap.object(0).unwrap().tables[0].entries, vec![]);
}

/// Canonical heap used by the input-order invariance tests.
///
/// * 0: holder A (rooted), entries `(1 -> 4)` and `(2 -> 5)`
/// * 1: key K1 (rooted)
/// * 2: key K2 (dead)
/// * 3: holder B (rooted), entries `(6 -> 7)` and `(1 -> 8)`
/// * 4: V1, strong `-> 6`
/// * 5: V2 (only reachable through the dead-key entry)
/// * 6: K3 (reachable only because V1 points at it)
/// * 7: V3
/// * 8: V4
///
/// Live set: `{0, 1, 3, 4, 6, 7, 8}`; dead: `{2, 5}`.
fn two_table_heap(a_entries: &[(usize, usize)], b_entries: &[(usize, usize)]) -> Heap {
    let mut slots: Vec<Option<Object>> = (0..9).map(|_| Some(Object::new())).collect();
    slots[0] = Some(holder(a_entries));
    slots[3] = Some(holder(b_entries));
    slots[4] = Some(Object::new().with_strong(6));
    heap_of(slots, &[0, 1, 3])
}

/// Expected result shared by every entry ordering of [`two_table_heap`].
fn assert_two_table_outcome(result: &ephemeron_compactor::Collection) {
    assert_eq!(
        result.relocation,
        vec![Some(0), Some(1), None, Some(2), Some(3), None, Some(4), Some(5), Some(6)]
    );
    assert_eq!(result.reverse_relocation, vec![0, 1, 3, 4, 6, 7, 8]);
    assert_eq!(result.heap.roots(), &[0, 1, 2]);
    assert_eq!(result.stats.live_objects, 7);
    assert_eq!(result.stats.reclaimed_objects, 2);
    assert_eq!(result.stats.entries_dropped, 1);
}

#[test]
fn reversing_records_of_both_tables_leaves_reachability_identical() {
    let forward = collect(&two_table_heap(
        &[(1, 4), (2, 5)],
        &[(6, 7), (1, 8)],
    ))
    .expect("valid heap");

    let reversed = collect(&two_table_heap(
        &[(2, 5), (1, 4)],
        &[(1, 8), (6, 7)],
    ))
    .expect("valid heap");

    assert_two_table_outcome(&forward);
    assert_two_table_outcome(&reversed);

    // Only the surviving entry of table A remains, rewritten in place.
    assert_eq!(sorted_entries(&forward.heap, 0), vec![Entry::new(1, 3)]);
    assert_eq!(sorted_entries(&reversed.heap, 0), vec![Entry::new(1, 3)]);

    // Table B keeps both entries; both tables' surviving sets match.
    assert_eq!(
        sorted_entries(&forward.heap, 2),
        vec![Entry::new(1, 6), Entry::new(4, 5)]
    );
    assert_eq!(
        sorted_entries(&forward.heap, 2),
        sorted_entries(&reversed.heap, 2)
    );

    // Strong reference of V1 was rewritten (old 4 -> new 3, target 6 -> new 4).
    assert_eq!(forward.heap.object(3).unwrap().strong, vec![4]);
    assert_eq!(reversed.heap.object(3).unwrap().strong, vec![4]);
}

#[test]
fn same_structure_with_reordered_entries_yields_identical_relocation() {
    // Same as above, expressed with an explicit `Table` so the two input
    // orders are visibly "records in opposite order".
    let make = |a: &[Entry], b: &[Entry]| {
        let mut slots: Vec<Option<Object>> = (0..9).map(|_| Some(Object::new())).collect();
        slots[0] = Some(Object::new().with_table(Table::new().with_entries(a.to_vec())));
        slots[3] = Some(Object::new().with_table(Table::new().with_entries(b.to_vec())));
        slots[4] = Some(Object::new().with_strong(6));
        heap_of(slots, &[0, 1, 3])
    };

    let a = [Entry::new(1, 4), Entry::new(2, 5)];
    let b = [Entry::new(6, 7), Entry::new(1, 8)];
    let a_rev: Vec<Entry> = a.iter().rev().copied().collect();
    let b_rev: Vec<Entry> = b.iter().rev().copied().collect();

    let forward = collect(&make(&a, &b)).expect("valid");
    let reversed = collect(&make(&a_rev, &b_rev)).expect("valid");

    assert_two_table_outcome(&forward);
    assert_two_table_outcome(&reversed);
    assert_eq!(forward.relocation, reversed.relocation);
    assert_eq!(forward.heap.roots(), reversed.heap.roots());
}

#[test]
fn chained_holdings_are_resolved_regardless_of_slot_order() {
    // Holders earlier than their keys in slot order, and vice versa, must give
    // identical results. A single traversal pass is not enough, so the
    // worklist must revisit.
    let mut slots: Vec<Option<Object>> = vec![Some(Object::new()); 8];
    slots[2] = Some(holder(&[(3, 6)]));
    slots[0] = Some(holder(&[(4, 7)]));
    slots[1] = Some(holder(&[(5, 3)]));
    // 6 -> 4, 7 -> 5 drive further promotion.
    slots[6] = Some(Object::new().with_strong(4));
    slots[7] = Some(Object::new().with_strong(5));
    // 3, 4 and 5 are keys; root holder 0 and the first key (5).
    let heap = heap_of(slots, &[0, 1, 2, 5]);

    let result = collect(&heap).expect("valid heap");

    assert_eq!(result.stats.live_objects, 8);
    assert_eq!(
        result.relocation,
        vec![Some(0), Some(1), Some(2), Some(3), Some(4), Some(5), Some(6), Some(7)]
    );
    assert_eq!(result.heap.object(1).unwrap().tables[0].entries, vec![Entry::new(5, 3)]);
    assert_eq!(result.heap.object(2).unwrap().tables[0].entries, vec![Entry::new(3, 6)]);
    assert_eq!(result.heap.object(0).unwrap().tables[0].entries, vec![Entry::new(4, 7)]);
}
