//! Sorted, coalesced half-open `[start, end)` interval sets in the flat
//! (unwrapped) 64-bit sequence-number space.
//!
//! All ranges are inclusive of `start` and exclusive of `end`. The set never
//! stores empty or overlapping ranges; touching ranges are merged so that byte
//! counts are exact even when segments are split arbitrarily.

/// An immutable set of disjoint, sorted, coalesced half-open byte ranges.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct IntervalSet {
    ranges: Vec<(u64, u64)>,
}

impl IntervalSet {
    /// Create an empty set.
    pub fn new() -> Self {
        Self { ranges: Vec::new() }
    }

    /// Create a set containing a single `[start, end)` range.
    ///
    /// # Panics
    /// Panics if `start > end`.
    pub fn from_range(start: u64, end: u64) -> Self {
        assert!(start <= end, "inverted interval [{start}, {end})");
        let mut ranges = Vec::new();
        if start < end {
            ranges.push((start, end));
        }
        Self { ranges }
    }

    /// Build a set from ranges (merging overlaps, mainly for tests).
    #[cfg(test)]
    pub fn from_ranges(items: &[(u64, u64)]) -> Self {
        let mut s = Self::new();
        for &(start, end) in items {
            s.insert(start, end);
        }
        s
    }

    /// Whether the set contains no bytes.
    pub fn is_empty(&self) -> bool {
        self.ranges.is_empty()
    }

    /// Iterate over the stored `[start, end)` ranges.
    pub fn iter(&self) -> impl Iterator<Item = (u64, u64)> + '_ {
        self.ranges.iter().copied()
    }

    /// Total number of bytes covered by the set.
    pub fn covered_bytes(&self) -> u64 {
        self.ranges.iter().map(|&(s, e)| e - s).sum()
    }

    /// Find the first covered byte at or above `at`, together with the end of
    /// the contiguous covered run containing it.
    fn run_at(&self, at: u64) -> Option<(u64, u64)> {
        for &(s, e) in &self.ranges {
            if e <= at {
                continue;
            }
            if at < s {
                return None;
            }
            return Some((at, e));
        }
        None
    }

    /// Insert every byte of `[start, end)`, merging with overlapping or
    /// adjacent existing ranges.
    pub fn insert(&mut self, start: u64, end: u64) {
        assert!(start <= end, "inverted interval [{start}, {end})");
        if start == end {
            return;
        }
        let mut out: Vec<(u64, u64)> = Vec::with_capacity(self.ranges.len() + 1);
        let mut i = 0usize;
        // Copy every range strictly before the new one.
        while i < self.ranges.len() && self.ranges[i].1 < start {
            out.push(self.ranges[i]);
            i += 1;
        }
        // Fold in every range overlapping or touching the new range.
        let (mut cur_s, mut cur_e) = (start, end);
        while i < self.ranges.len() && self.ranges[i].0 <= cur_e {
            cur_s = cur_s.min(self.ranges[i].0);
            cur_e = cur_e.max(self.ranges[i].1);
            i += 1;
        }
        out.push((cur_s, cur_e));
        // Everything left is strictly after the merged range.
        while i < self.ranges.len() {
            out.push(self.ranges[i]);
            i += 1;
        }
        self.ranges = out;
    }

    /// Remove every byte of `[start, end)`.
    pub fn remove(&mut self, start: u64, end: u64) {
        assert!(start <= end, "inverted interval [{start}, {end})");
        if start == end {
            return;
        }
        let mut out: Vec<(u64, u64)> = Vec::with_capacity(self.ranges.len() + 1);
        for &(s, e) in &self.ranges {
            if e <= start || s >= end {
                out.push((s, e));
            } else {
                if s < start {
                    out.push((s, start));
                }
                if e > end {
                    out.push((end, e));
                }
            }
        }
        self.ranges = out;
    }

    /// Drop every byte below `boundary` (cumulative-ack truncation).
    pub fn truncate_below(&mut self, boundary: u64) {
        self.ranges.retain_mut(|(s, e)| {
            if *e <= boundary {
                false
            } else if *s < boundary {
                *s = boundary;
                true
            } else {
                true
            }
        });
    }

    /// Set union.
    pub fn union(&self, other: &Self) -> Self {
        let mut out = self.clone();
        for &(s, e) in &other.ranges {
            out.insert(s, e);
        }
        out
    }

    /// Set intersection.
    pub fn intersection(&self, other: &Self) -> Self {
        let mut out = Self::new();
        let mut j = 0usize;
        for &(s1, e1) in &self.ranges {
            while j < other.ranges.len() && other.ranges[j].1 <= s1 {
                j += 1;
            }
            let mut k = j;
            while k < other.ranges.len() && other.ranges[k].0 < e1 {
                let (s2, e2) = other.ranges[k];
                let s = s1.max(s2);
                let e = e1.min(e2);
                if s < e {
                    out.insert(s, e);
                }
                k += 1;
            }
        }
        out
    }

    /// Bytes in `self` that are not in `other`.
    pub fn difference(&self, other: &Self) -> Self {
        let mut out = self.clone();
        for &(s, e) in &other.ranges {
            out.remove(s, e);
        }
        out
    }

    /// Whether every byte of `[start, end)` is covered.
    pub fn contains_all(&self, start: u64, end: u64) -> bool {
        if start == end {
            return true;
        }
        let mut cursor = start;
        while cursor < end {
            match self.run_at(cursor) {
                Some((_, e)) => cursor = e,
                None => return false,
            }
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn insert_coalesces_overlapping_and_adjacent() {
        let mut s = IntervalSet::new();
        s.insert(10, 20);
        s.insert(20, 25); // adjacent, must merge
        s.insert(22, 30); // overlapping, must merge
        s.insert(5, 8);
        assert_eq!(s.covered_bytes(), 23);
        assert_eq!(s.iter().collect::<Vec<_>>(), vec![(5, 8), (10, 30)]);
    }

    #[test]
    fn insert_preserves_gap_between_disjoint_ranges() {
        let mut s = IntervalSet::new();
        s.insert(10, 20);
        s.insert(22, 30); // byte 20..22 is a genuine gap
        assert_eq!(s.iter().collect::<Vec<_>>(), vec![(10, 20), (22, 30)]);
        assert_eq!(s.covered_bytes(), 18);
    }

    #[test]
    fn remove_splits_ranges() {
        let mut s = IntervalSet::from_range(0, 100);
        s.remove(40, 60);
        assert_eq!(s.iter().collect::<Vec<_>>(), vec![(0, 40), (60, 100)]);
        assert_eq!(s.covered_bytes(), 80);
        assert!(s.contains_all(0, 40));
        assert!(!s.contains_all(39, 41));
        assert!(s.contains_all(60, 100));
    }

    #[test]
    fn set_operations() {
        let a = IntervalSet::from_ranges(&[(0, 10), (20, 30)]);
        let b = IntervalSet::from_ranges(&[(5, 25)]);
        assert_eq!(a.union(&b).iter().collect::<Vec<_>>(), vec![(0, 30)]);
        assert_eq!(
            a.intersection(&b).iter().collect::<Vec<_>>(),
            vec![(5, 10), (20, 25)]
        );
        assert_eq!(
            a.difference(&b).iter().collect::<Vec<_>>(),
            vec![(0, 5), (25, 30)]
        );
    }

    #[test]
    fn truncate_below_drops_acked_bytes() {
        let mut s = IntervalSet::from_ranges(&[(0, 10), (15, 40)]);
        s.truncate_below(20);
        assert_eq!(s.iter().collect::<Vec<_>>(), vec![(20, 40)]);
    }
}
