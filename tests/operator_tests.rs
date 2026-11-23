//! Integration tests: compressed operator vs. a per-row reference model.
//!
//! The reference model expands every correction into per-row state over a
//! small domain and evaluates the expression row by row with three-valued
//! logic. The compressed operator must agree with it exactly, while large
//! spans are checked structurally (counts and intervals, never expanded).

use bitmap_interval_filter::{Expr, Interval, IntervalSet, Kind, Query, Store};
use std::collections::HashMap;

fn iv(start: u64, end: u64) -> Interval {
    Interval::new(start, end)
}

fn set(ivs: &[(u64, u64)]) -> IntervalSet {
    IntervalSet::from_intervals(ivs.iter().map(|&(s, e)| iv(s, e)))
}

// ---------------------------------------------------------------------------
// Per-row reference model
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum V {
    False,
    Unknown,
    True,
}

/// Mirrors every store correction into per-row arrays over `[0, n)`.
struct Model {
    n: u64,
    visible: Vec<bool>,
    columns: HashMap<u64, (Vec<bool>, Vec<bool>)>, // (true, unknown)
}

impl Model {
    fn new(n: u64) -> Self {
        Model { n, visible: vec![false; n as usize], columns: HashMap::new() }
    }

    fn correct_visibility(&mut self, interval: Interval, insert: bool) {
        for r in interval.start..interval.end.min(self.n) {
            self.visible[r as usize] = insert;
        }
    }

    fn correct_column(&mut self, column: u64, kind: Kind, interval: Interval, insert: bool) {
        let entry = self.columns.entry(column).or_insert_with(|| {
            (vec![false; self.n as usize], vec![false; self.n as usize])
        });
        let bits = match kind {
            Kind::True => &mut entry.0,
            Kind::Unknown => &mut entry.1,
        };
        for r in interval.start..interval.end.min(self.n) {
            bits[r as usize] = insert;
        }
    }

    fn eval(&self, expr: &Expr, row: u64) -> V {
        match expr {
            Expr::Column(id) => match self.columns.get(id) {
                // Unknown (null) dominates when a row carries both bits.
                Some((_, u)) if u[row as usize] => V::Unknown,
                Some((t, _)) if t[row as usize] => V::True,
                _ => V::False,
            },
            Expr::Not(inner) => match self.eval(inner, row) {
                V::True => V::False,
                V::False => V::True,
                V::Unknown => V::Unknown,
            },
            Expr::And(a, b) => {
                let (x, y) = (self.eval(a, row), self.eval(b, row));
                if x == V::False || y == V::False {
                    V::False
                } else if x == V::True && y == V::True {
                    V::True
                } else {
                    V::Unknown
                }
            }
            Expr::Or(a, b) => {
                let (x, y) = (self.eval(a, row), self.eval(b, row));
                if x == V::True || y == V::True {
                    V::True
                } else if x == V::False && y == V::False {
                    V::False
                } else {
                    V::Unknown
                }
            }
        }
    }

    /// Maximal contiguous true intervals over visible rows.
    fn true_intervals(&self, expr: &Expr) -> Vec<Interval> {
        let mut out = Vec::new();
        let mut r = 0u64;
        while r < self.n {
            if self.visible[r as usize] && self.eval(expr, r) == V::True {
                let start = r;
                while r < self.n && self.visible[r as usize] && self.eval(expr, r) == V::True {
                    r += 1;
                }
                out.push(iv(start, r));
            } else {
                r += 1;
            }
        }
        out
    }
}

/// Assert the query agrees with the model on intervals, count, and on every
/// page walk with each given page limit.
fn assert_matches_model(query: &Query, model: &Model, expr: &Expr, page_limits: &[usize]) {
    let expected = model.true_intervals(expr);
    assert_eq!(query.intervals(), expected.as_slice(), "interval mismatch");
    let expected_count: u64 = expected.iter().map(|i| i.len()).sum();
    assert_eq!(query.count(), expected_count, "count mismatch");

    for &limit in page_limits {
        let mut collected = Vec::new();
        let mut cursor = None;
        loop {
            let page = query.page(cursor, limit);
            collected.extend_from_slice(&page.intervals);
            match page.next_cursor {
                Some(c) => cursor = Some(c),
                None => break,
            }
        }
        assert_eq!(collected, expected, "page walk with limit {limit} mismatch");
    }
}

// ---------------------------------------------------------------------------
// Targeted scenarios
// ---------------------------------------------------------------------------

#[test]
fn visibility_holes_limit_not_and_output() {
    // Visible rows have holes: [0,10) and [20,30) only.
    let mut store = Store::new();
    store.correct_visibility(iv(0, 10), true);
    store.correct_visibility(iv(20, 30), true);
    store.correct_column(1, Kind::True, iv(0, 5), true);
    store.correct_column(1, Kind::True, iv(22, 28), true);
    store.correct_column(1, Kind::Unknown, iv(5, 7), true);

    // NOT complements only within visible rows and preserves unknown:
    // visible \ (true | unknown) = [7,10) ∪ [20,22) ∪ [28,30).
    let q = Query::open(&store, &Expr::not(Expr::column(1)));
    assert_eq!(q.intervals(), &[iv(7, 10), iv(20, 22), iv(28, 30)]);
    assert_eq!(q.count(), 7);

    // Unknown is preserved by NOT, so NOT(NOT A) == A exactly.
    let a = Query::open(&store, &Expr::column(1));
    let not_not_a = Query::open(&store, &Expr::not(Expr::not(Expr::column(1))));
    assert_eq!(a.intervals(), not_not_a.intervals());
    assert_eq!(a.intervals(), &[iv(0, 5), iv(22, 28)]);

    // The unknown band [5,7) must not appear in NOT A AND <always-true>.
    store.correct_column(2, Kind::True, iv(0, 30), true);
    let q = Query::open(
        &store,
        &Expr::and(Expr::not(Expr::column(1)), Expr::column(2)),
    );
    assert_eq!(q.intervals(), &[iv(7, 10), iv(20, 22), iv(28, 30)]);
}

#[test]
fn absent_bits_mean_false() {
    let mut store = Store::new();
    store.correct_visibility(iv(0, 100), true);
    store.correct_column(1, Kind::True, iv(10, 20), true);

    // Column 99 never arrived: AND with it is empty, OR with it is column 1.
    let q = Query::open(&store, &Expr::and(Expr::column(1), Expr::column(99)));
    assert!(q.intervals().is_empty());
    assert_eq!(q.count(), 0);

    let q = Query::open(&store, &Expr::or(Expr::column(1), Expr::column(99)));
    assert_eq!(q.intervals(), &[iv(10, 20)]);
}

#[test]
fn three_valued_and_or_overlapping_unknown() {
    let mut store = Store::new();
    store.correct_visibility(iv(0, 30), true);
    store.correct_column(1, Kind::True, iv(0, 10), true);
    store.correct_column(1, Kind::Unknown, iv(10, 20), true);
    store.correct_column(2, Kind::True, iv(15, 25), true);

    // AND: [0,10)∩[15,25) is empty; unknown∧true over [15,20) stays unknown.
    let q = Query::open(&store, &Expr::and(Expr::column(1), Expr::column(2)));
    assert!(q.intervals().is_empty());

    // OR: true bands union; [10,15) stays unknown and is not emitted.
    let q = Query::open(&store, &Expr::or(Expr::column(1), Expr::column(2)));
    assert_eq!(q.intervals(), &[iv(0, 10), iv(15, 25)]);
}

#[test]
fn unknown_split_across_batches_with_misaligned_boundaries() {
    // Column 1's true bits arrive in two batches; column 2's unknown band is
    // split into three batches whose boundaries align with neither column 1
    // nor the visibility segments.
    let mut store = Store::new();
    store.correct_visibility(iv(0, 7), true);
    store.correct_visibility(iv(7, 16), true); // adjacent piece, merges
    store.correct_column(1, Kind::True, iv(0, 3), true);
    store.correct_column(1, Kind::True, iv(3, 9), true);
    store.correct_column(2, Kind::True, iv(0, 16), true);
    store.correct_column(2, Kind::Unknown, iv(2, 5), true);
    store.correct_column(2, Kind::Unknown, iv(5, 11), true);
    store.correct_column(2, Kind::Unknown, iv(11, 14), true);

    let mut model = Model::new(16);
    model.correct_visibility(iv(0, 7), true);
    model.correct_visibility(iv(7, 16), true);
    model.correct_column(1, Kind::True, iv(0, 3), true);
    model.correct_column(1, Kind::True, iv(3, 9), true);
    model.correct_column(2, Kind::True, iv(0, 16), true);
    model.correct_column(2, Kind::Unknown, iv(2, 5), true);
    model.correct_column(2, Kind::Unknown, iv(5, 11), true);
    model.correct_column(2, Kind::Unknown, iv(11, 14), true);

    let expr = Expr::and(Expr::column(1), Expr::column(2));
    let q = Query::open(&store, &expr);
    // Sanity: unknown∧true over [2,9) is unknown, so only [0,2) and [9,16)
    // minus column-1 gaps survive... verified exactly against the model.
    assert_matches_model(&q, &model, &expr, &[1, 2, 3, 5]);
    assert_eq!(q.intervals(), &[iv(0, 2)]);

    let expr = Expr::or(Expr::column(1), Expr::not(Expr::column(2)));
    let q = Query::open(&store, &expr);
    assert_matches_model(&q, &model, &expr, &[1, 4]);
}

#[test]
fn late_correction_on_page_boundary_does_not_change_open_query() {
    // XOR layout yields two segments [0,10) and [12,20); the gap keeps them
    // from coalescing so the first page ends exactly at row 10.
    let mut store = Store::new();
    store.correct_visibility(iv(0, 20), true);
    store.correct_column(1, Kind::True, iv(0, 10), true);
    store.correct_column(2, Kind::True, iv(12, 20), true);
    let expr = Expr::or(
        Expr::and(Expr::column(1), Expr::not(Expr::column(2))),
        Expr::and(Expr::column(2), Expr::not(Expr::column(1))),
    );
    let query = Query::open(&store, &expr);
    assert_eq!(query.intervals(), &[iv(0, 10), iv(12, 20)]);
    assert_eq!(query.count(), 18);

    // First page ends exactly at the boundary row 10.
    let page1 = query.page(None, 1);
    assert_eq!(page1.intervals, &[iv(0, 10)]);
    assert_eq!(page1.next_cursor, Some(10));

    // Late correction lands exactly on the page boundary: remove [10,20),
    // which covers the whole second segment.
    store.correct_column(2, Kind::True, iv(10, 20), false);

    // The already-open query is frozen: page 2 still returns [12,20).
    let page2 = query.page(page1.next_cursor, 1);
    assert_eq!(page2.intervals, &[iv(12, 20)]);
    assert_eq!(page2.next_cursor, None);
    assert_eq!(query.count(), 18);

    // A newly opened query sees the correction.
    let fresh = Query::open(&store, &expr);
    assert_eq!(fresh.intervals(), &[iv(0, 10)]);
    assert_eq!(fresh.count(), 10);
    let page = fresh.page(Some(10), 4);
    assert!(page.intervals.is_empty());
    assert_eq!(page.next_cursor, None);

    // Symmetric case: a late *insert* at the boundary must not add rows to
    // an already-open query either.
    let query = Query::open(&store, &expr);
    assert_eq!(query.intervals(), &[iv(0, 10)]);
    store.correct_column(2, Kind::True, iv(10, 20), true);
    assert_eq!(query.intervals(), &[iv(0, 10)]);
    assert_eq!(query.page(Some(10), 1).intervals, &[]);
    let fresh = Query::open(&store, &expr);
    assert_eq!(fresh.intervals(), &[iv(0, 20)]);
}

#[test]
fn visibility_correction_hole_punched_after_open() {
    let mut store = Store::new();
    store.correct_visibility(iv(0, 100), true);
    store.correct_column(1, Kind::True, iv(0, 100), true);
    let query = Query::open(&store, &Expr::column(1));
    assert_eq!(query.count(), 100);

    // Punch a visibility hole; the open query must not change.
    store.correct_visibility(iv(40, 60), false);
    assert_eq!(query.count(), 100);
    assert_eq!(query.intervals(), &[iv(0, 100)]);

    let fresh = Query::open(&store, &Expr::column(1));
    assert_eq!(fresh.intervals(), &[iv(0, 40), iv(60, 100)]);
    assert_eq!(fresh.count(), 80);
}

#[test]
fn billion_row_span_stays_compressed() {
    const B: u64 = 1_000_000_000;
    let mut store = Store::new();
    store.correct_visibility(iv(0, B), true);
    store.correct_visibility(iv(300_000_000, 700_000_000), false); // hole
    store.correct_column(1, Kind::True, iv(0, 900_000_000), true);
    store.correct_column(1, Kind::Unknown, iv(100_000_000, 200_000_000), true);
    store.correct_column(2, Kind::True, iv(150_000_000, 900_000_000), true);

    let expr = Expr::or(Expr::column(1), Expr::column(2));
    let q = Query::open(&store, &expr);
    // col1 effective true = [0,900M) \ unknown[100M,200M); OR col2 [150M,900M)
    // gives [0,100M) ∪ [150M,900M); clipped to visible ([0,300M) ∪ [700M,B)):
    //      = [0,100M) ∪ [150M,300M) ∪ [700M,900M)
    assert_eq!(
        q.intervals(),
        &[iv(0, 100_000_000), iv(150_000_000, 300_000_000), iv(700_000_000, 900_000_000)]
    );
    assert_eq!(q.count(), 100_000_000 + 150_000_000 + 200_000_000);
    assert!(q.intervals().len() <= 4, "result must stay compressed");

    // NOT over the huge domain: complement within visible, unknown kept.
    let q = Query::open(&store, &Expr::not(Expr::column(1)));
    // visible \ (true ∪ unknown) = visible \ [0,900M) = [900M, B)
    assert_eq!(q.intervals(), &[iv(900_000_000, B)]);
    assert_eq!(q.count(), 100_000_000);

    // Page walk over the billion-row result with tiny pages.
    let q = Query::open(&store, &expr);
    let p1 = q.page(None, 2);
    assert_eq!(p1.intervals.len(), 2);
    assert_eq!(p1.next_cursor, Some(300_000_000));
    let p2 = q.page(p1.next_cursor, 2);
    assert_eq!(p2.intervals, &[iv(700_000_000, 900_000_000)]);
    assert_eq!(p2.next_cursor, None);

    // Cursor landing mid-interval resumes with the tail, no overlap/loss.
    let p = q.page(Some(50_000_000), 1);
    assert_eq!(p.intervals, &[iv(50_000_000, 100_000_000)]);
    assert_eq!(p.next_cursor, Some(100_000_000));
}

#[test]
fn page_cursor_inside_gap_and_at_end() {
    let mut store = Store::new();
    store.correct_visibility(iv(0, 30), true);
    store.correct_column(1, Kind::True, iv(0, 5), true);
    store.correct_column(1, Kind::True, iv(20, 25), true);
    let q = Query::open(&store, &Expr::column(1));

    // Cursor inside the gap [5,20) skips to the next interval.
    let p = q.page(Some(12), 4);
    assert_eq!(p.intervals, &[iv(20, 25)]);
    assert_eq!(p.next_cursor, None);

    // Cursor past the end yields an empty, exhausted page.
    let p = q.page(Some(25), 4);
    assert!(p.intervals.is_empty());
    assert_eq!(p.next_cursor, None);
}

// ---------------------------------------------------------------------------
// Randomized differential test (deterministic LCG, no external crates)
// ---------------------------------------------------------------------------

struct Lcg(u64);

impl Lcg {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        self.0 >> 11
    }

    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

fn random_expr(rng: &mut Lcg, depth: u32, columns: &[u64]) -> Expr {
    if depth == 0 || rng.below(3) == 0 {
        return Expr::column(columns[rng.below(columns.len() as u64) as usize]);
    }
    match rng.below(3) {
        0 => Expr::and(random_expr(rng, depth - 1, columns), random_expr(rng, depth - 1, columns)),
        1 => Expr::or(random_expr(rng, depth - 1, columns), random_expr(rng, depth - 1, columns)),
        _ => Expr::not(random_expr(rng, depth - 1, columns)),
    }
}

#[test]
fn randomized_differential_against_row_model() {
    const N: u64 = 256;
    let columns = [1u64, 2, 3];
    let mut rng = Lcg(0x5eed_1234_5678_9abc);

    for case in 0..200u64 {
        let mut store = Store::new();
        let mut model = Model::new(N);

        // Random visibility with holes, applied in misaligned batches.
        let vis_ops = 2 + rng.below(4);
        for _ in 0..vis_ops {
            let a = rng.below(N);
            let b = a + 1 + rng.below(N - a);
            let insert = rng.below(4) != 0;
            store.correct_visibility(iv(a, b), insert);
            model.correct_visibility(iv(a, b), insert);
        }

        // Random column batches: split segments, removals, unknown bands.
        for &col in &columns {
            let ops = rng.below(6);
            for _ in 0..ops {
                let a = rng.below(N);
                let b = a + 1 + rng.below(N - a);
                let kind = if rng.below(3) == 0 { Kind::Unknown } else { Kind::True };
                let insert = rng.below(4) != 0;
                store.correct_column(col, kind, iv(a, b), insert);
                model.correct_column(col, kind, iv(a, b), insert);
            }
        }

        let expr = random_expr(&mut rng, 3, &columns);
        let query = Query::open(&store, &expr);
        assert_matches_model(&query, &model, &expr, &[1, 2, 3, 7, 64]);

        // Late corrections must not disturb the open query; a fresh query
        // must match a model updated with the same corrections.
        let before: Vec<Interval> = query.intervals().to_vec();
        let before_count = query.count();
        let a = rng.below(N);
        let b = a + 1 + rng.below(N - a);
        let col = columns[rng.below(3) as usize];
        let kind = if rng.below(2) == 0 { Kind::Unknown } else { Kind::True };
        let insert = rng.below(2) == 0;
        store.correct_column(col, kind, iv(a, b), insert);
        model.correct_column(col, kind, iv(a, b), insert);

        assert_eq!(query.intervals(), before.as_slice(), "case {case}: open query changed");
        assert_eq!(query.count(), before_count, "case {case}: open query count changed");

        let fresh = Query::open(&store, &expr);
        assert_matches_model(&fresh, &model, &expr, &[1, 5]);
    }
}
