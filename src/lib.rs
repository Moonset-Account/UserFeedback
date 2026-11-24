//! # Row pattern recognition over partition-ordered SQL streams
//!
//! This crate implements a streaming row pattern recognition operator in the
//! spirit of SQL `MATCH_RECOGNIZE`. Patterns are built programmatically with
//! [`pattern::Pattern`]; the operator is driven row by row with
//! [`recognizer::Recognizer::push_row`] and emits [`recognizer::Event`]s.
//!
//! ## Modules
//!
//! * [`tribool`] - three-valued logic for predicates, including unknown
//!   values produced by references to empty captures;
//! * [`pattern`] - the pattern AST: variables, concatenation, written-order
//!   alternation and greedy/lazy finite or open-ended repetition;
//! * [`nfa`] - Thompson NFA compilation where edge order encodes priority;
//! * [`recognizer`] - the streaming operator itself, with cross-batch state,
//!   bounded history retention, snapshots and partition boundaries.

pub mod nfa;
pub mod pattern;
pub mod recognizer;
pub mod tribool;

pub use nfa::{Edge, EdgeKind, Nfa};
pub use pattern::{Greed, Pattern, Repeat};
pub use recognizer::{
    BuildError, ClosureSource, Event, Match, PartitionStats, Recognizer, RecognizerSnapshot,
    RowCapture, RowSource, SkipMode,
};
pub use tribool::{eq_options, cmp_options, Tribool};
