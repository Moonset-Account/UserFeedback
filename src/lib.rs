//! Deterministic causal dispatch for messages converging from several sources.
//!
//! Each [`Message`] carries the name of its source, a per-source sequence number
//! that starts at `1`, and a dependency vector: for every source it names, the
//! sequence number that must already have been *dispatched* before this message
//! may be dispatched. Sources that are not named in the vector have a
//! requirement of `0` (nothing).
//!
//! [`CausalDispatcher`] is a deterministic state machine:
//!
//! * a message is dispatchable only when it is the next sequence number of its
//!   own source **and** every dependency is satisfied;
//! * after every receipt the dispatcher releases as many messages as possible,
//!   one at a time, re-evaluating readiness after each release;
//! * when several messages are dispatchable at the same moment, the one with the
//!   lexicographically smallest `(source, seq)` pair goes first (see
//!   [`MessageId`]'s `Ord` implementation);
//! * messages that are still blocked stay in a waiting set, and a re-sent
//!   message is never dispatched twice.
//!
//! [`CausalDispatcher::checkpoint`] captures both the contiguous progress of
//! every source and the messages that are still waiting, so a receiver can be
//! closed and reopened without losing work or reordering delivery.
//!
//! # Example
//!
//! ```
//! use causal_dispatch::{CausalDispatcher, Message};
//!
//! let mut receiver = CausalDispatcher::new();
//!
//! // "dc-b" number 1 may only be dispatched after "dc-a" number 2.
//! let b1 = Message::new("dc-b", 1, "b1").depends_on("dc-a", 2);
//! assert!(receiver.receive(b1).unwrap().is_empty());
//!
//! assert_eq!(receiver.receive(Message::new("dc-a", 1, "a1")).unwrap().len(), 1);
//! let released = receiver.receive(Message::new("dc-a", 2, "a2")).unwrap();
//! let payloads: Vec<_> = released.iter().map(|m| m.payload).collect();
//! assert_eq!(payloads, ["a2", "b1"]);
//! ```

use std::collections::{btree_map, BTreeMap};
use std::error::Error;
use std::fmt;

/// A per-source sequence number. Sequence numbers start at `1`.
pub type Seq = u64;

/// Identity and ordering key of a message: `(source, seq)`.
///
/// The derived [`Ord`] implementation compares `source` first and `seq` second.
/// String comparison is the usual byte-wise (UTF-8) lexicographic order, so
/// e.g. `"dc-10" < "dc-2"` and `"Z" < "a"`.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct MessageId {
    /// Name of the source (data center, link, publisher, ...).
    pub source: String,
    /// Source-local sequence number, starting at `1`.
    pub seq: Seq,
}

impl fmt::Display for MessageId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}/{}", self.source, self.seq)
    }
}

/// One event arriving at the receiver.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Message<T> {
    /// Name of the source that published this message.
    pub source: String,
    /// Source-local sequence number; must be at least `1`.
    pub seq: Seq,
    /// Required already-dispatched sequence per source.
    ///
    /// A source that is absent from the map requires nothing. A source mapped
    /// to `n` requires that source to have dispatched `n` already (inclusive:
    /// `n` itself must be dispatched).
    pub deps: BTreeMap<String, Seq>,
    /// Application payload. The dispatcher never interprets it.
    pub payload: T,
}

impl<T> Message<T> {
    /// Creates a message with an empty dependency vector.
    pub fn new(source: impl Into<String>, seq: Seq, payload: T) -> Self {
        Self {
            source: source.into(),
            seq,
            deps: BTreeMap::new(),
            payload,
        }
    }

    /// Adds one dependency: before this message is dispatched, `source` must
    /// already have dispatched sequence number `seq`.
    ///
    /// If the same source is added more than once the **larger** requirement is
    /// kept, so repeated calls can only make the dependency stricter. A
    /// dependency may never be loosened by accident.
    pub fn depends_on(mut self, source: impl Into<String>, seq: Seq) -> Self {
        let source = source.into();
        let entry = self.deps.entry(source).or_insert(seq);
        *entry = (*entry).max(seq);
        self
    }

    /// The `(source, seq)` identity of this message.
    pub fn id(&self) -> MessageId {
        MessageId {
            source: self.source.clone(),
            seq: self.seq,
        }
    }
}

/// A message was rejected before it entered the receiver.
///
/// A rejected message does not change dispatcher state at all.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DispatchError {
    /// The message's source name was empty.
    EmptySource,
    /// A dependency named the empty source.
    EmptyDependencySource,
    /// The sequence number was `0`; source sequences start at `1`.
    ZeroSequence {
        /// The source that published the invalid message.
        source: String,
    },
    /// The message declared that its own source must already be at `required`,
    /// which is impossible because this message *is* sequence `seq`.
    SelfDependencyNotInPast {
        /// The source that published the invalid message.
        source: String,
        /// Sequence number of the rejected message.
        seq: Seq,
        /// The impossible requirement on the message's own source.
        required: Seq,
    },
}

impl fmt::Display for DispatchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DispatchError::EmptySource => write!(f, "message source must not be empty"),
            DispatchError::EmptyDependencySource => {
                write!(f, "a dependency names the empty source")
            }
            DispatchError::ZeroSequence { source } => {
                write!(f, "source {source:?} published sequence 0; sequences start at 1")
            }
            DispatchError::SelfDependencyNotInPast {
                source,
                seq,
                required,
            } => write!(
                f,
                "message {source}/{seq} requires its own source to be at {required}, \
                 but only {source}/{} could precede it",
                seq.saturating_sub(1)
            ),
        }
    }
}

impl Error for DispatchError {}

/// Snapshot of a [`CausalDispatcher`].
///
/// It contains the contiguous progress of every source and every message that
/// is still waiting, plus the two counters. Restoring it reconstructs a
/// receiver in exactly the same state.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Checkpoint<T> {
    progressed: BTreeMap<String, Seq>,
    pending: BTreeMap<MessageId, Message<T>>,
    dispatched: u64,
    ignored_duplicates: u64,
}

impl<T> Checkpoint<T> {
    /// Highest contiguously dispatched sequence for `source` (`0` if none).
    pub fn progress(&self, source: &str) -> Seq {
        self.progressed.get(source).copied().unwrap_or(0)
    }

    /// All sources with non-zero progress.
    pub fn progressed(&self) -> &BTreeMap<String, Seq> {
        &self.progressed
    }

    /// Identities of the messages that were still waiting, in the order they
    /// would be considered for dispatch (`(source, seq)` order).
    pub fn waiting_ids(&self) -> impl Iterator<Item = &MessageId> {
        self.pending.keys()
    }

    /// Number of messages that were still waiting.
    pub fn pending_len(&self) -> usize {
        self.pending.len()
    }

    /// Number of messages dispatched before the checkpoint was taken.
    pub fn dispatched_count(&self) -> u64 {
        self.dispatched
    }

    /// Number of duplicate deliveries ignored before the checkpoint was taken.
    pub fn ignored_duplicates(&self) -> u64 {
        self.ignored_duplicates
    }
}

/// Deterministic causal dispatch state machine.
///
/// Messages enter through [`receive`](Self::receive); every call returns the
/// messages that became dispatchable, in dispatch order. The caller is expected
/// to deliver them downstream in exactly that order.
#[derive(Clone, Debug)]
pub struct CausalDispatcher<T> {
    /// Highest contiguously dispatched sequence per source.
    progressed: BTreeMap<String, Seq>,
    /// Messages that are not dispatchable yet, ordered by `(source, seq)`.
    pending: BTreeMap<MessageId, Message<T>>,
    dispatched: u64,
    ignored_duplicates: u64,
}

impl<T> Default for CausalDispatcher<T> {
    fn default() -> Self {
        Self::new()
    }
}

impl<T> CausalDispatcher<T> {
    /// Creates an empty receiver.
    pub fn new() -> Self {
        Self {
            progressed: BTreeMap::new(),
            pending: BTreeMap::new(),
            dispatched: 0,
            ignored_duplicates: 0,
        }
    }

    /// Accepts one message and returns every message that can now be
    /// dispatched, in order.
    ///
    /// The returned vector is a prefix of the receiver's future delivery order:
    /// if it contains several messages, each one was only released after
    /// re-checking readiness, so later entries may depend on earlier ones.
    ///
    /// A message that was already dispatched, or that is already waiting, is a
    /// duplicate: it is counted in
    /// [`ignored_duplicates`](Self::ignored_duplicates) and otherwise ignored.
    /// The first copy received wins.
    ///
    /// # Errors
    ///
    /// Returns a [`DispatchError`] for malformed messages (empty source, zero
    /// sequence, or a self-dependency that is not in the past). State is left
    /// unchanged.
    pub fn receive(&mut self, message: Message<T>) -> Result<Vec<Message<T>>, DispatchError> {
        self.validate(&message)?;

        let id = message.id();
        if message.seq <= self.progress(&id.source) {
            self.ignored_duplicates += 1;
            return Ok(Vec::new());
        }
        if self.pending.contains_key(&id) {
            self.ignored_duplicates += 1;
            return Ok(Vec::new());
        }

        self.pending.insert(id, message);
        Ok(self.drain_ready())
    }

    /// Highest contiguously dispatched sequence for `source` (`0` if none).
    pub fn progress(&self, source: &str) -> Seq {
        self.progressed.get(source).copied().unwrap_or(0)
    }

    /// All sources with non-zero progress.
    pub fn progressed(&self) -> &BTreeMap<String, Seq> {
        &self.progressed
    }

    /// The messages that are still waiting, in `(source, seq)` order.
    pub fn waiting(&self) -> btree_map::Values<'_, MessageId, Message<T>> {
        self.pending.values()
    }

    /// Identities of the messages that are still waiting, in `(source, seq)`
    /// order.
    pub fn waiting_ids(&self) -> btree_map::Keys<'_, MessageId, Message<T>> {
        self.pending.keys()
    }

    /// Number of messages that are still waiting.
    pub fn pending_len(&self) -> usize {
        self.pending.len()
    }

    /// Whether `(source, seq)` is currently waiting to be dispatched.
    pub fn is_pending(&self, source: &str, seq: Seq) -> bool {
        self.pending
            .contains_key(&MessageId { source: source.to_owned(), seq })
    }

    /// Number of messages dispatched so far.
    pub fn dispatched_count(&self) -> u64 {
        self.dispatched
    }

    /// Number of duplicate deliveries ignored so far.
    pub fn ignored_duplicates(&self) -> u64 {
        self.ignored_duplicates
    }

    /// Captures progress and waiting messages.
    ///
    /// Restoring the returned checkpoint with [`Self::restore`] yields a
    /// receiver that continues exactly where this one was. The checkpoint is a
    /// value and may be restored more than once.
    pub fn checkpoint(&self) -> Checkpoint<T>
    where
        T: Clone,
    {
        Checkpoint {
            progressed: self.progressed.clone(),
            pending: self.pending.clone(),
            dispatched: self.dispatched,
            ignored_duplicates: self.ignored_duplicates,
        }
    }

    /// Rebuilds a receiver from a checkpoint.
    ///
    /// A checkpoint is always taken in a quiescent state (every `receive`
    /// drains before returning), so nothing in it is dispatchable and restore
    /// cannot drop a message. Restoring the same checkpoint twice yields two
    /// independent receivers in identical states.
    pub fn restore(checkpoint: Checkpoint<T>) -> Self {
        Self {
            progressed: checkpoint.progressed,
            pending: checkpoint.pending,
            dispatched: checkpoint.dispatched,
            ignored_duplicates: checkpoint.ignored_duplicates,
        }
    }

    fn validate(&self, message: &Message<T>) -> Result<(), DispatchError> {
        if message.source.is_empty() {
            return Err(DispatchError::EmptySource);
        }
        if message.seq == 0 {
            return Err(DispatchError::ZeroSequence {
                source: message.source.clone(),
            });
        }
        for (source, required) in &message.deps {
            if source.is_empty() {
                return Err(DispatchError::EmptyDependencySource);
            }
            if *source == message.source && *required >= message.seq {
                return Err(DispatchError::SelfDependencyNotInPast {
                    source: message.source.clone(),
                    seq: message.seq,
                    required: *required,
                });
            }
        }
        Ok(())
    }

    fn is_ready(&self, message: &Message<T>) -> bool {
        if self.progress(&message.source).checked_add(1) != Some(message.seq) {
            return false;
        }
        message
            .deps
            .iter()
            .all(|(source, required)| self.progress(source) >= *required)
    }

    /// Releases ready messages one at a time. Because `pending` is keyed by
    /// `MessageId` and `MessageId`'s `Ord` is `(source, seq)`, the first ready
    /// entry found on each pass is exactly the lexicographically smallest one.
    fn drain_ready(&mut self) -> Vec<Message<T>> {
        let mut released = Vec::new();
        loop {
            let next = self
                .pending
                .iter()
                .find(|(_, message)| self.is_ready(message))
                .map(|(id, _)| id.clone());

            let Some(id) = next else { break };
            let message = self
                .pending
                .remove(&id)
                .expect("id came from the pending map");
            self.progressed.insert(message.source.clone(), message.seq);
            self.dispatched += 1;
            released.push(message);
        }
        released
    }
}
