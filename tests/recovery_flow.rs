//! End-to-end tests driving the recovery module through event sequences.
//! These are real functional tests: every assertion is computed from state
//! transitions, never from canned demo output.

use sender_recovery::{
    DupEvent, Event, Reason, RecoveryError, RecoverySender, SendEvent, WireRange, FIRST_ATTEMPT,
};

fn send(start: u32, end: u32, attempt: u32) -> Event {
    Event::Send(SendEvent {
        start,
        end,
        attempt,
    })
}

fn dup(start: u32, end: u32, attempt: u32) -> Event {
    Event::Dup(DupEvent {
        start,
        end,
        attempt,
    })
}

fn sack(ranges: &[(u32, u32)]) -> Event {
    Event::Sack(ranges.iter().map(|&(s, e)| WireRange::new(s, e)).collect())
}

fn revoke(ranges: &[(u32, u32)]) -> Event {
    Event::Revoke(ranges.iter().map(|&(s, e)| WireRange::new(s, e)).collect())
}

fn loss(ranges: &[(u32, u32)]) -> Event {
    Event::Loss(ranges.iter().map(|&(s, e)| WireRange::new(s, e)).collect())
}

/// Normal fast-recovery path: loss -> round entry -> partial retransmit ->
/// duplicate evidence -> cumulative ack crosses boundary -> window restored.
#[test]
fn full_fast_recovery_withdraws_window_reduction() {
    let mut s = RecoverySender::new(1000, 100).unwrap();

    let snap = s.apply(send(1000, 1100, FIRST_ATTEMPT)).unwrap();
    assert_eq!(snap.in_flight, 100);
    assert_eq!(snap.cwnd, 100);
    assert!(snap.next_retransmit.is_empty());

    // Three bytes are marked lost; no round yet, so nothing is retransmittable.
    let snap = s.apply(loss(&[(1020, 1023)])).unwrap();
    assert_eq!(snap.in_flight, 100); // lost bytes remain in flight
    assert!(snap.next_retransmit.is_empty());

    // Caller declares round 1 with a halved window.
    let snap = s
        .apply(Event::EnterRecovery {
            round: 1,
            reduced_cwnd: 50,
            reason: Reason::FastRetransmit,
        })
        .unwrap();
    assert_eq!(snap.active_round, Some(1));
    assert_eq!(snap.cwnd, 50);
    assert_eq!(snap.next_retransmit, vec![WireRange::new(1020, 1023)]);
    let view = snap.rounds.get(&1).unwrap();
    assert_eq!(view.entry_una, 1000);
    assert_eq!(view.recovery_point, 1100);

    // Retransmit the lost bytes under a fresh attempt, split into two pieces.
    let snap = s.apply(send(1020, 1022, 2)).unwrap();
    assert_eq!(snap.in_flight, 100); // distinct bytes, no double counting
    assert_eq!(snap.next_retransmit, vec![WireRange::new(1022, 1023)]);
    s.apply(send(1022, 1023, 3)).unwrap();
    assert!(s.snapshot().next_retransmit.is_empty());

    // Partial evidence cannot withdraw the round.
    let snap = s.apply(dup(1020, 1022, 2)).unwrap();
    let view = snap.rounds.get(&1).unwrap();
    assert!(!view.evidence_complete);
    assert!(!view.withdrawable);
    assert_eq!(snap.cwnd, 50);

    // Cumulative ack still short of the recovery point (1100).
    let snap = s.apply(Event::CumAck(1090)).unwrap();
    let view = snap.rounds.get(&1).unwrap();
    assert!(!view.crossed_boundary);
    assert_eq!(snap.in_flight, 10);
    assert_eq!(snap.cwnd, 50);

    // Remaining evidence arrives, then the ack crosses the boundary.
    s.apply(dup(1022, 1023, 3)).unwrap();
    let snap = s.apply(Event::CumAck(1100)).unwrap();
    assert_eq!(snap.active_round, None);
    assert_eq!(snap.cwnd, 100); // saved window restored
    assert_eq!(snap.in_flight, 0);
    let view = snap.rounds.get(&1).unwrap();
    assert!(view.withdrawn);
    assert!(!view.active);
    assert!(view.evidence_complete);
    assert!(view.crossed_boundary);
}

/// Sequence numbers wrap around the 2^32 boundary mid-window; byte accounting
/// and the half-circle rule must still hold.
#[test]
fn recovery_across_32bit_wraparound() {
    let isn = u32::MAX - 30;
    let mut s = RecoverySender::new(isn, 100).unwrap();

    // 60 bytes straddling the wrap: [MAX-30, 0) (31 bytes) + [0, 29) (29).
    let snap = s
        .apply(send(isn, isn.wrapping_add(60), FIRST_ATTEMPT))
        .unwrap();
    assert_eq!(snap.in_flight, 60);
    assert_eq!(
        snap.first_sent,
        vec![WireRange::new(isn, 0), WireRange::new(0, 29)]
    );

    // 10 bytes straddling the wrap are lost: [MAX-5, 0) + [0, 5).
    s.apply(loss(&[(u32::MAX - 5, 5)])).unwrap();
    let snap = s
        .apply(Event::EnterRecovery {
            round: 1,
            reduced_cwnd: 40,
            reason: Reason::FastRetransmit,
        })
        .unwrap();
    // Lowest sequence comes before the wrap line; the run splits at wrap.
    // The lost run is 10 bytes: 5 before the wrap and 5 after it.
    assert_eq!(
        snap.next_retransmit,
        vec![WireRange::new(u32::MAX - 5, 0), WireRange::new(0, 5)]
    );

    // Retransmit the whole wrapped range under attempt 2.
    s.apply(send(u32::MAX - 5, 5, 2)).unwrap();
    // The same attempt number cannot be reused for another retransmission.
    assert_eq!(
        s.apply(send(u32::MAX - 5, 5, 2)).unwrap_err(),
        RecoveryError::AttemptReused(2)
    );

    // Evidence on the same wrapped range, then ack across the recovery point.
    s.apply(dup(u32::MAX - 5, 5, 2)).unwrap();
    let snap = s
        .apply(Event::CumAck(isn.wrapping_add(60)))
        .unwrap();
    assert_eq!(snap.active_round, None);
    assert_eq!(snap.cwnd, 100);
    assert_eq!(snap.in_flight, 0);
}

/// A timeout while a round is active permanently bars that round's withdrawal,
/// and marks every non-SACKed outstanding byte as lost.
#[test]
fn timeout_blocks_withdrawal_and_marks_outstanding_lost() {
    let mut s = RecoverySender::new(0, 100).unwrap();
    s.apply(send(0, 80, FIRST_ATTEMPT)).unwrap();
    s.apply(sack(&[(40, 60)])).unwrap();

    s.apply(loss(&[(10, 20)])).unwrap();
    s.apply(Event::EnterRecovery {
        round: 1,
        reduced_cwnd: 40,
        reason: Reason::FastRetransmit,
    })
    .unwrap();
    s.apply(send(10, 20, 2)).unwrap();

    // Timeout: sacked 40..60 excluded, every other outstanding byte is lost.
    let snap = s.apply(Event::Timeout).unwrap();
    let lost: Vec<(u64, u64)> = s.lost_flat();
    let flat_una = s.flat_una();
    let expected: Vec<(u64, u64)> = vec![
        (flat_una, flat_una + 40),
        (flat_una + 60, flat_una + 80),
    ];
    assert_eq!(lost, expected);
    assert!(snap.rounds[&1].timed_out);

    // Even with complete evidence and a crossing ack, the round stays active.
    s.apply(dup(10, 20, 2)).unwrap();
    let snap = s.apply(Event::CumAck(80)).unwrap();
    assert_eq!(snap.active_round, Some(1));
    assert_eq!(snap.cwnd, 40);
    let view = snap.rounds.get(&1).unwrap();
    assert!(!view.withdrawable);
    assert!(view.timed_out);
}

/// Nested rounds: evidence is attempt-scoped and cannot be borrowed across
/// rounds; rounds withdraw LIFO and restore each saved window in turn.
#[test]
fn nested_rounds_keep_attempt_evidence_separate() {
    let mut s = RecoverySender::new(0, 100).unwrap();
    s.apply(send(0, 90, FIRST_ATTEMPT)).unwrap();
    s.apply(loss(&[(10, 30), (50, 70)])).unwrap();

    s.apply(Event::EnterRecovery {
        round: 7,
        reduced_cwnd: 50,
        reason: Reason::FastRetransmit,
    })
    .unwrap();
    s.apply(send(10, 30, 2)).unwrap();

    // A second, stricter round nests inside the first.
    s.apply(Event::EnterRecovery {
        round: 8,
        reduced_cwnd: 30,
        reason: Reason::Reordering,
    })
    .unwrap();
    assert_eq!(s.active_stack(), vec![7, 8]);

    s.apply(send(50, 70, 3)).unwrap();

    // Attempt-2 evidence belongs to round 7; it must not satisfy round 8.
    s.apply(dup(10, 30, 2)).unwrap();
    let snap = s.snapshot();
    assert!(!snap.rounds[&8].evidence_complete);
    assert!(snap.rounds[&7].evidence_complete);

    // Withdrawal needs the inner round's own evidence plus boundary crossing.
    s.apply(dup(50, 70, 3)).unwrap();
    let snap = s.apply(Event::CumAck(90)).unwrap();
    assert_eq!(snap.active_round, None);
    // Inner withdrawal restores 50 (round 7 saved window), then round 7
    // withdraws and restores the original 100.
    assert_eq!(snap.cwnd, 100);
    assert!(snap.rounds[&7].withdrawn);
    assert!(snap.rounds[&8].withdrawn);
    assert!(snap.rounds[&7].evidence_complete);
    assert!(snap.rounds[&8].evidence_complete);
}

/// A SACK block can be explicitly revoked; revoked bytes return to the repair
/// queue and are retransmittable lowest-sequence first.
#[test]
fn revoked_sack_returns_to_repair_and_clears_on_reack() {
    let mut s = RecoverySender::new(0, 100).unwrap();
    s.apply(send(0, 50, FIRST_ATTEMPT)).unwrap();
    s.apply(sack(&[(20, 30)])).unwrap();
    let snap = s.snapshot();
    assert_eq!(snap.in_flight, 40);
    assert_eq!(snap.sacked, vec![WireRange::new(20, 30)]);

    // Receiver revokes the SACK before it is cumulatively acknowledged.
    let snap = s.apply(revoke(&[(20, 30)])).unwrap();
    assert!(snap.sacked.is_empty());
    assert_eq!(snap.revoked, vec![WireRange::new(20, 30)]);
    assert_eq!(snap.in_flight, 50);

    s.apply(Event::EnterRecovery {
        round: 1,
        reduced_cwnd: 40,
        reason: Reason::Reordering,
    })
    .unwrap();
    assert_eq!(
        s.snapshot().next_retransmit,
        vec![WireRange::new(20, 30)]
    );
    s.apply(send(20, 30, 2)).unwrap();
    assert!(s.snapshot().revoked.is_empty());

    // A fresh SACK for the same bytes removes them from repair without
    // inventing duplicate evidence.
    s.apply(sack(&[(20, 30)])).unwrap();
    let snap = s.snapshot();
    assert_eq!(snap.sacked, vec![WireRange::new(20, 30)]);
    assert!(!snap.rounds[&1].evidence_complete);

    s.apply(dup(20, 30, 2)).unwrap();
    let snap = s.apply(Event::CumAck(50)).unwrap();
    assert_eq!(snap.cwnd, 100);
    assert!(snap.rounds[&1].evidence_complete);
}

/// Segmentation can change between calls: limited retransmit slices byte
/// ranges, and duplicate arrivals byte-by-byte still reconcile exactly.
#[test]
fn byte_exact_tracking_under_split_segments_and_partial_dups() {
    let mut s = RecoverySender::new(100, 100).unwrap();
    s.apply(send(100, 160, FIRST_ATTEMPT)).unwrap();
    s.apply(loss(&[(100, 120)])).unwrap();
    s.apply(Event::EnterRecovery {
        round: 1,
        reduced_cwnd: 30,
        reason: Reason::FastRetransmit,
    })
    .unwrap();

    // Ask for at most 7, 7 then 6 bytes, consuming the full 20-byte run.
    let pieces = [7u64, 7, 6];
    let mut attempt = 2u32;
    let mut cursor = 100u32;
    for chunk in pieces {
        let (fs, fe) = s.limited_next_retransmit_flat(chunk).unwrap();
        assert_eq!(fe - fs, chunk);
        s.apply(send(cursor, cursor + chunk as u32, attempt)).unwrap();
        cursor += chunk as u32;
        attempt += 1;
    }
    assert_eq!(s.next_retransmit_flat(), None);

    // Dup evidence arrives byte-by-byte under each attempt; only the matching
    // attempt counts.
    let mut at = 100u32;
    let sizes = [7u32, 7, 6];
    for (i, size) in sizes.iter().enumerate() {
        let used_attempt = 2 + i as u32;
        for b in 0..*size {
            s.apply(dup(at + b, at + b + 1, used_attempt)).unwrap();
        }
        at += size;
    }
    let snap = s.snapshot();
    assert!(snap.rounds[&1].evidence_complete);
    assert_eq!(
        snap.rounds[&1].duplicate_evidence,
        vec![WireRange::new(100, 120)]
    );

    // Evidence under a wrong attempt does not add bytes to the round.
    let before = s.snapshot().rounds[&1].duplicate_evidence.clone();
    s.apply(dup(100, 105, FIRST_ATTEMPT)).unwrap();
    assert_eq!(before, s.snapshot().rounds[&1].duplicate_evidence);

    let snap = s.apply(Event::CumAck(160)).unwrap();
    assert_eq!(snap.active_round, None);
    assert_eq!(snap.cwnd, 100);
}

/// Retransmission always picks the lowest sequence candidate and in-flight
/// counts never double-count retransmitted bytes.
#[test]
fn lowest_sequence_first_and_distinct_in_flight() {
    let mut s = RecoverySender::new(0, 100).unwrap();
    s.apply(send(0, 60, FIRST_ATTEMPT)).unwrap();
    s.apply(loss(&[(5, 10), (30, 40)])).unwrap();
    s.apply(Event::EnterRecovery {
        round: 1,
        reduced_cwnd: 50,
        reason: Reason::FastRetransmit,
    })
    .unwrap();

    assert_eq!(
        s.snapshot().next_retransmit,
        vec![WireRange::new(5, 10)]
    );
    s.apply(send(5, 10, 2)).unwrap();
    assert_eq!(
        s.snapshot().next_retransmit,
        vec![WireRange::new(30, 40)]
    );
    // Same distinct bytes still in flight once.
    assert_eq!(s.in_flight(), 60);
}

/// A round with no retransmissions at all withdraws as soon as the ack crosses
/// its boundary, since every (zero) retransmitted byte trivially has evidence.
#[test]
fn zero_retransmission_round_withdraws_on_boundary() {
    let mut s = RecoverySender::new(0, 100).unwrap();
    s.apply(send(0, 20, FIRST_ATTEMPT)).unwrap();
    s.apply(Event::EnterRecovery {
        round: 1,
        reduced_cwnd: 40,
        reason: Reason::Reordering,
    })
    .unwrap();
    assert_eq!(s.cwnd(), 40);
    let snap = s.apply(Event::CumAck(20)).unwrap();
    assert_eq!(snap.cwnd, 100);
    assert_eq!(snap.active_round, None);
}

/// Validation errors preserve the state machine's invariants.
#[test]
fn invalid_events_are_rejected() {
    let mut s = RecoverySender::new(0, 100).unwrap();

    // Retransmission without an active round.
    assert_eq!(
        s.apply(send(0, 5, 2)).unwrap_err(),
        RecoveryError::NoActiveRound
    );

    // Attempt zero is reserved.
    assert_eq!(
        s.apply(send(0, 5, 0)).unwrap_err(),
        RecoveryError::InvalidAttempt
    );

    // Original sends must be contiguous.
    s.apply(send(0, 10, FIRST_ATTEMPT)).unwrap();
    assert!(matches!(
        s.apply(send(12, 15, FIRST_ATTEMPT)).unwrap_err(),
        RecoveryError::NonContiguousSend { .. }
    ));

    // Window enforcement on original sends.
    assert!(matches!(
        s.apply(send(10, 200, FIRST_ATTEMPT)).unwrap_err(),
        RecoveryError::CwndExceeded { .. }
    ));

    s.apply(loss(&[(2, 5)])).unwrap();
    s.apply(Event::EnterRecovery {
        round: 1,
        reduced_cwnd: 50,
        reason: Reason::FastRetransmit,
    })
    .unwrap();

    // Non-candidate bytes cannot be retransmitted.
    assert_eq!(
        s.apply(send(6, 8, 2)).unwrap_err(),
        RecoveryError::NotRetransmittable {
            range: (s.flat_una() + 6, s.flat_una() + 8)
        }
    );

    // Attempt numbers cannot be reused.
    s.apply(send(2, 5, 2)).unwrap();
    assert_eq!(
        s.apply(send(2, 5, 2)).unwrap_err(),
        RecoveryError::AttemptReused(2)
    );

    // Duplicate evidence must reference a known attempt.
    assert_eq!(
        s.apply(dup(2, 5, 99)).unwrap_err(),
        RecoveryError::UnknownAttempt(99)
    );

    // Cumulative acks cannot move backwards.
    s.apply(Event::CumAck(3)).unwrap();
    assert!(matches!(
        s.apply(Event::CumAck(2)).unwrap_err(),
        RecoveryError::OldAck { .. }
    ));

    // Round numbers are unique and windows must actually shrink.
    assert_eq!(
        s.apply(Event::EnterRecovery {
            round: 1,
            reduced_cwnd: 10,
            reason: Reason::Timeout
        })
        .unwrap_err(),
        RecoveryError::DuplicateRound(1)
    );
    assert_eq!(
        s.apply(Event::EnterRecovery {
            round: 2,
            reduced_cwnd: 60,
            reason: Reason::Timeout
        })
        .unwrap_err(),
        RecoveryError::WindowNotReduced {
            current: 50,
            requested: 60
        }
    );
}

/// Half-circle limit is enforced on wire ranges and window sizes.
#[test]
fn half_circle_limit_enforced() {
    let mut s = RecoverySender::new(0, 100).unwrap();
    let half = u32::try_from(sender_recovery::HALF).unwrap();
    assert!(matches!(
        s.apply(send(0, half, FIRST_ATTEMPT)).unwrap_err(),
        RecoveryError::RangeTooLarge(_)
    ));
    assert_eq!(
        RecoverySender::new(0, sender_recovery::HALF).unwrap_err(),
        RecoveryError::InvalidCwnd(sender_recovery::HALF)
    );
}

/// Snapshot distinguishes first-send bytes, sacked bytes and the bytes actually
/// retransmitted inside the active round.
#[test]
fn snapshot_separates_send_classes() {
    let mut s = RecoverySender::new(500, 100).unwrap();
    s.apply(send(500, 600, FIRST_ATTEMPT)).unwrap();
    s.apply(sack(&[(580, 590)])).unwrap();
    s.apply(loss(&[(520, 540)])).unwrap();
    s.apply(Event::EnterRecovery {
        round: 3,
        reduced_cwnd: 40,
        reason: Reason::FastRetransmit,
    })
    .unwrap();
    s.apply(send(520, 535, 2)).unwrap();
    s.apply(send(535, 540, 3)).unwrap();

    let snap = s.snapshot();
    assert_eq!(snap.first_sent, vec![WireRange::new(500, 600)]);
    assert_eq!(snap.sacked, vec![WireRange::new(580, 590)]);
    assert!(snap.lost.is_empty()); // retransmitted bytes leave the lost set
    let round = snap.rounds.get(&3).unwrap();
    assert_eq!(round.retransmitted, vec![WireRange::new(520, 540)]);

    // History retains both attempts and the original send class.
    let a1 = snap.attempts.get(&FIRST_ATTEMPT).unwrap();
    assert!(a1.first_send);
    assert_eq!(a1.ranges, vec![WireRange::new(500, 600)]);
    let a2 = snap.attempts.get(&2).unwrap();
    assert!(!a2.first_send);
    assert_eq!(a2.ranges, vec![WireRange::new(520, 535)]);

    // Cumulative ack truncates live sets but preserves attempt history.
    let snap = s.apply(Event::CumAck(600)).unwrap();
    assert!(snap.first_sent.is_empty());
    assert!(snap.sacked.is_empty());
    assert!(snap.attempts.contains_key(&3));
}

/// A timeout before any retransmission makes every outstanding byte a
/// candidate again; subsequent repairs in the same round still cannot
/// withdraw because the timeout flag is sticky.
#[test]
fn timeout_then_repair_remains_blocked_from_withdrawal() {
    let mut s = RecoverySender::new(0, 100).unwrap();
    s.apply(send(0, 40, FIRST_ATTEMPT)).unwrap();
    s.apply(Event::EnterRecovery {
        round: 1,
        reduced_cwnd: 40,
        reason: Reason::Timeout,
    })
    .unwrap();

    s.apply(Event::Timeout).unwrap();
    // All 40 outstanding bytes are now lost and retransmittable.
    assert_eq!(
        s.snapshot().next_retransmit,
        vec![WireRange::new(0, 40)]
    );

    // Repair under fresh attempts; even complete evidence cannot withdraw.
    s.apply(send(0, 40, 2)).unwrap();
    s.apply(dup(0, 40, 2)).unwrap();
    let snap = s.apply(Event::CumAck(40)).unwrap();
    assert_eq!(snap.active_round, Some(1));
    assert_eq!(snap.cwnd, 40);
    assert!(snap.rounds[&1].timed_out);
    assert!(snap.rounds[&1].evidence_complete);
    assert!(snap.rounds[&1].crossed_boundary);
}

/// Direct check that duplicate evidence tagged with another round's attempt
/// number never satisfies the current round, even when the byte ranges are
/// identical across overlapping rounds.
#[test]
fn overlapping_rounds_never_borrow_attempt_evidence() {
    let mut s = RecoverySender::new(0, 100).unwrap();
    s.apply(send(0, 50, FIRST_ATTEMPT)).unwrap();
    s.apply(loss(&[(10, 20)])).unwrap();

    // Round 1 retransmits 10..20 under attempt 2.
    s.apply(Event::EnterRecovery {
        round: 1,
        reduced_cwnd: 60,
        reason: Reason::FastRetransmit,
    })
    .unwrap();
    s.apply(send(10, 20, 2)).unwrap();

    // Round 2 needs a fresh loss marker for the same bytes before they are
    // retransmittable again under a new attempt.
    s.apply(loss(&[(10, 20)])).unwrap();
    s.apply(Event::EnterRecovery {
        round: 2,
        reduced_cwnd: 30,
        reason: Reason::Reordering,
    })
    .unwrap();
    s.apply(send(10, 20, 3)).unwrap();

    // Only round 1's attempt gets evidence.
    let snap = s.apply(dup(10, 20, 2)).unwrap();
    assert!(snap.rounds[&1].evidence_complete);
    assert!(!snap.rounds[&2].evidence_complete);
    assert_eq!(
        snap.rounds[&2].duplicate_evidence,
        Vec::<WireRange>::new()
    );

    // Boundary crossing alone cannot withdraw round 2.
    let snap = s.apply(Event::CumAck(50)).unwrap();
    assert_eq!(snap.active_round, Some(2));
    assert_eq!(snap.cwnd, 30);

    // The correct attempt's evidence unblocks round 2 immediately.
    let snap = s.apply(dup(10, 20, 3)).unwrap();
    assert_eq!(snap.active_round, None);
    assert_eq!(snap.cwnd, 100);
    assert!(snap.rounds[&2].withdrawn);
    assert!(snap.rounds[&1].withdrawn);
}

/// Withdrawal requires the cumulative ack to actually reach the recovery
/// boundary; stopping one byte short keeps the round active, and the very next
/// ack event completes it without any new evidence.
#[test]
fn boundary_must_be_reached_exactly_before_withdrawal() {
    let mut s = RecoverySender::new(0, 100).unwrap();
    s.apply(send(0, 30, FIRST_ATTEMPT)).unwrap();
    s.apply(loss(&[(5, 10)])).unwrap();
    s.apply(Event::EnterRecovery {
        round: 1,
        reduced_cwnd: 40,
        reason: Reason::FastRetransmit,
    })
    .unwrap();
    s.apply(send(5, 10, 2)).unwrap();
    s.apply(dup(5, 10, 2)).unwrap();

    let snap = s.apply(Event::CumAck(29)).unwrap();
    assert_eq!(snap.active_round, Some(1));
    assert!(!snap.rounds[&1].crossed_boundary);
    assert_eq!(snap.cwnd, 40);

    let snap = s.apply(Event::CumAck(30)).unwrap();
    assert_eq!(snap.active_round, None);
    assert_eq!(snap.cwnd, 100);
}

/// SACK, revoke, re-SACK and loss marking interleaved on overlapping ranges
/// reconcile byte sets exactly instead of double counting anything.
#[test]
fn interleaved_sack_revoke_loss_reconciles_byte_sets() {
    let mut s = RecoverySender::new(0, 100).unwrap();
    s.apply(send(0, 40, FIRST_ATTEMPT)).unwrap();

    s.apply(sack(&[(10, 30)])).unwrap();
    assert_eq!(s.in_flight(), 20);

    // Revoke only the middle; edges stay sacked.
    s.apply(revoke(&[(15, 25)])).unwrap();
    assert_eq!(
        s.sacked_flat(),
        vec![(s.flat_una() + 10, s.flat_una() + 15), (s.flat_una() + 25, s.flat_una() + 30)]
    );
    assert_eq!(
        s.revoked_flat(),
        vec![(s.flat_una() + 15, s.flat_una() + 25)]
    );
    assert_eq!(s.in_flight(), 30);

    // A loss mark over a sacked edge moves it out of SACK into the lost set.
    s.apply(loss(&[(25, 30)])).unwrap();
    assert_eq!(
        s.sacked_flat(),
        vec![(s.flat_una() + 10, s.flat_una() + 15)]
    );
    assert!(s.lost_flat().contains(&(s.flat_una() + 25, s.flat_una() + 30)));

    // A fresh SACK repairs the revoked middle and clears its repair state.
    s.apply(sack(&[(15, 25)])).unwrap();
    assert!(s.revoked_flat().is_empty());
    assert_eq!(s.in_flight(), 25); // 0..10 and 25..40 outstanding

    // Cumulative ack truncates live sets; sacked 12..25 remains non-in-flight
    // while only 25..40 (15 bytes) is outstanding.
    let snap = s.apply(Event::CumAck(12)).unwrap();
    assert_eq!(snap.in_flight, 15);
    assert_eq!(snap.sacked, vec![WireRange::new(12, 25)]);
}

/// A retransmission may cover only part of a lost original segment, and the
/// remaining tail stays the next retransmittable range.
#[test]
fn partial_retransmission_leaves_tail_retransmittable() {
    let mut s = RecoverySender::new(0, 100).unwrap();
    s.apply(send(0, 50, FIRST_ATTEMPT)).unwrap();
    // One original segment 10..40 is lost wholesale.
    s.apply(loss(&[(10, 40)])).unwrap();
    s.apply(Event::EnterRecovery {
        round: 1,
        reduced_cwnd: 50,
        reason: Reason::FastRetransmit,
    })
    .unwrap();

    // Repair only the first quarter of the original segment.
    let snap = s.apply(send(10, 18, 2)).unwrap();
    assert_eq!(snap.next_retransmit, vec![WireRange::new(18, 40)]);
    assert_eq!(
        s.lost_flat(),
        vec![(s.flat_una() + 18, s.flat_una() + 40)]
    );
    assert_eq!(s.in_flight(), 50);
}

