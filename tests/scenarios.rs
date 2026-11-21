//! End-to-end scenarios, including dropping the root at the head of a chain.

use ephemeron_compactor::{collect, Entry, Heap, Object, Table};

fn heap_of(slots: Vec<Option<Object>>, roots: &[usize]) -> Heap {
    let mut heap = Heap::from_slots(slots);
    heap.set_roots(roots.iter().copied());
    heap
}

fn holder(entries: &[(usize, usize)]) -> Object {
    Object::new().with_table(
        Table::new().with_entries(entries.iter().map(|&(k, v)| Entry::new(k, v))),
    )
}

/// The two-table heap described in `tests/ephemeron.rs`, replicated here so the
/// scenario stands on its own.
///
/// * 0: holder A (rooted), entries `(1 -> 4)`, `(2 -> 5)`
/// * 1: key K1 (rooted)
/// * 2: key K2 (unrooted)
/// * 3: holder B (rooted), entries `(6 -> 7)`, `(1 -> 8)`
/// * 4: V1, strong `-> 6`
/// * 5: V2
/// * 6: K3
/// * 7: V3
/// * 8: V4
fn chain_heap(roots: &[usize]) -> Heap {
    let mut slots: Vec<Option<Object>> = (0..9).map(|_| Some(Object::new())).collect();
    slots[0] = Some(holder(&[(1, 4), (2, 5)]));
    slots[3] = Some(holder(&[(6, 7), (1, 8)]));
    slots[4] = Some(Object::new().with_strong(6));
    heap_of(slots, roots)
}

#[test]
fn chain_head_root_present_keeps_the_whole_chain() {
    let result = collect(&chain_heap(&[0, 1, 3])).expect("valid heap");

    assert_eq!(result.stats.live_objects, 7);
    assert_eq!(
        result.relocation,
        vec![Some(0), Some(1), None, Some(2), Some(3), None, Some(4), Some(5), Some(6)]
    );
    assert_eq!(result.heap.roots(), &[0, 1, 2]);
    // The entry (2 -> 5) is dropped because key 2 is dead; the other three
    // entries survive, their slots rewritten.
    assert_eq!(
        result.heap.object(0).unwrap().tables[0].entries,
        vec![Entry::new(1, 3)]
    );
    assert_eq!(
        result.heap.object(2).unwrap().tables[0].entries,
        vec![Entry::new(4, 5), Entry::new(1, 6)]
    );
}

#[test]
fn removing_the_chain_head_root_releases_everything_it_upheld() {
    // Same heap as `chain_head_root_present_keeps_the_whole_chain`, but the
    // root naming K1 (slot 1) is withdrawn. K1 was the head that made V1 live,
    // and V1's strong reference to K3 was what kept the second table's value
    // alive. Both must now be reclaimed.
    let result = collect(&chain_heap(&[0, 3])).expect("valid heap");

    assert_eq!(result.relocation, vec![Some(0), None, None, Some(1), None, None, None, None, None]);
    assert_eq!(result.reverse_relocation, vec![0, 3]);
    assert_eq!(result.stats.live_objects, 2);
    assert_eq!(result.stats.reclaimed_objects, 7);
    assert_eq!(result.heap.len(), 2);
    assert_eq!(result.heap.roots(), &[0, 1]);

    // Every entry whose key was on the released chain is gone, including the
    // ones whose own value happened to still be reachable in isolation.
    for slot in 0..result.heap.len() {
        assert_eq!(result.heap.object(slot).unwrap().tables[0].entries, vec![]);
    }
    assert_eq!(result.stats.entries_dropped, 4);
}

#[test]
fn collection_is_idempotent() {
    let first = collect(&chain_heap(&[0, 1, 3])).expect("valid heap");
    let second = collect(&first.heap).expect("collected heap is valid");

    assert_eq!(second.heap, first.heap);
    assert!(second.relocation.iter().all(|&m| m == Some(m.unwrap())));
    assert_eq!(second.stats.live_objects, first.heap.len());
    assert_eq!(second.stats.reclaimed_objects, 0);
    assert_eq!(second.stats.entries_dropped, 0);
}

#[test]
fn a_second_collection_after_root_removal_reclaims_nothing_more() {
    let first = collect(&chain_heap(&[0, 1, 3])).expect("valid heap");
    let mut roots = first.heap.roots().to_vec();
    // Drop the root that moved to new slot 1 (old slot 1, key K1).
    roots.retain(|&r| r != 1);
    let mut trimmed = first.heap.clone();
    trimmed.set_roots(roots.clone());

    let second = collect(&trimmed).expect("valid heap");
    assert_eq!(roots, vec![0, 2]);
    // The two holders are themselves rooted, so they survive and shift down to
    // new slots 0 and 1; everything that hung off K1 is gone.
    assert_eq!(second.heap.roots(), vec![0, 1]);
    assert_eq!(second.stats.live_objects, 2);
    assert_eq!(
        second.relocation,
        vec![Some(0), None, Some(1), None, None, None, None]
    );
    // Holder A's single entry and holder B's two entries all lost their keys.
    assert_eq!(second.stats.entries_dropped, 3);
}

#[test]
fn compaction_rewrites_all_three_kinds_of_reference_together() {
    // Layout with a leading hole and a dead object in the middle, so that
    // every surviving reference changes index.
    // old -> new: 1->0 holder, 2->1 key, 3->2 value-with-strong-to-4, 4->3 key2
    let heap = heap_of(
        vec![
            None,
            Some(holder(&[(2, 3), (4, 3)])),
            Some(Object::new()),
            Some(Object::new().with_strong(4)),
            Some(Object::new()),
            Some(Object::new()), // dead tail
        ],
        &[1, 2],
    );

    let result = collect(&heap).expect("valid heap");

    assert_eq!(
        result.relocation,
        vec![None, Some(0), Some(1), Some(2), Some(3), None]
    );
    let holder = result.heap.object(0).unwrap();
    assert_eq!(
        holder.tables[0].entries,
        vec![Entry::new(1, 2), Entry::new(3, 2)]
    );
    assert_eq!(result.heap.object(2).unwrap().strong, vec![3]);

    // No surviving reference may point outside the compacted heap.
    assert!(result.heap.dangling_reference_sites().is_empty());
    for object in result.heap.slots().flatten() {
        for &target in &object.strong {
            assert!(target < result.heap.len());
        }
        for table in &object.tables {
            for entry in &table.entries {
                assert!(entry.key < result.heap.len());
                assert!(entry.value < result.heap.len());
            }
        }
    }
}

#[test]
fn stats_account_for_every_input_slot() {
    let heap = chain_heap(&[0, 1, 3]);
    let result = collect(&heap).expect("valid heap");

    assert_eq!(
        result.stats.live_objects + result.stats.reclaimed_objects + result.stats.holes_before,
        result.stats.slots_before
    );
    assert_eq!(result.relocation.len(), result.stats.slots_before);
    assert_eq!(result.reverse_relocation.len(), result.stats.live_objects);
    assert_eq!(result.heap.len(), result.stats.live_objects);
    assert_eq!(
        result.relocation.iter().filter(|m| m.is_some()).count(),
        result.stats.live_objects
    );
}

#[test]
fn reverse_relocation_inverts_relocation_for_survivors() {
    let result = collect(&chain_heap(&[0, 1, 3])).expect("valid heap");

    for (old, mapped) in result.relocation.iter().enumerate() {
        match mapped {
            Some(new) => assert_eq!(result.reverse_relocation[*new], old),
            None => assert!(!result.reverse_relocation.contains(&old)),
        }
    }
}
