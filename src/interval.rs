use std::fmt;

/// Global row number.
pub type Row = u64;

/// A half-open row interval `[start, end)`.
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Interval {
    pub start: Row,
    pub end: Row,
}

impl Interval {
    pub fn new(start: Row, end: Row) -> Self {
        debug_assert!(start < end, "intervals must be non-empty half-open ranges");
        Interval { start, end }
    }

    pub fn len(&self) -> u64 {
        self.end - self.start
    }
}

impl fmt::Debug for Interval {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[{}, {})", self.start, self.end)
    }
}

impl fmt::Display for Interval {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[{}, {})", self.start, self.end)
    }
}

/// A compressed bitmap: sorted, disjoint, coalesced half-open intervals.
///
/// Adjacent intervals (`a.end == b.start`) are always merged so each set is a
/// canonical list of maximal contiguous runs. All operations are O(n + m) in
/// the number of stored segments; rows are never expanded.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct IntervalSet {
    ivs: Vec<Interval>,
}

impl IntervalSet {
    pub fn empty() -> Self {
        IntervalSet { ivs: Vec::new() }
    }

    /// Build a set from arbitrary (possibly overlapping or unsorted)
    /// intervals, coalescing overlaps and adjacency.
    pub fn from_intervals<I: IntoIterator<Item = Interval>>(ivs: I) -> Self {
        let mut ivs: Vec<Interval> = ivs.into_iter().collect();
        ivs.sort();
        let mut out: Vec<Interval> = Vec::with_capacity(ivs.len());
        for iv in ivs {
            match out.last_mut() {
                Some(last) if iv.start <= last.end => {
                    last.end = last.end.max(iv.end);
                }
                _ => out.push(iv),
            }
        }
        IntervalSet { ivs: out }
    }

    pub fn intervals(&self) -> &[Interval] {
        &self.ivs
    }

    pub fn segment_count(&self) -> usize {
        self.ivs.len()
    }

    pub fn is_empty(&self) -> bool {
        self.ivs.is_empty()
    }

    /// Total number of rows covered, computed from segment lengths.
    pub fn row_count(&self) -> u64 {
        self.ivs.iter().map(Interval::len).sum()
    }

    pub fn contains(&self, row: Row) -> bool {
        self.ivs
            .binary_search_by(|iv| {
                if row < iv.start {
                    std::cmp::Ordering::Greater
                } else if row >= iv.end {
                    std::cmp::Ordering::Less
                } else {
                    std::cmp::Ordering::Equal
                }
            })
            .is_ok()
    }

    pub fn union(&self, other: &IntervalSet) -> IntervalSet {
        IntervalSet::from_intervals(self.ivs.iter().copied().chain(other.ivs.iter().copied()))
    }

    pub fn intersect(&self, other: &IntervalSet) -> IntervalSet {
        let mut out = Vec::new();
        let (mut i, mut j) = (0usize, 0usize);
        while i < self.ivs.len() && j < other.ivs.len() {
            let (a, b) = (self.ivs[i], other.ivs[j]);
            let start = a.start.max(b.start);
            let end = a.end.min(b.end);
            if start < end {
                out.push(Interval { start, end });
            }
            if a.end < b.end {
                i += 1;
            } else {
                j += 1;
            }
        }
        IntervalSet { ivs: out }
    }

    /// `self` minus `other`.
    pub fn subtract(&self, other: &IntervalSet) -> IntervalSet {
        let mut out = Vec::new();
        let mut j = 0usize;
        for &a in &self.ivs {
            let mut cur = a.start;
            while j < other.ivs.len() && other.ivs[j].end <= cur {
                j += 1;
            }
            let mut k = j;
            while k < other.ivs.len() && other.ivs[k].start < a.end {
                let b = other.ivs[k];
                if b.start > cur {
                    out.push(Interval::new(cur, b.start.min(a.end)));
                }
                cur = cur.max(b.end);
                if cur >= a.end {
                    break;
                }
                k += 1;
            }
            if cur < a.end {
                out.push(Interval::new(cur, a.end));
            }
        }
        IntervalSet { ivs: out }
    }

    /// Complement of `self` restricted to `domain`.
    pub fn complement_within(&self, domain: &IntervalSet) -> IntervalSet {
        domain.subtract(self)
    }
}
