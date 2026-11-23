use crate::expr::Expr;
use crate::interval::{Interval, IntervalSet, Row};
use crate::store::Store;

/// Three-valued truth sets for a (sub)expression, both clipped to visibility.
struct Truth {
    t: IntervalSet,
    u: IntervalSet,
}

/// One page of result intervals.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Page {
    /// Maximal contiguous true intervals in this page.
    pub intervals: Vec<Interval>,
    /// Row to resume from, or `None` when the result is exhausted.
    pub next_cursor: Option<Row>,
}

/// A query over a pinned input version.
///
/// The result (true rows intersected with visibility) is materialized as a
/// compressed [`IntervalSet`] when the query opens; corrections applied to
/// the [`Store`] afterwards never change what this query returns.
pub struct Query {
    result: IntervalSet,
    pinned_version: u64,
}

impl Query {
    /// Open a query against the store's current version and evaluate `expr`.
    pub fn open(store: &Store, expr: &Expr) -> Query {
        Self::open_at(store, expr, store.current_version())
    }

    /// Open a query pinned to an explicit (past or current) version.
    pub fn open_at(store: &Store, expr: &Expr, version: u64) -> Query {
        let visible = store.visibility_at(version);
        let truth = eval(expr, store, version, &visible);
        Query { result: truth.t, pinned_version: version }
    }

    /// Version this query is pinned to.
    pub fn pinned_version(&self) -> u64 {
        self.pinned_version
    }

    /// Number of true rows, computed from segment lengths (no expansion).
    pub fn count(&self) -> u64 {
        self.result.row_count()
    }

    /// All maximal contiguous true intervals.
    pub fn intervals(&self) -> &[Interval] {
        self.result.intervals()
    }

    /// Fetch up to `limit` result intervals starting at or after `cursor`
    /// (`None` starts from the beginning). If `cursor` falls inside an
    /// interval, the returned page begins with the tail of that interval, so
    /// pages never overlap and never lose rows. `next_cursor` is the end of
    /// the last returned interval while more rows remain, else `None`.
    pub fn page(&self, cursor: Option<Row>, limit: usize) -> Page {
        let start = cursor.unwrap_or(0);
        if limit == 0 {
            return Page { intervals: Vec::new(), next_cursor: Some(start) };
        }
        let mut intervals: Vec<Interval> = Vec::with_capacity(limit);
        for iv in self.result.intervals() {
            if iv.end <= start {
                continue;
            }
            if intervals.len() == limit {
                // More results remain beyond this page; resume where it ends.
                let next = intervals.last().map(|last| last.end).unwrap_or(start);
                return Page { intervals, next_cursor: Some(next) };
            }
            intervals.push(Interval { start: iv.start.max(start), end: iv.end });
        }
        Page { intervals, next_cursor: None }
    }
}

fn eval(expr: &Expr, store: &Store, version: u64, visible: &IntervalSet) -> Truth {
    match expr {
        Expr::Column(id) => {
            let (t, u) = store.column_at(*id, version);
            // A row's outcome is a single state; where a batch marks a row
            // as both true and unknown, unknown (null) dominates.
            Truth {
                t: t.subtract(&u).intersect(visible),
                u: u.intersect(visible),
            }
        }
        Expr::Not(inner) => {
            // NOT complements only within valid visible rows and keeps unknown.
            let r = eval(inner, store, version, visible);
            let known_true_or_unknown = r.t.union(&r.u);
            Truth {
                t: known_true_or_unknown.complement_within(visible),
                u: r.u,
            }
        }
        Expr::And(a, b) => {
            let (ra, rb) = (eval(a, store, version, visible), eval(b, store, version, visible));
            // false if either side is false; unknown only where neither side
            // can be false and at least one side is unknown.
            let possible_a = ra.t.union(&ra.u);
            let possible_b = rb.t.union(&rb.u);
            let u = ra
                .u
                .union(&rb.u)
                .intersect(&possible_a)
                .intersect(&possible_b);
            Truth { t: ra.t.intersect(&rb.t), u }
        }
        Expr::Or(a, b) => {
            let (ra, rb) = (eval(a, store, version, visible), eval(b, store, version, visible));
            // false only if both sides are false; unknown where at least one
            // side is unknown and neither side is true.
            let t = ra.t.union(&rb.t);
            let u = ra.u.union(&rb.u).subtract(&t);
            Truth { t, u }
        }
    }
}
