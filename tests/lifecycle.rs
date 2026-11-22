//! Acceptance tests for the async I/O slot/generation lifecycle.
//!
//! Scenarios map directly to the problem statement:
//! * normal multi-segment completion with buffer loans and returns;
//! * cancel receipt arriving before the final completion, with a late device
//!   buffer reclaimed while the consumer still holds an earlier buffer;
//! * stale old-generation events after slot reuse;
//! * duplicate completions/returns never adding balance;
//! * boundary conditions (slot exhaustion, out-of-range ids);
//! * capacity changes traceable to actual returns via the event log.

use io_slot_runtime::{Event, IoRuntime, RequestHandle, SlotState, Step};

/// Normal path: one request, several completion segments, consumer returns,
/// final completion, slot reuse with a bumped generation.
#[test]
fn normal_multi_segment_lifecycle() {
    let mut rt = IoRuntime::new(2, 4);

    let h = rt.submit().expect("slot available");
    assert_eq!(h, RequestHandle { slot: 0, generation: 1 });
    assert_eq!(rt.slot_state(0), Some(SlotState::Active));

    // One request produces multiple completions; each loans a buffer.
    rt.complete(h, 0);
    rt.complete(h, 1);
    rt.complete(h, 2);
    assert_eq!(rt.available_capacity(), 1);
    assert_eq!(rt.is_loaned(0), Some(true));
    assert_eq!(rt.is_loaned(2), Some(true));

    // Consumer returns two buffers; capacity tracks the actual returns.
    rt.return_buffer(0);
    rt.return_buffer(1);
    assert_eq!(rt.available_capacity(), 3);

    // Final completion frees the slot even while buffer 2 is still loaned.
    rt.finalize(h);
    assert_eq!(rt.slot_state(0), Some(SlotState::Free));

    // Reuse: same slot, generation bumped and verifiable.
    let h2 = rt.submit().expect("slot reusable after finalize");
    assert_eq!(h2.slot, 0);
    assert_eq!(h2.generation, 2);
    assert_eq!(rt.slot_generation(0), Some(2));

    // The still-outstanding buffer 2 is released only by its consumer return.
    rt.return_buffer(2);
    assert_eq!(rt.available_capacity(), 4);
}

/// Headline interleaving: cancel receipt arrives before the final completion;
/// the device still delivers one last segment afterwards, which must be
/// reclaimed (not loaned), while the consumer keeps holding an earlier buffer.
#[test]
fn cancel_then_late_device_segment_while_consumer_holds_buffer() {
    let mut rt = IoRuntime::new(1, 3);

    let h = rt.submit().unwrap();
    rt.complete(h, 0); // loaned to consumer
    assert_eq!(rt.available_capacity(), 2);

    // Cancel receipt first.
    rt.cancel(h);
    assert_eq!(rt.slot_state(0), Some(SlotState::Cancelled));

    // The underlying layer still delivers a last segment: reclaimed, not
    // handed to the consumer, capacity unchanged.
    rt.complete(h, 1);
    assert_eq!(rt.available_capacity(), 2);
    assert_eq!(rt.is_loaned(1), Some(false));
    assert!(rt.log().contains(&Event::Reclaimed { handle: h, buffer: 1 }));
    assert!(!rt.log().contains(&Event::Completion { handle: h, buffer: 1 }));

    // Consumer still holds buffer 0 across the cancellation.
    assert_eq!(rt.is_loaned(0), Some(true));

    // Final completion arrives after the cancel receipt: slot frees.
    rt.finalize(h);
    assert_eq!(rt.slot_state(0), Some(SlotState::Free));

    // Only the consumer's actual return releases the loaned buffer.
    rt.return_buffer(0);
    assert_eq!(rt.available_capacity(), 3);
    assert_eq!(rt.is_loaned(0), Some(false));
}

/// After a slot is reused, late events carrying the old generation must not
/// advance (or disturb) the new request occupying the slot.
#[test]
fn stale_generation_events_do_not_advance_new_request() {
    let mut rt = IoRuntime::new(1, 2);

    let old = rt.submit().unwrap();
    rt.finalize(old);
    let new = rt.submit().unwrap();
    assert_eq!(new.slot, old.slot);
    assert_eq!(new.generation, old.generation + 1);

    // Late old-generation device events: all ignored.
    rt.complete(old, 0);
    rt.cancel(old);
    rt.finalize(old);

    assert_eq!(rt.available_capacity(), 2, "stale completion must not loan a buffer");
    assert_eq!(rt.slot_state(0), Some(SlotState::Active), "stale finalize must not free the slot");

    let stale_count = rt
        .log()
        .iter()
        .filter(|e| matches!(e, Event::StaleIgnored { handle, .. } if *handle == old))
        .count();
    assert_eq!(stale_count, 3);

    // The new request proceeds normally.
    rt.complete(new, 1);
    assert_eq!(rt.available_capacity(), 1);
    rt.finalize(new);
    assert_eq!(rt.slot_state(0), Some(SlotState::Free));
}

/// Duplicate completions, returns, cancels and finalizes never add balance
/// and never change state twice.
#[test]
fn duplicates_never_add_balance() {
    let mut rt = IoRuntime::new(1, 2);

    let h = rt.submit().unwrap();
    rt.complete(h, 0);
    rt.complete(h, 0); // duplicate completion of the same buffer
    assert_eq!(rt.available_capacity(), 1);

    rt.cancel(h);
    rt.cancel(h); // duplicate cancel receipt
    assert_eq!(rt.slot_state(0), Some(SlotState::Cancelled));

    rt.finalize(h);
    rt.finalize(h); // duplicate final completion
    assert_eq!(rt.slot_state(0), Some(SlotState::Free));

    rt.return_buffer(0);
    rt.return_buffer(0); // duplicate consumer return
    assert_eq!(rt.available_capacity(), 2, "capacity must not exceed the pool size");

    let dup_count = rt
        .log()
        .iter()
        .filter(|e| matches!(e, Event::DuplicateIgnored { .. }))
        .count();
    assert_eq!(dup_count, 4);
}

/// Boundary: slot exhaustion, out-of-range ids, and empty runtime.
#[test]
fn boundary_conditions() {
    // No slots at all: submit fails immediately.
    let mut empty = IoRuntime::new(0, 1);
    assert_eq!(empty.submit(), None);

    let mut rt = IoRuntime::new(1, 1);
    let h = rt.submit().unwrap();
    assert_eq!(rt.submit(), None, "no free slot while request is in flight");

    // Out-of-range buffer and slot ids are ignored, not panics.
    rt.complete(h, 7);
    rt.return_buffer(7);
    rt.complete(RequestHandle { slot: 9, generation: 1 }, 0);
    rt.cancel(RequestHandle { slot: 9, generation: 1 });
    rt.finalize(RequestHandle { slot: 9, generation: 1 });
    assert_eq!(rt.available_capacity(), 1);
    assert_eq!(
        rt.log()
            .iter()
            .filter(|e| matches!(e, Event::InvalidIgnored { .. }))
            .count(),
        5
    );

    // A wrong-generation handle on a live slot is stale, not invalid.
    rt.complete(RequestHandle { slot: 0, generation: 99 }, 0);
    assert!(rt.log().iter().any(|e| matches!(e, Event::StaleIgnored { .. })));
    assert_eq!(rt.available_capacity(), 1);

    // Slot only frees after the final completion.
    rt.complete(h, 0);
    assert_eq!(rt.submit(), None, "active slot is not reusable");
    rt.finalize(h);
    assert!(rt.submit().is_some());
}

/// Capacity accounting is traceable: every decrease comes from a `Completion`
/// loan, every increase from an actual `Returned` event, and reclaimed or
/// duplicate events never move the balance.
#[test]
fn capacity_changes_are_traceable_to_actual_events() {
    let mut rt = IoRuntime::new(2, 3);

    let h = rt.submit().unwrap();
    rt.complete(h, 0); // loan:      3 -> 2
    rt.cancel(h);
    rt.complete(h, 1); // reclaimed: 2 -> 2
    rt.complete(h, 2); // reclaimed: 2 -> 2
    rt.return_buffer(0); // return:  2 -> 3
    rt.return_buffer(1); // not loaned (reclaimed): 3 -> 3
    rt.finalize(h);

    assert_eq!(rt.available_capacity(), 3);

    // Replay the log and check the capacity delta of each event kind.
    let mut capacity = 3;
    for event in rt.log() {
        match event {
            Event::Completion { .. } => capacity -= 1,
            Event::Returned { .. } => capacity += 1,
            _ => {}
        }
    }
    assert_eq!(capacity, rt.available_capacity());

    // Every capacity increase corresponds to a Returned event for a buffer
    // that was actually loaned.
    let returned: Vec<usize> = rt
        .log()
        .iter()
        .filter_map(|e| match e {
            Event::Returned { buffer } => Some(*buffer),
            _ => None,
        })
        .collect();
    assert_eq!(returned, vec![0]);
}

/// Script-driven interleaving of two requests on two slots: cancellation,
/// reclamation, consumer returns and slot reuse all interleaved, driven
/// through the `Step` input interface.
#[test]
fn interleaved_script_across_slots() {
    let mut rt = IoRuntime::new(2, 2);

    // Phase 1: acquire both slots.
    let handles = rt.run_script(&[Step::Submit, Step::Submit]);
    let a = handles[0].unwrap();
    let b = handles[1].unwrap();
    assert_ne!(a.slot, b.slot);

    // Phase 2: interleave device events and consumer returns.
    rt.run_script(&[
        Step::Complete { handle: a, buffer: 0 }, // A loans buffer 0
        Step::Complete { handle: b, buffer: 1 }, // B loans buffer 1
        Step::Cancel { handle: a },              // A cancelled
        Step::Complete { handle: a, buffer: 0 }, // dup of loaned buffer: ignored
        Step::Finalize { handle: b },            // B finishes
        Step::Return { buffer: 1 },              // consumer returns B's buffer
        Step::Finalize { handle: a },            // A's final completion
    ]);
    assert_eq!(rt.available_capacity(), 1); // buffer 0 still with consumer
    assert_eq!(rt.slot_state(a.slot), Some(SlotState::Free));
    assert_eq!(rt.slot_state(b.slot), Some(SlotState::Free));

    // Phase 3: reuse both slots; generations bumped per slot.
    let handles = rt.run_script(&[Step::Submit, Step::Submit]);
    let a2 = handles[0].unwrap();
    let b2 = handles[1].unwrap();
    assert_eq!((a2.slot, a2.generation), (a.slot, a.generation + 1));
    assert_eq!((b2.slot, b2.generation), (b.slot, b.generation + 1));

    // Old-generation event for A must not touch the new request; the
    // consumer's outstanding buffer 0 is still returned normally.
    rt.run_script(&[
        Step::Complete { handle: a, buffer: 0 },
        Step::Return { buffer: 0 },
    ]);
    assert_eq!(rt.available_capacity(), 2);
    assert_eq!(rt.slot_state(a2.slot), Some(SlotState::Active));
}
