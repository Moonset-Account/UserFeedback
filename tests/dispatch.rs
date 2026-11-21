//! End-to-end tests for the causal dispatcher.
//!
//! Scenarios are grouped as normal (well-ordered flow), boundary (gaps,
//! duplicates, malformed input, ties) and interleaved (several sources racing,
//! checkpoint / restart halfway through).

use causal_dispatch::{CausalDispatcher, Checkpoint, DispatchError, Message, MessageId};

/// Renders a release batch as `"source/seq"` strings for easy comparison.
fn ids(batch: &[Message<&'static str>]) -> Vec<String> {
    batch.iter().map(|m| m.id().to_string()).collect()
}

fn msg(source: &str, seq: u64, payload: &'static str) -> Message<&'static str> {
    Message::new(source, seq, payload)
}

// ---------------------------------------------------------------------------
// Normal scenarios
// ---------------------------------------------------------------------------

#[test]
fn single_source_delivers_in_order_and_ignores_the_future() {
    let mut rx = CausalDispatcher::new();

    // Sequence 2 arrives before 1: it must wait, not be dispatched.
    assert!(rx.receive(msg("dc-a", 2, "a2")).unwrap().is_empty());
    assert_eq!(rx.progress("dc-a"), 0);
    assert!(rx.is_pending("dc-a", 2));

    // Sequence 1 unlocks itself and then 2, in one batch, in order.
    let released = rx.receive(msg("dc-a", 1, "a1")).unwrap();
    assert_eq!(ids(&released), ["dc-a/1", "dc-a/2"]);
    assert_eq!(rx.progress("dc-a"), 2);
    assert_eq!(rx.pending_len(), 0);
    assert_eq!(rx.dispatched_count(), 2);
}

#[test]
fn independent_sources_advance_without_blocking_each_other() {
    let mut rx = CausalDispatcher::new();

    assert_eq!(ids(&rx.receive(msg("dc-a", 1, "a1")).unwrap()), ["dc-a/1"]);
    assert_eq!(ids(&rx.receive(msg("dc-b", 1, "b1")).unwrap()), ["dc-b/1"]);
    assert_eq!(ids(&rx.receive(msg("dc-c", 1, "c1")).unwrap()), ["dc-c/1"]);
    assert_eq!(
        rx.progressed().keys().map(String::as_str).collect::<Vec<_>>(),
        ["dc-a", "dc-b", "dc-c"]
    );
}

#[test]
fn declared_dependency_holds_a_message_back_until_satisfied() {
    let mut rx = CausalDispatcher::new();

    // b1 is the next number of dc-b, but waits for dc-a/2.
    let b1 = msg("dc-b", 1, "b1").depends_on("dc-a", 2);
    assert!(rx.receive(b1).unwrap().is_empty());
    assert_eq!(rx.progress("dc-b"), 0);

    assert_eq!(ids(&rx.receive(msg("dc-a", 1, "a1")).unwrap()), ["dc-a/1"]);
    // b1 still blocked: dc-a is only at 1.
    assert_eq!(rx.pending_len(), 1);

    let released = rx.receive(msg("dc-a", 2, "a2")).unwrap();
    assert_eq!(ids(&released), ["dc-a/2", "dc-b/1"]);
    assert_eq!(rx.progress("dc-b"), 1);
}

#[test]
fn duplicate_delivery_of_dispatched_message_is_ignored() {
    let mut rx = CausalDispatcher::new();

    assert_eq!(rx.receive(msg("dc-a", 1, "a1")).unwrap().len(), 1);
    assert!(rx.receive(msg("dc-a", 1, "a1")).unwrap().is_empty());
    assert!(rx.receive(msg("dc-a", 1, "a1-different-payload")).unwrap().is_empty());

    assert_eq!(rx.dispatched_count(), 1);
    assert_eq!(rx.ignored_duplicates(), 2);
    assert_eq!(rx.progress("dc-a"), 1);
}

#[test]
fn duplicate_while_waiting_keeps_the_first_copy() {
    let mut rx = CausalDispatcher::new();

    let first = msg("dc-b", 1, "first").depends_on("dc-a", 1);
    let second = msg("dc-b", 1, "second").depends_on("dc-a", 1);
    assert!(rx.receive(first).unwrap().is_empty());
    assert!(rx.receive(second).unwrap().is_empty());
    assert_eq!(rx.ignored_duplicates(), 1);
    assert_eq!(rx.pending_len(), 1);

    let released = rx.receive(msg("dc-a", 1, "a1")).unwrap();
    let payloads: Vec<_> = released.iter().map(|m| m.payload).collect();
    assert_eq!(payloads, ["a1", "first"]);
}

// ---------------------------------------------------------------------------
// Boundary scenarios
// ---------------------------------------------------------------------------

#[test]
fn gap_in_own_sequence_blocks_later_numbers() {
    let mut rx = CausalDispatcher::new();

    assert!(rx.receive(msg("dc-a", 3, "a3")).unwrap().is_empty());
    assert!(rx.receive(msg("dc-a", 4, "a4")).unwrap().is_empty());
    assert!(rx.receive(msg("dc-a", 2, "a2")).unwrap().is_empty());

    // Filling number 1 releases the whole contiguous run 1,2,3,4.
    let released = rx.receive(msg("dc-a", 1, "a1")).unwrap();
    assert_eq!(ids(&released), ["dc-a/1", "dc-a/2", "dc-a/3", "dc-a/4"]);
    assert_eq!(rx.pending_len(), 0);
}

#[test]
fn ties_are_broken_by_source_name_then_sequence() {
    let mut rx = CausalDispatcher::new();

    // Both become ready at the same instant once dc-a/1 lands.
    let b1 = msg("dc-b", 1, "b1").depends_on("dc-a", 1);
    let a2 = msg("dc-a", 2, "a2").depends_on("dc-a", 1);
    assert!(rx.receive(b1).unwrap().is_empty());
    assert!(rx.receive(a2).unwrap().is_empty());

    let released = rx.receive(msg("dc-a", 1, "a1")).unwrap();
    // "dc-a" sorts before "dc-b", so a/2 is released before b/1.
    assert_eq!(ids(&released), ["dc-a/1", "dc-a/2", "dc-b/1"]);
}

#[test]
fn sequence_tie_is_decided_after_source_tie() {
    let mut rx = CausalDispatcher::new();

    // Several sources become ready together; the smallest source name wins
    // first, and within one source the smaller sequence wins.
    let mut pending = Vec::new();
    pending.push(rx.receive(msg("zulu", 1, "z1").depends_on("hub", 1)).unwrap());
    pending.push(rx.receive(msg("alpha", 2, "a2").depends_on("hub", 1)).unwrap());
    pending.push(rx.receive(msg("alpha", 1, "a1").depends_on("hub", 1)).unwrap());
    pending.push(rx.receive(msg("mike", 1, "m1").depends_on("hub", 1)).unwrap());
    pending.push(rx.receive(msg("mike", 2, "m2").depends_on("hub", 1)).unwrap());
    assert!(pending.iter().all(Vec::is_empty));

    let released = rx.receive(msg("hub", 1, "h1")).unwrap();
    assert_eq!(
        ids(&released),
        ["hub/1", "alpha/1", "alpha/2", "mike/1", "mike/2", "zulu/1"]
    );
}

#[test]
fn out_of_order_own_sequence_is_not_released_even_when_deps_are_met() {
    // mike/2 depends only on hub/1, and hub/1 is dispatched, but mike/1 has
    // never arrived -- so mike/2 must keep waiting.
    let mut rx = CausalDispatcher::new();
    assert!(rx.receive(msg("mike", 2, "m2").depends_on("hub", 1)).unwrap().is_empty());
    assert_eq!(ids(&rx.receive(msg("hub", 1, "h1")).unwrap()), ["hub/1"]);
    assert!(rx.is_pending("mike", 2));

    // Once mike/1 lands, the run 1,2 is released together.
    assert_eq!(
        ids(&rx.receive(msg("mike", 1, "m1")).unwrap()),
        ["mike/1", "mike/2"]
    );
    assert_eq!(rx.pending_len(), 0);
}

#[test]
fn source_names_use_bytewise_lexicographic_order() {
    // "Z" (0x5A) sorts before "a" (0x61), and "dc-10" before "dc-2".
    let mut rx = CausalDispatcher::new();
    for (source, seq) in [("a", 1), ("Z", 1), ("dc-2", 1), ("dc-10", 1)] {
        assert!(rx.receive(msg(source, seq, "x").depends_on("root", 1)).unwrap().is_empty());
    }
    let released = rx.receive(msg("root", 1, "r")).unwrap();
    assert_eq!(ids(&released), ["root/1", "Z/1", "a/1", "dc-10/1", "dc-2/1"]);
}

#[test]
fn message_cannot_depend_on_itself_or_the_future_of_its_own_source() {
    let mut rx = CausalDispatcher::new();

    let err = rx
        .receive(msg("dc-a", 2, "a2").depends_on("dc-a", 2))
        .unwrap_err();
    assert_eq!(
        err,
        DispatchError::SelfDependencyNotInPast {
            source: "dc-a".into(),
            seq: 2,
            required: 2
        }
    );

    // A dependency on an earlier own number is allowed.
    assert!(rx
        .receive(msg("dc-a", 2, "ok").depends_on("dc-a", 1))
        .unwrap()
        .is_empty());
    assert_eq!(rx.pending_len(), 1);
    assert!(rx.receive(msg("dc-a", 1, "a1")).is_ok());

    // State is untouched by the rejected message.
    assert_eq!(rx.ignored_duplicates(), 0);
}

#[test]
fn malformed_messages_are_rejected_without_changing_state() {
    let mut rx = CausalDispatcher::new();

    assert_eq!(
        rx.receive(msg("", 1, "x")).unwrap_err(),
        DispatchError::EmptySource
    );
    assert_eq!(
        rx.receive(msg("dc-a", 0, "x")).unwrap_err(),
        DispatchError::ZeroSequence {
            source: "dc-a".into()
        }
    );
    assert_eq!(
        rx.receive(msg("dc-a", 1, "x").depends_on("", 1)).unwrap_err(),
        DispatchError::EmptyDependencySource
    );

    assert_eq!(rx.pending_len(), 0);
    assert_eq!(rx.dispatched_count(), 0);
    assert_eq!(rx.progress("dc-a"), 0);
}

#[test]
fn zero_dependency_is_a_no_op() {
    let mut rx = CausalDispatcher::new();
    let m = msg("dc-b", 1, "b1").depends_on("dc-a", 0);
    assert_eq!(ids(&rx.receive(m).unwrap()), ["dc-b/1"]);
}

#[test]
fn depends_on_keeps_the_strictest_requirement_per_source() {
    let m = msg("dc-b", 1, "b1").depends_on("dc-a", 2).depends_on("dc-a", 3);
    assert_eq!(m.deps.get("dc-a"), Some(&3));
    let m = msg("dc-b", 1, "b1").depends_on("dc-a", 3).depends_on("dc-a", 2);
    assert_eq!(m.deps.get("dc-a"), Some(&3));
}

#[test]
fn message_id_ordering_is_source_then_seq() {
    let mk = |s: &str, n: u64| MessageId {
        source: s.into(),
        seq: n,
    };
    assert!(mk("a", 9) < mk("b", 1));
    assert!(mk("a", 1) < mk("a", 2));
    assert_eq!(mk("dc-a", 3).to_string(), "dc-a/3");
}

// ---------------------------------------------------------------------------
// Interleaved scenarios
// ---------------------------------------------------------------------------

#[test]
fn three_sources_interleave_through_a_shared_bus() {
    // dc-b waits on dc-a/2, dc-c waits on dc-b/1.
    let mut rx = CausalDispatcher::new();

    let c1 = msg("dc-c", 1, "c1").depends_on("dc-b", 1);
    let b1 = msg("dc-b", 1, "b1").depends_on("dc-a", 2);

    // Arrival order: c then b. Neither may be released.
    assert!(rx.receive(c1).unwrap().is_empty());
    assert!(rx.receive(b1).unwrap().is_empty());
    assert_eq!(rx.pending_len(), 2);
    assert_eq!(rx.dispatched_count(), 0);

    // a1 alone is not enough for b1.
    assert_eq!(ids(&rx.receive(msg("dc-a", 1, "a1")).unwrap()), ["dc-a/1"]);
    assert_eq!(rx.pending_len(), 2);

    // a2 cascades: a2 -> b1 -> c1, each re-checked after the previous release.
    let released = rx.receive(msg("dc-a", 2, "a2")).unwrap();
    let payloads: Vec<_> = released.iter().map(|m| m.payload).collect();
    assert_eq!(payloads, ["a2", "b1", "c1"]);
    assert_eq!(rx.progressed().len(), 3);
    assert_eq!(rx.progress("dc-a"), 2);
    assert_eq!(rx.progress("dc-b"), 1);
    assert_eq!(rx.progress("dc-c"), 1);
    assert_eq!(rx.pending_len(), 0);
}

#[test]
fn checkpoint_keeps_progress_and_waiting_messages() {
    let mut rx = CausalDispatcher::new();
    assert_eq!(ids(&rx.receive(msg("dc-a", 1, "a1")).unwrap()), ["dc-a/1"]);
    // dc-b/1 waits for dc-a/2, dc-c/1 waits for dc-b/1.
    assert!(rx.receive(msg("dc-b", 1, "b1").depends_on("dc-a", 2)).unwrap().is_empty());
    assert!(rx.receive(msg("dc-c", 1, "c1").depends_on("dc-b", 1)).unwrap().is_empty());

    let saved = rx.checkpoint();

    // The checkpoint records both halves: contiguous progress and waiters.
    assert_eq!(saved.progress("dc-a"), 1);
    assert_eq!(saved.progress("dc-b"), 0);
    assert_eq!(saved.pending_len(), 2);
    assert_eq!(
        saved.waiting_ids().map(|id| id.to_string()).collect::<Vec<_>>(),
        ["dc-b/1", "dc-c/1"]
    );
    assert_eq!(saved.dispatched_count(), 1);

    // Reconstruct the receiver and finish the same causal chain.
    let mut restored: CausalDispatcher<&'static str> = CausalDispatcher::restore(saved);
    let released = restored.receive(msg("dc-a", 2, "a2")).unwrap();
    let payloads: Vec<_> = released.iter().map(|m| m.payload).collect();
    assert_eq!(payloads, ["a2", "b1", "c1"]);
    assert_eq!(restored.pending_len(), 0);
}

#[test]
fn checkpoint_round_trip_is_equivalent_and_restorable_twice() {
    let mut rx = CausalDispatcher::new();
    assert_eq!(ids(&rx.receive(msg("dc-a", 1, "a1")).unwrap()), ["dc-a/1"]);
    assert!(rx.receive(msg("dc-a", 3, "a3")).unwrap().is_empty());

    let saved: Checkpoint<&'static str> = rx.checkpoint();
    let left: CausalDispatcher<&'static str> = CausalDispatcher::restore(saved.clone());
    let right: CausalDispatcher<&'static str> = CausalDispatcher::restore(saved);

    // Both restores see the same state, including the waiting a3.
    assert_eq!(left.checkpoint(), right.checkpoint());

    let mut left = left;
    let mut right = right;
    // A retried a3 is a duplicate for both.
    assert!(left.receive(msg("dc-a", 3, "a3")).unwrap().is_empty());
    assert!(right.receive(msg("dc-a", 3, "a3")).unwrap().is_empty());
    assert_eq!(left.ignored_duplicates(), right.ignored_duplicates());

    // Both complete identically.
    let l = ids(&left.receive(msg("dc-a", 2, "a2")).unwrap());
    let r = ids(&right.receive(msg("dc-a", 2, "a2")).unwrap());
    assert_eq!(l, ["dc-a/2", "dc-a/3"]);
    assert_eq!(l, r);
}

#[test]
fn circular_dependency_deadlocks_without_spinning_or_dropping() {
    // b1 waits for a1; a1 waits for b1. Nothing may be dispatched, and the
    // receiver must stay queryable: both messages remain pending.
    let mut rx = CausalDispatcher::new();
    assert!(rx
        .receive(msg("dc-a", 1, "a1").depends_on("dc-b", 1))
        .unwrap()
        .is_empty());
    assert!(rx
        .receive(msg("dc-b", 1, "b1").depends_on("dc-a", 1))
        .unwrap()
        .is_empty());

    assert_eq!(rx.dispatched_count(), 0);
    assert_eq!(rx.pending_len(), 2);
    assert_eq!(rx.progress("dc-a"), 0);
    assert_eq!(rx.progress("dc-b"), 0);
    assert_eq!(
        rx.waiting_ids().map(|id| id.to_string()).collect::<Vec<_>>(),
        ["dc-a/1", "dc-b/1"]
    );
}

#[test]
fn message_whose_dependencies_are_already_met_dispatches_on_arrival() {
    let mut rx = CausalDispatcher::new();
    assert_eq!(ids(&rx.receive(msg("dc-a", 1, "a1")).unwrap()), ["dc-a/1"]);
    // dc-b/1 arrives after dc-a/1 is already dispatched.
    assert_eq!(
        ids(&rx.receive(msg("dc-b", 1, "b1").depends_on("dc-a", 1)).unwrap()),
        ["dc-b/1"]
    );
    assert_eq!(rx.pending_len(), 0);
}

#[test]
fn prompt_scenario_c_then_b_then_restart_then_a1_a2() {
    // The scenario from the task, end to end:
    //   dc-b/1 waits for dc-a/2, dc-c/1 waits for dc-b/1.
    //   Receive C first, then B. Neither may be dispatched.
    //   Take a checkpoint, reopen the receiver.
    //   Re-deliver C and B (old messages) -- still nothing.
    //   Supply A1 and A2 -- the caller receives the full order.
    //   Re-delivering any old message again dispatches nothing.
    let mut rx = CausalDispatcher::new();
    let mut delivered: Vec<String> = Vec::new();
    let accept = |rx: &mut CausalDispatcher<&'static str>,
                  delivered: &mut Vec<String>,
                  m: Message<&'static str>| {
        for out in rx.receive(m).unwrap() {
            delivered.push(out.id().to_string());
        }
    };

    // C before B, exactly as the task describes.
    accept(&mut rx, &mut delivered, msg("dc-c", 1, "c1").depends_on("dc-b", 1));
    accept(&mut rx, &mut delivered, msg("dc-b", 1, "b1").depends_on("dc-a", 2));
    assert!(delivered.is_empty(), "C and B must wait");
    assert_eq!(rx.pending_len(), 2);

    // Breakpoint: progress per source plus the waiting messages.
    let saved = rx.checkpoint();
    assert_eq!(saved.progress("dc-a"), 0);
    assert_eq!(saved.progress("dc-b"), 0);
    assert_eq!(saved.progress("dc-c"), 0);
    assert_eq!(saved.pending_len(), 2);
    assert_eq!(
        saved.waiting_ids().map(|id| id.to_string()).collect::<Vec<_>>(),
        ["dc-b/1", "dc-c/1"]
    );

    let mut reopened: CausalDispatcher<&'static str> = CausalDispatcher::restore(saved);

    // Re-sending the messages that were already waiting changes nothing.
    accept(&mut reopened, &mut delivered, msg("dc-c", 1, "c1").depends_on("dc-b", 1));
    accept(&mut reopened, &mut delivered, msg("dc-b", 1, "b1").depends_on("dc-a", 2));
    assert!(delivered.is_empty());
    assert_eq!(reopened.ignored_duplicates(), 2);

    // Now the missing A side arrives, in order.
    accept(&mut reopened, &mut delivered, msg("dc-a", 1, "a1"));
    accept(&mut reopened, &mut delivered, msg("dc-a", 2, "a2"));

    assert_eq!(delivered, ["dc-a/1", "dc-a/2", "dc-b/1", "dc-c/1"]);
    assert_eq!(reopened.pending_len(), 0);
    assert_eq!(reopened.dispatched_count(), 4);

    // The full backlog is re-sent one more time: nothing is dispatched.
    let before = reopened.dispatched_count();
    for m in [
        msg("dc-a", 1, "a1"),
        msg("dc-a", 2, "a2"),
        msg("dc-b", 1, "b1").depends_on("dc-a", 2),
        msg("dc-c", 1, "c1").depends_on("dc-b", 1),
    ] {
        accept(&mut reopened, &mut delivered, m);
    }
    assert_eq!(reopened.dispatched_count(), before);
    assert_eq!(reopened.ignored_duplicates(), 6);
    assert_eq!(delivered.len(), 4);
}

#[test]
fn checkpoint_taken_mid_cascade_loses_nothing() {
    // Restart while a2 is still waiting inside the receiver: it must come along
    // in the checkpoint and be released after the restart.
    let mut rx = CausalDispatcher::new();
    assert!(rx
        .receive(msg("dc-a", 2, "a2").depends_on("dc-a", 1))
        .unwrap()
        .is_empty());
    let saved = rx.checkpoint();
    assert_eq!(saved.progress("dc-a"), 0);
    assert_eq!(saved.pending_len(), 1);

    let mut reopened: CausalDispatcher<&'static str> = CausalDispatcher::restore(saved);
    assert_eq!(
        ids(&reopened.receive(msg("dc-a", 1, "a1")).unwrap()),
        ["dc-a/1", "dc-a/2"]
    );
}

#[test]
fn restart_replays_old_messages_without_dispatching_them() {
    let mut rx = CausalDispatcher::new();
    assert_eq!(ids(&rx.receive(msg("dc-a", 1, "a1")).unwrap()), ["dc-a/1"]);
    let saved = rx.checkpoint();

    let mut reopened: CausalDispatcher<&'static str> = CausalDispatcher::restore(saved);

    // The caller re-sends everything it has seen: nothing new is released.
    assert!(reopened.receive(msg("dc-a", 1, "a1")).unwrap().is_empty());
    assert_eq!(reopened.dispatched_count(), 1);

    // Progress survived the restart, so a2 is the next accepted number.
    assert_eq!(ids(&reopened.receive(msg("dc-a", 2, "a2")).unwrap()), ["dc-a/2"]);
    assert_eq!(reopened.progress("dc-a"), 2);
}

#[test]
fn interleaved_high_volume_keeps_per_source_order() {
    // Deliver 40 messages from three sources in a scrambled order with
    // cross-source dependencies, then check every source's order.
    let mut rx = CausalDispatcher::new();
    let mut delivered: Vec<(String, u64)> = Vec::new();

    let feed = |rx: &mut CausalDispatcher<String>,
                delivered: &mut Vec<(String, u64)>,
                source: &str,
                seq: u64| {
        let mut m = Message::new(source, seq, format!("{source}/{seq}"));
        if source != "dc-a" && seq == 1 {
            m = m.depends_on("dc-a", 3);
        }
        if source == "dc-c" && seq > 1 {
            m = m.depends_on("dc-b", seq - 1);
        }
        for out in rx.receive(m).unwrap() {
            delivered.push((out.source, out.seq));
        }
    };

    // Round 1: everything except dc-a/1..3 arrives first.
    for seq in 1..=6 {
        for source in ["dc-b", "dc-c"] {
            feed(&mut rx, &mut delivered, source, seq);
        }
    }
    assert!(delivered.is_empty(), "nothing may pass before dc-a/3");

    // Round 2: dc-a catches up; the backlog drains.
    for seq in 1..=8 {
        feed(&mut rx, &mut delivered, "dc-a", seq);
    }

    // Every message from dc-a and dc-b was delivered; dc-c up to 6 too.
    for source in ["dc-a", "dc-b", "dc-c"] {
        let got: Vec<u64> = delivered
            .iter()
            .filter(|(s, _)| s == source)
            .map(|(_, n)| *n)
            .collect();
        let mut expected: Vec<u64> = got.clone();
        expected.sort_unstable();
        assert_eq!(got, expected, "{source} must be delivered in ascending order");
    }
    assert_eq!(delivered.iter().filter(|(s, _)| s == "dc-a").count(), 8);
    assert_eq!(delivered.iter().filter(|(s, _)| s == "dc-b").count(), 6);
    assert_eq!(delivered.iter().filter(|(s, _)| s == "dc-c").count(), 6);
    assert_eq!(rx.pending_len(), 0);
    assert_eq!(rx.dispatched_count(), 20);
}

#[test]
fn readiness_is_re_evaluated_after_every_release() {
    // "aa-c"/1 sorts before "zz-b"/1, so it is the lexicographic minimum while
    // blocked. The dispatcher must skip it, release "zz-b"/1, and only then
    // find "aa-c"/1 ready. A naive "release the smallest ready message once per
    // receipt" loop would get this order wrong.
    let mut rx = CausalDispatcher::new();
    assert!(rx
        .receive(msg("aa-c", 1, "c1").depends_on("zz-b", 1))
        .unwrap()
        .is_empty());
    assert!(rx
        .receive(msg("zz-b", 1, "b1").depends_on("root", 1))
        .unwrap()
        .is_empty());

    let released = rx.receive(msg("root", 1, "r")).unwrap();
    assert_eq!(ids(&released), ["root/1", "zz-b/1", "aa-c/1"]);
}

#[test]
fn dependency_on_an_unknown_source_blocks_until_that_source_appears() {
    let mut rx = CausalDispatcher::new();
    let m = msg("dc-b", 1, "b1").depends_on("dc-ghost", 1);
    assert!(rx.receive(m).unwrap().is_empty());
    assert_eq!(rx.progress("dc-ghost"), 0);
    assert!(rx.is_pending("dc-b", 1));

    assert_eq!(
        ids(&rx.receive(msg("dc-ghost", 1, "g1")).unwrap()),
        ["dc-ghost/1", "dc-b/1"]
    );
}

#[test]
fn dependency_is_inclusive_of_the_named_number() {
    let mut rx = CausalDispatcher::new();
    // Requires dc-a/2 to be *dispatched*, not merely received.
    let b = msg("dc-b", 1, "b1").depends_on("dc-a", 2);
    assert!(rx.receive(b).unwrap().is_empty());
    assert!(rx.receive(msg("dc-a", 2, "a2")).unwrap().is_empty());
    assert!(rx.is_pending("dc-b", 1));
    assert_eq!(
        ids(&rx.receive(msg("dc-a", 1, "a1")).unwrap()),
        ["dc-a/1", "dc-a/2", "dc-b/1"]
    );
}

#[test]
fn empty_receiver_checkpoint_restores_cleanly() {
    let rx: CausalDispatcher<String> = CausalDispatcher::new();
    let saved = rx.checkpoint();
    assert_eq!(saved.pending_len(), 0);
    assert_eq!(saved.dispatched_count(), 0);
    assert_eq!(saved.progressed().len(), 0);

    let mut restored = CausalDispatcher::restore(saved);
    let mut ok = restored
        .receive(Message::new("dc-a", 1, String::from("a1")))
        .unwrap();
    assert_eq!(ok.len(), 1);
    assert_eq!(ok.remove(0).payload, "a1");
}
