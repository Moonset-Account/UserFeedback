//! Compressed bitmap-interval operator for filtering column batches.
//!
//! Inputs are half-open intervals keyed by global row number, in three classes:
//! visible rows, predicate-true rows and predicate-unknown rows. Columns arrive
//! in batches whose segment boundaries need not align. Expressions support
//! `AND`, `OR` and `NOT` under three-valued logic: an absent true/unknown bit
//! means false, and `NOT` complements only within valid visible rows while
//! preserving unknown. The operator emits only the maximal contiguous true
//! intervals, together with a row count and resumable pages.
//!
//! Opening a [`Query`] pins the input version: late interval corrections never
//! add or remove rows for queries that are already open. Memory and traversal
//! cost are proportional to the number of compressed segments, never to the
//! row span, so spans of a billion rows are handled without per-row expansion.

mod expr;
mod interval;
mod query;
mod store;

pub use expr::Expr;
pub use interval::{Interval, IntervalSet, Row};
pub use query::{Page, Query};
pub use store::{ColumnId, Kind, Store};
