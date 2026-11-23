//! # Sender-side recovery module
//!
//! A deterministic, event-sequence-driven sender-side loss-recovery state
//! machine for a transport stack. Sequence numbers wrap modulo 2^32 on the
//! wire; internally they are unwrapped to a monotonic 64-bit space so that
//! byte-level set arithmetic stays exact across wrap points and arbitrary
//! segment splits.
//!
//! Start at [`RecoverySender`], feed [`Event`] values through
//! [`RecoverySender::apply`], and inspect the returned [`Snapshot`] after each
//! event.
//!
//! ## Guarantees
//!
//! * In-flight counts are **distinct** sent bytes not yet cumulatively or
//!   selectively acknowledged.
//! * Retransmissions always target the lowest-sequence lost or revoked bytes
//!   first and may cover partial original segments.
//! * Recovery rounds save the window and unacknowledged boundary on entry;
//!   duplicate evidence is bound to attempt numbers and can never be borrowed
//!   between overlapping rounds.
//! * A round's window reduction is withdrawn only when all its retransmitted
//!   bytes carry matching-attempt duplicate evidence, the cumulative ack has
//!   crossed the recovery boundary, and no timeout was observed meanwhile.

mod intervals;
mod recovery;
mod seq;

pub use recovery::{
    AttemptView, DupEvent, Event, Reason, RecoveryError, RecoverySender, RoundView, SendEvent,
    Snapshot, FIRST_ATTEMPT,
};
pub use seq::{initial_flat, split_to_wire, unwrap_range, unwrap_value, Wire, WireRange, HALF, WRAP};

#[cfg(test)]
mod construction_tests {
    use super::*;

    #[test]
    fn rejects_invalid_initial_window() {
        assert_eq!(
            RecoverySender::new(0, 0).unwrap_err(),
            RecoveryError::InvalidCwnd(0)
        );
        assert_eq!(
            RecoverySender::new(0, HALF).unwrap_err(),
            RecoveryError::InvalidCwnd(HALF)
        );
    }

    #[test]
    fn fresh_sender_reports_empty_state() {
        let s = RecoverySender::new(100, 10).unwrap();
        assert_eq!(s.in_flight(), 0);
        assert_eq!(s.cwnd(), 10);
        assert!(s.next_retransmit_flat().is_none());
        assert!(s.active_round().is_none());
    }
}
