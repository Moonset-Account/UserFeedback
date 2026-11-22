//! 行块切分与块结构。

use crate::row::Row;

/// 一个输入块，持有不超过给定容量的行数，并带算子分配的块 ID。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Block<K: Ord, V, Id> {
    pub id: Id,
    pub rows: Vec<Row<K, V>>,
}

impl<K: Ord, V, Id> Block<K, V, Id> {
    /// 块内当前持有的行数。
    pub fn len(&self) -> usize {
        self.rows.len()
    }

    /// 块是否为空（正常切块流程不会产生空块）。
    pub fn is_empty(&self) -> bool {
        self.rows.is_empty()
    }
}

/// 把一批行按输入顺序切成每个块至多 `cap` 行的若干小块。
///
/// - `cap` 必须为正数，否则 panic；
/// - 空输入返回空向量；
/// - 最后一个块可能不足 `cap` 行，其余块恰好 `cap` 行。
///
/// 返回的是不带 ID 的原始切块，块 ID 由 [`crate::operator::FullOuterJoin`] 统一分配。
pub fn partition<K: Ord + Clone, V: Clone>(
    rows: &[Row<K, V>],
    cap: usize,
) -> Vec<Vec<Row<K, V>>> {
    assert!(cap > 0, "block capacity must be positive");
    rows.chunks(cap).map(|chunk| chunk.to_vec()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(id: u64) -> Row<i64, i64> {
        Row {
            id,
            key: Some(1),
            value: id as i64,
        }
    }

    #[test]
    fn partition_empty_input_yields_no_blocks() {
        let blocks: Vec<Vec<Row<i64, i64>>> = partition(&[], 4);
        assert!(blocks.is_empty());
    }

    #[test]
    fn partition_exact_multiple_fills_every_block() {
        let rows: Vec<_> = (0..6).map(row).collect();
        let blocks = partition(&rows, 2);
        assert_eq!(blocks.len(), 3);
        assert!(blocks.iter().all(|b| b.len() == 2));
    }

    #[test]
    fn partition_remainder_goes_into_last_small_block() {
        let rows: Vec<_> = (0..7).map(row).collect();
        let blocks = partition(&rows, 3);
        assert_eq!(blocks.len(), 3);
        assert_eq!(blocks[0].len(), 3);
        assert_eq!(blocks[1].len(), 3);
        assert_eq!(blocks[2].len(), 1);
        assert_eq!(blocks[2][0].id, 6);
    }

    #[test]
    fn partition_cap_one_means_one_row_per_block() {
        let rows: Vec<_> = (0..4).map(row).collect();
        let blocks = partition(&rows, 1);
        assert_eq!(blocks.len(), 4);
        assert!(blocks.iter().all(|b| b.len() == 1));
    }

    #[test]
    #[should_panic(expected = "block capacity must be positive")]
    fn partition_zero_capacity_is_rejected() {
        let rows = vec![row(1)];
        let _ = partition(&rows, 0);
    }
}
