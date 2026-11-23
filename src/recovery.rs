//! Event-driven sender-side loss recovery.
//!
//! A [`RecoverySender`] is fed an [`Event`] sequence describing everything the
//! sender observes: original sends and retransmissions (tagged with an attempt
//! number), cumulative acknowledgements, selective acknowledgements, explicit
//! SACK revocations, loss markings, duplicate-reception evidence and timeouts.
//! After every event [`RecoverySender::snapshot`] reports the next
//! retransmittable range, the distinct in-flight byte count and the current
//! congestion window.
//!
//! ## Model
//!
//! * Wire sequence numbers are modular `u32`; internally they are unwrapped to
//!   a monotonic `u64` space (see [`crate::seq`]). Every active window must be
//!   smaller than half the circle (`2^31` bytes).
//! * In-flight bytes are the **distinct** bytes that have been sent but are
//!   neither cumulatively acknowledged nor selectively acknowledged. Lost,
//!   revoked and retransmitted bytes therefore stay in-flight until acked.
//! * Retransmission candidates are bytes marked lost or restored from a
//!   revoked SACK, lowest sequence number first; a retransmission may cover any
//!   sub-range of an original segment.
//! * Each recovery round saves the congestion window and the unacknowledged
//!   boundary on entry. A round's window reduction is withdrawn only when (1)
//!   every byte retransmitted in that round has duplicate-reception evidence
//!   carrying the matching attempt number, (2) the cumulative acknowledgement
//!   has crossed the round's recovery boundary, and (3) no timeout was observed
//!   while the round was active.

use crate::intervals::IntervalSet;
use crate::seq::{
    self, split_to_wire, unwrap_range, unwrap_value, WireRange,
};
use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

/// Attempt number used for the original (first) transmission of every byte.
pub const FIRST_ATTEMPT: u32 = 1;

/// Reasons a caller may declare when entering a recovery round.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reason {
    /// Entered on duplicate acknowledgements / fast retransmit.
    FastRetransmit,
    /// Entered after a retransmission timeout.
    Timeout,
    /// Entered because apparent reordering was detected.
    Reordering,
    /// Caller-defined reason code.
    Other(u8),
}

/// A send or retransmission event: bytes `[start, end)` transmitted with the
/// given attempt number. Attempt [`FIRST_ATTEMPT`] is the original send; every
/// retransmission must use a fresh attempt number.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SendEvent {
    /// First wire sequence number of the transmitted bytes.
    pub start: u32,
    /// One past the last transmitted wire sequence number.
    pub end: u32,
    /// Transmission attempt; 1 for original sends, 2+ for retransmissions.
    pub attempt: u32,
}

/// Duplicate-reception evidence for bytes `[start, end)` of a specific attempt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DupEvent {
    /// First wire sequence number covered by the duplicate report.
    pub start: u32,
    /// One past the last covered wire sequence number.
    pub end: u32,
    /// Attempt whose delivery was observed as a duplicate.
    pub attempt: u32,
}

/// Everything that can happen to the sender, in on-the-wire order.
#[derive(Debug, Clone)]
pub enum Event {
    /// Original send (`attempt == 1`) or retransmission (`attempt >= 2`).
    Send(SendEvent),
    /// Cumulative acknowledgement: every byte below the given number is acked.
    CumAck(u32),
    /// Selective acknowledgement blocks.
    Sack(Vec<WireRange>),
    /// Explicit revocation of previously advertised, not-yet-cum-acked SACK.
    Revoke(Vec<WireRange>),
    /// Loss-marker blocks (e.g. from a local loss detector).
    Loss(Vec<WireRange>),
    /// Duplicate reception evidence for a specific attempt.
    Dup(DupEvent),
    /// Retransmission timeout: outstanding non-SACKed bytes become lost and
    /// any active round is barred from withdrawing its window reduction.
    Timeout,
    /// Declare a new recovery round with a reduced congestion window.
    EnterRecovery {
        /// Caller-chosen unique round number.
        round: u32,
        /// Reduced congestion window to apply while the round is active.
        reduced_cwnd: u64,
        /// Why the round is being entered.
        reason: Reason,
    },
}

/// Errors returned when an event is inconsistent with the sender state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecoveryError {
    /// A range spans at least half the sequence-number circle.
    RangeTooLarge(String),
    /// An empty byte range was supplied where data is required.
    EmptyRange,
    /// Attempt number zero is reserved and illegal.
    InvalidAttempt,
    /// A retransmission attempt number was already used.
    AttemptReused(u32),
    /// Duplicate evidence references an attempt that was never transmitted.
    UnknownAttempt(u32),
    /// A retransmission was requested while no recovery round is active.
    NoActiveRound,
    /// Requested retransmission bytes are not currently marked lost or
    /// revoked (or are already selectively acknowledged).
    NotRetransmittable {
        /// Offending flat range.
        range: (u64, u64),
    },
    /// Original sends must extend the sent stream contiguously.
    NonContiguousSend {
        /// Expected flat start, actual flat start.
        expected: u64,
        actual: u64,
    },
    /// A send would exceed the current congestion window.
    CwndExceeded {
        /// Bytes that would be in flight after the send.
        would_be_in_flight: u64,
        /// Current congestion window.
        cwnd: u64,
    },
    /// A cumulative acknowledgement moves the ack point backwards.
    OldAck {
        /// Previous flat ack point.
        previous: u64,
        /// Requested flat ack point.
        requested: u64,
    },
    /// A congestion window value is zero or at least half the circle.
    InvalidCwnd(u64),
    /// A round number was reused while still known to the sender.
    DuplicateRound(u32),
    /// The reduced window is not strictly below the current window.
    WindowNotReduced {
        /// Current congestion window.
        current: u64,
        /// Requested reduced window.
        requested: u64,
    },
}

impl fmt::Display for RecoveryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RecoveryError::RangeTooLarge(m) => write!(f, "{m}"),
            RecoveryError::EmptyRange => write!(f, "empty byte range"),
            RecoveryError::InvalidAttempt => write!(f, "attempt zero is reserved"),
            RecoveryError::AttemptReused(a) => {
                write!(f, "retransmission attempt {a} was already used")
            }
            RecoveryError::UnknownAttempt(a) => {
                write!(f, "duplicate evidence for unknown attempt {a}")
            }
            RecoveryError::NoActiveRound => {
                write!(f, "no active recovery round for retransmission")
            }
            RecoveryError::NotRetransmittable { range: (s, e) } => {
                write!(f, "flat range [{s}, {e}) is not retransmittable")
            }
            RecoveryError::NonContiguousSend { expected, actual } => {
                write!(
                    f,
                    "original send must be contiguous: expected {expected}, got {actual}"
                )
            }
            RecoveryError::CwndExceeded {
                would_be_in_flight,
                cwnd,
            } => write!(
                f,
                "send would raise in-flight to {would_be_in_flight}, cwnd is {cwnd}"
            ),
            RecoveryError::OldAck {
                previous,
                requested,
            } => write!(
                f,
                "cumulative ack moved backwards: {requested} < {previous}"
            ),
            RecoveryError::InvalidCwnd(c) => {
                write!(f, "invalid congestion window {c}")
            }
            RecoveryError::DuplicateRound(r) => {
                write!(f, "recovery round {r} already exists")
            }
            RecoveryError::WindowNotReduced { current, requested } => {
                write!(
                    f,
                    "reduced window {requested} is not below current window {current}"
                )
            }
        }
    }
}

impl Error for RecoveryError {}

/// Per-round information in a [`Snapshot`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RoundView {
    /// Round number declared on entry.
    pub round: u32,
    /// Whether the round is still active (its window reduction is in force).
    pub active: bool,
    /// Whether the round's window reduction has already been withdrawn.
    pub withdrawn: bool,
    /// Reason given on entry.
    pub reason: Reason,
    /// Congestion window saved on entry, restored when the round withdraws.
    pub saved_cwnd: u64,
    /// Unacknowledged sequence boundary at entry.
    pub entry_una: u32,
    /// Recovery boundary: the highest sent byte at entry.
    pub recovery_point: u32,
    /// A timeout occurred while the round was active.
    pub timed_out: bool,
    /// Every byte retransmitted during the round (all attempts).
    pub retransmitted: Vec<WireRange>,
    /// Bytes with matching-attempt duplicate evidence, over the round.
    pub duplicate_evidence: Vec<WireRange>,
    /// All retransmitted bytes carry matching-attempt duplicate evidence.
    pub evidence_complete: bool,
    /// The cumulative ack has reached the recovery boundary.
    pub crossed_boundary: bool,
    /// All three withdrawal conditions currently hold.
    pub withdrawable: bool,
}

/// All wire ranges recorded for one transmission attempt.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttemptView {
    /// Attempt number.
    pub attempt: u32,
    /// Ranges sent under this attempt.
    pub ranges: Vec<WireRange>,
    /// Whether this is the original first-send attempt.
    pub first_send: bool,
}

/// State visible after each event.
#[derive(Debug, Clone)]
pub struct Snapshot {
    /// Current cumulative acknowledgement point on the wire.
    pub una: u32,
    /// Distinct sent bytes not yet cumulatively or selectively acknowledged.
    pub in_flight: u64,
    /// Current congestion window (reduced while rounds are active).
    pub cwnd: u64,
    /// Lowest-sequence contiguous retransmittable run, split at wrap points;
    /// empty when nothing is retransmittable.
    pub next_retransmit: Vec<WireRange>,
    /// Currently active round number, if any.
    pub active_round: Option<u32>,
    /// All rounds known to the sender, including withdrawn ones.
    pub rounds: BTreeMap<u32, RoundView>,
    /// Every transmission attempt ever recorded (history is retained).
    pub attempts: BTreeMap<u32, AttemptView>,
    /// Live originally-sent bytes still at or above `una`.
    pub first_sent: Vec<WireRange>,
    /// Live selectively-acknowledged bytes.
    pub sacked: Vec<WireRange>,
    /// Live bytes explicitly marked lost.
    pub lost: Vec<WireRange>,
    /// Live SACK bytes that were revoked and await repair.
    pub revoked: Vec<WireRange>,
}

#[derive(Debug)]
struct RoundState {
    reason: Reason,
    saved_cwnd: u64,
    entry_una_flat: u64,
    recovery_point_flat: u64,
    timed_out: bool,
    withdrawn: bool,
    /// Bytes retransmitted in this round, keyed by attempt number.
    retrans_by_attempt: BTreeMap<u32, IntervalSet>,
    /// Union of every retransmitted byte.
    retransmitted: IntervalSet,
}

impl RoundState {
    fn evidence_bytes(&self, dups: &BTreeMap<u32, IntervalSet>) -> IntervalSet {
        let mut covered = IntervalSet::new();
        for (attempt, bytes) in &self.retrans_by_attempt {
            if let Some(dup) = dups.get(attempt) {
                covered = covered.union(&dup.intersection(bytes));
            }
        }
        covered
    }

    fn evidence_complete(&self, dups: &BTreeMap<u32, IntervalSet>) -> bool {
        let covered = self.evidence_bytes(dups);
        self.retransmitted
            .iter()
            .all(|(s, e)| covered.contains_all(s, e))
    }
}

/// Sender-side recovery state machine.
#[derive(Debug)]
pub struct RecoverySender {
    una_flat: u64,
    max_sent_flat: Option<u64>,
    cwnd: u64,
    // Live sets, always truncated at `una_flat`.
    sent: IntervalSet,
    sacked: IntervalSet,
    lost: IntervalSet,
    revoked: IntervalSet,
    // Full history, never truncated.
    attempts: BTreeMap<u32, IntervalSet>,
    dups: BTreeMap<u32, IntervalSet>,
    rounds: BTreeMap<u32, RoundState>,
    entry_order: Vec<u32>,
    active: Option<u32>,
}

impl RecoverySender {
    /// Create a sender with initial wire sequence number `isn` and an initial
    /// congestion window of `initial_cwnd` bytes (must be below half circle).
    pub fn new(isn: u32, initial_cwnd: u64) -> Result<Self, RecoveryError> {
        if initial_cwnd == 0 || initial_cwnd >= crate::seq::HALF {
            return Err(RecoveryError::InvalidCwnd(initial_cwnd));
        }
        Ok(Self {
            una_flat: seq::initial_flat(isn),
            max_sent_flat: None,
            cwnd: initial_cwnd,
            sent: IntervalSet::new(),
            sacked: IntervalSet::new(),
            lost: IntervalSet::new(),
            revoked: IntervalSet::new(),
            attempts: BTreeMap::new(),
            dups: BTreeMap::new(),
            rounds: BTreeMap::new(),
            entry_order: Vec::new(),
            active: None,
        })
    }

    /// Flat (unwrapped) cumulative acknowledgement point.
    pub fn flat_una(&self) -> u64 {
        self.una_flat
    }

    /// Current congestion window in bytes.
    pub fn cwnd(&self) -> u64 {
        self.cwnd
    }

    /// Distinct in-flight byte count: sent bytes minus cumulative and
    /// selective acknowledgements.
    pub fn in_flight(&self) -> u64 {
        self.sent.difference(&self.sacked).covered_bytes()
    }

    /// Active round number, if any.
    pub fn active_round(&self) -> Option<u32> {
        self.active
    }

    /// Live originally-sent flat ranges at or above `una`.
    pub fn first_sent_flat(&self) -> Vec<(u64, u64)> {
        self.sent.iter().collect()
    }

    /// Live sacked flat ranges.
    pub fn sacked_flat(&self) -> Vec<(u64, u64)> {
        self.sacked.iter().collect()
    }

    /// Live lost flat ranges.
    pub fn lost_flat(&self) -> Vec<(u64, u64)> {
        self.lost.iter().collect()
    }

    /// Live revoked flat ranges.
    pub fn revoked_flat(&self) -> Vec<(u64, u64)> {
        self.revoked.iter().collect()
    }

    fn ref_flat(&self) -> u64 {
        self.max_sent_flat.unwrap_or(self.una_flat)
    }

    fn decode(&self, range: WireRange) -> Result<(u64, u64), RecoveryError> {
        if range.is_empty() {
            return Err(RecoveryError::EmptyRange);
        }
        unwrap_range(range, self.ref_flat()).map_err(RecoveryError::RangeTooLarge)
    }

    /// Intersect a decoded flat range with the live sent bytes.
    fn clamp_to_sent(&self, start: u64, end: u64) -> IntervalSet {
        IntervalSet::from_range(start, end).intersection(&self.sent)
    }

    /// Apply one event and return the post-event snapshot.
    pub fn apply(&mut self, event: Event) -> Result<Snapshot, RecoveryError> {
        match event {
            Event::Send(ev) => self.handle_send(ev)?,
            Event::CumAck(ack) => self.handle_cum_ack(ack)?,
            Event::Sack(blocks) => self.handle_sack(&blocks)?,
            Event::Revoke(blocks) => self.handle_revoke(&blocks)?,
            Event::Loss(blocks) => self.handle_loss(&blocks)?,
            Event::Dup(ev) => self.handle_dup(ev)?,
            Event::Timeout => self.handle_timeout(),
            Event::EnterRecovery {
                round,
                reduced_cwnd,
                reason,
            } => self.handle_enter_recovery(round, reduced_cwnd, reason)?,
        }
        self.maybe_withdraw_rounds();
        Ok(self.snapshot())
    }

    fn handle_send(&mut self, ev: SendEvent) -> Result<(), RecoveryError> {
        if ev.attempt == 0 {
            return Err(RecoveryError::InvalidAttempt);
        }
        let (start, end) = self.decode(WireRange::new(ev.start, ev.end))?;

        if ev.attempt == FIRST_ATTEMPT {
            let expected = self.max_sent_flat.unwrap_or(self.una_flat);
            if start != expected {
                return Err(RecoveryError::NonContiguousSend {
                    expected,
                    actual: start,
                });
            }
            let added = end - start;
            let would_be = self.in_flight() + added;
            if would_be > self.cwnd {
                return Err(RecoveryError::CwndExceeded {
                    would_be_in_flight: would_be,
                    cwnd: self.cwnd,
                });
            }
            self.sent.insert(start, end);
            self.max_sent_flat = Some(end);
            self.attempts
                .entry(FIRST_ATTEMPT)
                .or_default()
                .insert(start, end);
            return Ok(());
        }

        // Retransmission: an active round is required, the attempt is fresh,
        // and every requested byte must currently be a repair candidate.
        let round_id = self.active.ok_or(RecoveryError::NoActiveRound)?;
        if self.attempts.contains_key(&ev.attempt) {
            return Err(RecoveryError::AttemptReused(ev.attempt));
        }
        let eligible = self
            .lost
            .union(&self.revoked)
            .difference(&self.sacked);
        if !eligible.contains_all(start, end) {
            return Err(RecoveryError::NotRetransmittable {
                range: (start, end),
            });
        }

        self.sent.insert(start, end);
        self.lost.remove(start, end);
        self.revoked.remove(start, end);
        self.attempts
            .entry(ev.attempt)
            .or_default()
            .insert(start, end);

        let round = self.rounds.get_mut(&round_id).expect("active round exists");
        round.retransmitted.insert(start, end);
        round
            .retrans_by_attempt
            .entry(ev.attempt)
            .or_default()
            .insert(start, end);
        Ok(())
    }

    fn handle_cum_ack(&mut self, ack: u32) -> Result<(), RecoveryError> {
        let flat = unwrap_value(ack, self.una_flat);
        if flat < self.una_flat {
            return Err(RecoveryError::OldAck {
                previous: self.una_flat,
                requested: flat,
            });
        }
        if flat > self.una_flat + crate::seq::HALF {
            return Err(RecoveryError::RangeTooLarge(format!(
                "cumulative ack {ack} is more than half a circle ahead"
            )));
        }
        self.una_flat = flat;
        for set in [
            &mut self.sent,
            &mut self.sacked,
            &mut self.lost,
            &mut self.revoked,
        ] {
            set.truncate_below(flat);
        }
        Ok(())
    }

    fn handle_sack(&mut self, blocks: &[WireRange]) -> Result<(), RecoveryError> {
        for &block in blocks {
            let (start, end) = self.decode(block)?;
            let inside = self.clamp_to_sent(start, end);
            for (s, e) in inside.iter() {
                self.sacked.insert(s, e);
                self.lost.remove(s, e);
                self.revoked.remove(s, e);
            }
        }
        Ok(())
    }

    fn handle_revoke(&mut self, blocks: &[WireRange]) -> Result<(), RecoveryError> {
        for &block in blocks {
            let (start, end) = self.decode(block)?;
            let live = IntervalSet::from_range(start, end).intersection(&self.sacked);
            for (s, e) in live.iter() {
                self.sacked.remove(s, e);
                self.revoked.insert(s, e);
            }
        }
        Ok(())
    }

    fn handle_loss(&mut self, blocks: &[WireRange]) -> Result<(), RecoveryError> {
        for &block in blocks {
            let (start, end) = self.decode(block)?;
            let inside = self.clamp_to_sent(start, end);
            for (s, e) in inside.iter() {
                self.sacked.remove(s, e);
                self.lost.insert(s, e);
            }
        }
        Ok(())
    }

    fn handle_dup(&mut self, ev: DupEvent) -> Result<(), RecoveryError> {
        if ev.attempt == 0 {
            return Err(RecoveryError::InvalidAttempt);
        }
        if !self.attempts.contains_key(&ev.attempt) {
            return Err(RecoveryError::UnknownAttempt(ev.attempt));
        }
        let (start, end) = self.decode(WireRange::new(ev.start, ev.end))?;
        let known = IntervalSet::from_range(start, end)
            .intersection(self.attempts.get(&ev.attempt).expect("checked above"));
        if !known.is_empty() {
            let entry = self.dups.entry(ev.attempt).or_default();
            for (s, e) in known.iter() {
                entry.insert(s, e);
            }
        }
        Ok(())
    }

    fn handle_timeout(&mut self) {
        if let Some(round_id) = self.active {
            let round = self.rounds.get_mut(&round_id).expect("active round exists");
            round.timed_out = true;
        }
        // Every outstanding non-SACKed byte is treated as lost.
        let outstanding = self.sent.difference(&self.sacked);
        self.lost = self.lost.union(&outstanding);
    }

    fn handle_enter_recovery(
        &mut self,
        round: u32,
        reduced_cwnd: u64,
        reason: Reason,
    ) -> Result<(), RecoveryError> {
        if reduced_cwnd == 0 || reduced_cwnd >= crate::seq::HALF {
            return Err(RecoveryError::InvalidCwnd(reduced_cwnd));
        }
        if reduced_cwnd >= self.cwnd {
            return Err(RecoveryError::WindowNotReduced {
                current: self.cwnd,
                requested: reduced_cwnd,
            });
        }
        if self.rounds.contains_key(&round) {
            return Err(RecoveryError::DuplicateRound(round));
        }
        let state = RoundState {
            reason,
            saved_cwnd: self.cwnd,
            entry_una_flat: self.una_flat,
            recovery_point_flat: self.max_sent_flat.unwrap_or(self.una_flat),
            timed_out: false,
            withdrawn: false,
            retrans_by_attempt: BTreeMap::new(),
            retransmitted: IntervalSet::new(),
        };
        self.rounds.insert(round, state);
        self.entry_order.push(round);
        self.active = Some(round);
        self.cwnd = reduced_cwnd;
        Ok(())
    }

    fn can_withdraw(&self, round_id: u32) -> bool {
        let round = self.rounds.get(&round_id).expect("round exists");
        if round.withdrawn || round.timed_out {
            return false;
        }
        if self.una_flat < round.recovery_point_flat {
            return false;
        }
        round.evidence_complete(&self.dups)
    }

    /// Withdraw rounds in LIFO order while their conditions hold.
    fn maybe_withdraw_rounds(&mut self) {
        loop {
            let Some(round_id) = self.active else {
                break;
            };
            if !self.can_withdraw(round_id) {
                break;
            }
            let round = self
                .rounds
                .get_mut(&round_id)
                .expect("active round exists");
            let restored = round.saved_cwnd;
            round.withdrawn = true;
            self.cwnd = restored;
            self.entry_order.pop();
            self.active = self.entry_order.last().copied();
        }
    }

    /// Lowest-sequence retransmittable run as flat `[start, end)`, if any.
    pub fn next_retransmit_flat(&self) -> Option<(u64, u64)> {
        if self.active.is_none() {
            return None;
        }
        let eligible = self
            .lost
            .union(&self.revoked)
            .difference(&self.sacked);
        let next = eligible.iter().next();
        next
    }

    /// Lowest-sequence retransmittable run limited to at most `max_bytes`,
    /// returned as flat `[start, end)`; lets callers change segmentation and
    /// re-check byte-by-byte.
    pub fn limited_next_retransmit_flat(&self, max_bytes: u64) -> Option<(u64, u64)> {
        let (start, end) = self.next_retransmit_flat()?;
        let capped = end.min(start.saturating_add(max_bytes.max(1)));
        Some((start, capped))
    }

    fn wire_ranges(set: &IntervalSet) -> Vec<WireRange> {
        set.iter()
            .flat_map(|(s, e)| split_to_wire(s, e))
            .collect()
    }

    fn round_view(&self, id: u32, round: &RoundState) -> RoundView {
        let evidence = round.evidence_bytes(&self.dups);
        let evidence_complete = round.evidence_complete(&self.dups);
        let crossed = self.una_flat >= round.recovery_point_flat;
        // Only the innermost active round can actually withdraw right now;
        // outer rounds are blocked until the nested round withdraws first.
        let is_innermost = self
            .entry_order
            .last()
            .copied()
            .map(|inner| inner == id)
            .unwrap_or(false);
        let withdrawable =
            is_innermost && !round.withdrawn && !round.timed_out && crossed && evidence_complete;
        RoundView {
            round: id,
            active: !round.withdrawn,
            withdrawn: round.withdrawn,
            reason: round.reason,
            saved_cwnd: round.saved_cwnd,
            entry_una: seq::wrap_value(round.entry_una_flat),
            recovery_point: seq::wrap_value(round.recovery_point_flat),
            timed_out: round.timed_out,
            retransmitted: Self::wire_ranges(&round.retransmitted),
            duplicate_evidence: Self::wire_ranges(&evidence),
            evidence_complete,
            crossed_boundary: crossed,
            withdrawable,
        }
    }

    /// Build the current observable state.
    pub fn snapshot(&self) -> Snapshot {
        let next_retransmit = match self.next_retransmit_flat() {
            Some((s, e)) => split_to_wire(s, e),
            None => Vec::new(),
        };
        let rounds = self
            .rounds
            .iter()
            .map(|(id, r)| (*id, self.round_view(*id, r)))
            .collect();
        let attempts = self
            .attempts
            .iter()
            .map(|(id, set)| {
                (
                    *id,
                    AttemptView {
                        attempt: *id,
                        ranges: Self::wire_ranges(set),
                        first_send: *id == FIRST_ATTEMPT,
                    },
                )
            })
            .collect();
        Snapshot {
            una: seq::wrap_value(self.una_flat),
            in_flight: self.in_flight(),
            cwnd: self.cwnd,
            next_retransmit,
            active_round: self.active,
            rounds,
            attempts,
            first_sent: Self::wire_ranges(&self.sent),
            sacked: Self::wire_ranges(&self.sacked),
            lost: Self::wire_ranges(&self.lost),
            revoked: Self::wire_ranges(&self.revoked),
        }
    }

    /// Round numbers still active, outer to inner.
    pub fn active_stack(&self) -> Vec<u32> {
        self.entry_order
            .iter()
            .copied()
            .filter(|id| {
                self.rounds
                    .get(id)
                    .map(|r| !r.withdrawn)
                    .unwrap_or(false)
            })
            .collect()
    }

    /// Attempt numbers that have ever transmitted bytes.
    pub fn known_attempts(&self) -> BTreeSet<u32> {
        self.attempts.keys().copied().collect()
    }
}
