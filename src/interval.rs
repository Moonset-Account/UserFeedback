//! 流数据的字节区间与不相交区间集合。
//!
//! 一个 [`StreamRange`] 用 `(流号, 起始偏移, 结束偏移)` 标识一段流字节，
//! 区间为半开区间 `[start, end)`。同一区间可以被多个承载包携带，因此
//! 发送端需要以“字节区间”而不是“包”为单位跟踪重传。

use std::cmp::Ordering;

/// 某条流上的半开字节区间 `[start, end)`。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StreamRange {
    /// 流号。
    pub stream_id: u64,
    /// 起始字节偏移（含）。
    pub start: u64,
    /// 结束字节偏移（不含）。
    pub end: u64,
}

impl StreamRange {
    /// 构造一个区间并校验偏移合法性。
    pub const fn new(stream_id: u64, start: u64, end: u64) -> Result<Self, InvalidRange> {
        if start > end {
            return Err(InvalidRange { start, end });
        }
        Ok(Self {
            stream_id,
            start,
            end,
        })
    }

    /// 区间长度（字节数），空区间为 0。
    pub const fn len(&self) -> u64 {
        self.end - self.start
    }

    /// 是否为空区间。
    pub const fn is_empty(&self) -> bool {
        self.start == self.end
    }

    /// 两个区间是否属于同一条流且有重叠。
    pub fn overlaps(&self, other: &Self) -> bool {
        self.stream_id == other.stream_id && self.start < other.end && other.start < self.end
    }

    /// 两个区间是否属于同一条流且首尾相接（可合并为一个连续区间）。
    fn abuts(&self, other: &Self) -> bool {
        self.stream_id == other.stream_id
            && (self.end == other.start || other.end == self.start)
    }

    /// 返回两个同流区间的并集外壳，调用前需保证重叠或相接。
    fn hull(&self, other: &Self) -> Self {
        StreamRange {
            stream_id: self.stream_id,
            start: self.start.min(other.start),
            end: self.end.max(other.end),
        }
    }
}

/// 区间非法：起始偏移大于结束偏移。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InvalidRange {
    /// 起始偏移。
    pub start: u64,
    /// 结束偏移。
    pub end: u64,
}

/// 一组保持有序、同流不相交的流区间集合。
///
/// 同一条流上相邻（首尾相接）的区间会被合并；不同流的区间按流号排列，
/// 互不影响。集合内部始终是规范形式，因此相等比较与计费都是确定的。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct IntervalSet {
    ranges: Vec<StreamRange>,
}

impl IntervalSet {
    /// 建立空集合。
    pub fn new() -> Self {
        Self { ranges: Vec::new() }
    }

    /// 从一组区间建立集合（自动归并）。
    pub fn from_ranges(items: impl IntoIterator<Item = StreamRange>) -> Self {
        let mut set = Self::new();
        for item in items {
            set.insert(item);
        }
        set
    }

    /// 当前规范区间数。
    pub fn len(&self) -> usize {
        self.ranges.len()
    }

    /// 集合是否为空。
    pub fn is_empty(&self) -> bool {
        self.ranges.is_empty()
    }

    /// 以规范顺序查看内部区间。
    pub fn iter(&self) -> impl Iterator<Item = &StreamRange> {
        self.ranges.iter()
    }

    /// 所有区间覆盖的字节总数。
    pub fn total_bytes(&self) -> u64 {
        self.ranges.iter().map(|r| r.len()).fold(0u64, u64::saturating_add)
    }

    /// 插入一个区间，自动与重叠或相邻的同流区间合并；空区间直接忽略。
    pub fn insert(&mut self, range: StreamRange) {
        if range.is_empty() {
            return;
        }
        let pos = self
            .ranges
            .iter()
            .position(|existing| (existing.stream_id, existing.start) >= (range.stream_id, range.start))
            .unwrap_or(self.ranges.len());

        let mut merged = range;
        let mut end = pos;
        while end < self.ranges.len() {
            let existing = self.ranges[end];
            if existing.stream_id != range.stream_id || (!merged.overlaps(&existing) && !merged.abuts(&existing)) {
                break;
            }
            merged = merged.hull(&existing);
            end += 1;
        }
        let mut start = pos;
        while start > 0 {
            let existing = self.ranges[start - 1];
            if existing.stream_id != range.stream_id || (!merged.overlaps(&existing) && !merged.abuts(&existing)) {
                break;
            }
            merged = merged.hull(&existing);
            start -= 1;
        }
        if start < end {
            self.ranges.drain(start..end);
        }
        self.ranges.insert(start, merged);
    }

    /// 删除与 `range` 重叠的部分，必要时把一个区间切成两段；空区间直接忽略。
    pub fn remove(&mut self, range: StreamRange) {
        if range.is_empty() || self.ranges.is_empty() {
            return;
        }
        let mut replacements: Vec<StreamRange> = Vec::new();
        self.ranges.retain_mut(|existing| {
            if !existing.overlaps(&range) {
                return true;
            }
            if existing.start < range.start {
                replacements.push(StreamRange {
                    stream_id: existing.stream_id,
                    start: existing.start,
                    end: range.start,
                });
            }
            if range.end < existing.end {
                replacements.push(StreamRange {
                    stream_id: existing.stream_id,
                    start: range.end,
                    end: existing.end,
                });
            }
            false
        });
        for item in replacements {
            self.insert(item);
        }
    }

    /// 并入另一个集合的全部区间。
    pub fn union_with(&mut self, other: &IntervalSet) {
        for range in other.ranges.iter().copied() {
            self.insert(range);
        }
    }

    /// 判断某条流上的单个偏移是否已被覆盖。
    pub fn contains_offset(&self, stream_id: u64, offset: u64) -> bool {
        self.ranges
            .iter()
            .any(|r| r.stream_id == stream_id && r.start <= offset && offset < r.end)
    }

    /// 判断给定区间的每个字节是否都已被覆盖（空区间视为已覆盖）。
    pub fn covers(&self, range: &StreamRange) -> bool {
        if range.is_empty() {
            return true;
        }
        let mut cursor = range.start;
        for existing in &self.ranges {
            if existing.stream_id != range.stream_id {
                continue;
            }
            if existing.end <= cursor {
                continue;
            }
            if existing.start > cursor {
                return false;
            }
            cursor = existing.end;
            if cursor >= range.end {
                return true;
            }
        }
        false
    }
}

impl Ord for StreamRange {
    fn cmp(&self, other: &Self) -> Ordering {
        (self.stream_id, self.start, self.end).cmp(&(other.stream_id, other.start, other.end))
    }
}

impl PartialOrd for StreamRange {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn r(stream_id: u64, start: u64, end: u64) -> StreamRange {
        StreamRange::new(stream_id, start, end).unwrap()
    }

    #[test]
    fn merges_overlapping_and_adjacent_ranges() {
        let mut set = IntervalSet::new();
        set.insert(r(0, 10, 20));
        set.insert(r(0, 15, 25));
        set.insert(r(0, 25, 30));
        assert_eq!(set.iter().copied().collect::<Vec<_>>(), vec![r(0, 10, 30)]);
        assert_eq!(set.total_bytes(), 20);
    }

    #[test]
    fn keeps_streams_and_gaps_separate() {
        let mut set = IntervalSet::new();
        set.insert(r(1, 0, 5));
        set.insert(r(0, 0, 5));
        set.insert(r(0, 10, 15));
        assert_eq!(
            set.iter().copied().collect::<Vec<_>>(),
            vec![r(0, 0, 5), r(0, 10, 15), r(1, 0, 5)]
        );
        assert_eq!(set.total_bytes(), 15);
    }

    #[test]
    fn remove_splits_and_trims_ranges() {
        let mut set = IntervalSet::from_ranges([r(0, 0, 100)]);
        set.remove(r(0, 40, 60));
        assert_eq!(
            set.iter().copied().collect::<Vec<_>>(),
            vec![r(0, 0, 40), r(0, 60, 100)]
        );
        set.remove(r(0, 0, 10));
        assert_eq!(
            set.iter().copied().collect::<Vec<_>>(),
            vec![r(0, 10, 40), r(0, 60, 100)]
        );
        set.remove(r(0, 90, 200));
        assert_eq!(
            set.iter().copied().collect::<Vec<_>>(),
            vec![r(0, 10, 40), r(0, 60, 90)]
        );
    }

    #[test]
    fn remove_does_not_touch_other_streams() {
        let mut set = IntervalSet::from_ranges([r(0, 0, 10), r(1, 0, 10)]);
        set.remove(r(0, 0, 10));
        assert_eq!(set.iter().copied().collect::<Vec<_>>(), vec![r(1, 0, 10)]);
    }

    #[test]
    fn covers_and_contains_reflect_membership() {
        let set = IntervalSet::from_ranges([r(0, 10, 20), r(0, 30, 40)]);
        assert!(set.contains_offset(0, 10));
        assert!(!set.contains_offset(0, 20));
        assert!(set.covers(&r(0, 12, 18)));
        assert!(!set.covers(&r(0, 18, 32)));
        assert!(set.covers(&r(0, 30, 40)));
        assert!(set.covers(&r(1, 5, 5)));
        assert!(!set.covers(&r(1, 5, 6)));
    }

    #[test]
    fn invalid_range_is_rejected() {
        assert_eq!(
            StreamRange::new(0, 9, 8),
            Err(InvalidRange { start: 9, end: 8 })
        );
        assert!(StreamRange::new(0, 8, 8).unwrap().is_empty());
    }
}
