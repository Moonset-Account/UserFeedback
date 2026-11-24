//! 半开键区间工具。键按字节序（`Vec<u8>` 的字典序）排列。

use crate::error::EngineError;

/// 区间边界：`None` 表示该侧无界（起点侧为 -∞，终点侧为 +∞）。
pub type Bound = Option<Vec<u8>>;

/// 半开区间 `[start, end)`，任一端为 `None` 表示开放到无穷。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeyRange {
    pub start: Bound,
    pub end: Bound,
}

impl KeyRange {
    /// 构造 `[start, end)` 并校验非空。
    pub fn new(start: Bound, end: Bound) -> Result<Self, EngineError> {
        if let (Some(a), Some(b)) = (&start, &end) {
            if a >= b {
                return Err(EngineError::InvalidRange { start, end });
            }
        }
        Ok(Self { start, end })
    }

    /// 全键空间 `(-∞, +∞)`。
    pub fn universe() -> Self {
        Self { start: None, end: None }
    }

    /// 单点的最小包围区间 `[key, next_boundary)`，其中 next_boundary 仅用于切分，
    /// 这里直接返回 `[key, +∞)`，由 [`KeyRange::intersect`] 再夹取。
    pub fn point_prefix(key: Vec<u8>) -> Self {
        Self { start: Some(key), end: None }
    }

    /// 判断键是否落在区间内。
    pub fn contains(&self, key: &[u8]) -> bool {
        if let Some(a) = &self.start {
            if key < a.as_slice() {
                return false;
            }
        }
        if let Some(b) = &self.end {
            if key >= b.as_slice() {
                return false;
            }
        }
        true
    }

    /// 两个半开区间是否不相交（相邻但不重叠也算不相交）。
    pub fn is_disjoint(&self, other: &KeyRange) -> bool {
        // self.end <= other.start 或 other.end <= self.start
        if let (Some(se), Some(os)) = (&self.end, &other.start) {
            if se <= os {
                return true;
            }
        }
        if let (Some(oe), Some(ss)) = (&other.end, &self.start) {
            if oe <= ss {
                return true;
            }
        }
        false
    }

    /// 区间交集；不相交时返回 `None`。
    pub fn intersect(&self, other: &KeyRange) -> Option<KeyRange> {
        if self.is_disjoint(other) {
            return None;
        }
        let start = match (&self.start, &other.start) {
            (None, x) | (x, None) => x.clone(),
            (Some(a), Some(b)) => Some(if a >= b { a.clone() } else { b.clone() }),
        };
        let end = match (&self.end, &other.end) {
            (None, x) | (x, None) => x.clone(),
            (Some(a), Some(b)) => Some(if a <= b { a.clone() } else { b.clone() }),
        };
        Some(KeyRange { start, end })
    }

    /// 若两区间相交或相邻，返回合并后的并集；否则返回 `None`。
    pub fn union_adjacent(&self, other: &KeyRange) -> Option<KeyRange> {
        let adjacent = match (&self.end, &other.start) {
            (Some(se), Some(os)) => se == os,
            _ => false,
        } || match (&other.end, &self.start) {
            (Some(oe), Some(ss)) => oe == ss,
            _ => false,
        };
        if self.is_disjoint(other) && !adjacent {
            return None;
        }
        let start = match (&self.start, &other.start) {
            (None, x) | (x, None) => x.clone(),
            (Some(a), Some(b)) => Some(if a <= b { a.clone() } else { b.clone() }),
        };
        let end = match (&self.end, &other.end) {
            (None, x) | (x, None) => x.clone(),
            (Some(a), Some(b)) => Some(if a >= b { a.clone() } else { b.clone() }),
        };
        Some(KeyRange { start, end })
    }
}

/// 将一组区间合并为互不相交、按起点排序的并集区间。
pub fn merge_ranges(ranges: &[KeyRange]) -> Vec<KeyRange> {
    let mut sorted: Vec<KeyRange> = ranges.to_vec();
    sorted.sort_by(|a, b| match (&a.start, &b.start) {
        (None, _) => std::cmp::Ordering::Less,
        (_, None) => std::cmp::Ordering::Greater,
        (Some(x), Some(y)) => x.cmp(y),
    });
    let mut out: Vec<KeyRange> = Vec::new();
    for r in sorted {
        match out.last_mut() {
            Some(last) => match last.union_adjacent(&r) {
                Some(merged) => *last = merged,
                None => out.push(r),
            },
            None => out.push(r),
        }
    }
    out
}

/// 用一组切点把 `range` 切成若干相邻半开区间。
///
/// 切点 `cuts` 只会在其落在 `range` 内部时生效；返回的片段首尾相接、
/// 并集恰为原区间。
pub fn split_at(range: &KeyRange, cuts: &[Vec<u8>]) -> Vec<KeyRange> {
    let mut inner: Vec<&Vec<u8>> = cuts
        .iter()
        .filter(|k| range.contains(k.as_slice()))
        .collect();
    inner.sort();
    inner.dedup();

    let mut pieces = Vec::new();
    let mut cursor = range.start.clone();
    for cut in inner {
        // 跳过与当前起点重合的切点（否则会产生空区间）。
        if let Some(c) = &cursor {
            if c == cut {
                continue;
            }
        }
        pieces.push(KeyRange {
            start: cursor.clone(),
            end: Some(cut.clone()),
        });
        cursor = Some(cut.clone());
    }
    pieces.push(KeyRange {
        start: cursor,
        end: range.end.clone(),
    });
    pieces
}

#[cfg(test)]
mod tests {
    use super::*;

    fn k(s: &str) -> Vec<u8> {
        s.as_bytes().to_vec()
    }

    #[test]
    fn contains_and_intersect() {
        let r = KeyRange::new(Some(k("a")), Some(k("m"))).unwrap();
        assert!(r.contains(b"a"));
        assert!(r.contains(b"g"));
        assert!(!r.contains(b"m"));
        assert!(!r.contains(b"`"));

        let r2 = KeyRange::new(Some(k("g")), None).unwrap();
        let hit = r.intersect(&r2).unwrap();
        assert_eq!(hit.start, Some(k("g")));
        assert_eq!(hit.end, Some(k("m")));
        assert!(r.intersect(&KeyRange::new(Some(k("m")), None).unwrap()).is_none());
    }

    #[test]
    fn merging_and_splitting() {
        let ranges = vec![
            KeyRange::new(Some(k("a")), Some(k("c"))).unwrap(),
            KeyRange::new(Some(k("b")), Some(k("f"))).unwrap(),
            KeyRange::new(Some(k("z")), None).unwrap(),
        ];
        let merged = merge_ranges(&ranges);
        assert_eq!(merged.len(), 2);
        assert_eq!(merged[0], KeyRange::new(Some(k("a")), Some(k("f"))).unwrap());

        let pieces = split_at(
            &KeyRange::new(None, Some(k("t"))).unwrap(),
            &[k("a"), k("m"), k("a"), k("z")],
        );
        assert_eq!(pieces.len(), 3);
        assert_eq!(pieces[0], KeyRange::new(None, Some(k("a"))).unwrap());
        assert_eq!(pieces[1], KeyRange::new(Some(k("a")), Some(k("m"))).unwrap());
        assert_eq!(pieces[2], KeyRange::new(Some(k("m")), Some(k("t"))).unwrap());
    }

    #[test]
    fn invalid_ranges_rejected() {
        assert!(KeyRange::new(Some(k("z")), Some(k("a"))).is_err());
        assert!(KeyRange::new(Some(k("a")), Some(k("a"))).is_err());
        assert!(KeyRange::new(None, None).is_ok());
    }
}
