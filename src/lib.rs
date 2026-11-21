//! 三列行值 `NOT IN` 算子（三值逻辑）。
//!
//! # 背景
//!
//! 执行器用固定为三列的整数行反复探测一张固定右表，列值允许为 [`Option::None`]（SQL
//! 中的 `NULL`）。行比较遵循 SQL 三值逻辑：
//!
//! - 行相等 `l = r`：任一对**非空**值不等则为 [`TriBool::False`]；否则只要任一列含空
//!   就是 [`TriBool::Unknown`]；三列全部非空且相等才为 [`TriBool::True`]。
//! - `l NOT IN (r1, r2, ...)`：
//!   - 存在 `ri` 使 `l = ri` 为真 → [`TriBool::False`]；
//!   - 否则存在 `ri` 使 `l = ri` 为未知 → [`TriBool::Unknown`]；
//!   - 否则（含右表为空）→ [`TriBool::True`]。
//!
//! 用作 `WHERE` 过滤时只保留结果为 [`TriBool::True`] 的行；`Unknown` 与 `False` 都被过滤。
//!
//! # 索引与复杂度
//!
//! 右表每行按其非空列掩码 `m`（3 位）展开为 `m` 的全部子集 `sub ⊆ m`，每条索引键为
//! `(sub, sub 各列上的值)`，载荷是一个 8 位掩码，记录哪些右行空值模式 `m` 能产生该键。
//!
//! 探测左行（非空列掩码 `P`）时枚举 `P` 的全部子集（共 `2^P` 个），每个子集做**一次**
//! 哈希查找：
//!
//! - 键 `sub = 0b111` 命中 ⇒ 存在三列全非空且全等的右行 ⇒ 真匹配，`NOT IN` 为假；
//! - 否则命中键的载荷中若存在右行模式 `m` 满足 `m & (P \ sub) = 0`，说明该右行在双方
//!   都非空的列上全部相等，其余列至少一侧为空 ⇒ 未知匹配；
//! - 载荷中其他右行模式在 `P \ sub` 上有非空列，其值必与左行不等（否则会被更大的子掩码
//!   键命中），对应假匹配，不影响结论。
//!
//! 因此单行探测的哈希查找次数恰为 `2^p`（`p` 为左行非空列数，取值 1/2/4/8），只取决于
//! 三列的空值组合，与右表行数无关；每次探测通过 [`ProbeReport`] 如实回报查找次数，
//! 调用方可据此计数。

use std::collections::HashMap;
use std::hash::{Hash, Hasher};

/// 三值逻辑的结果：真 / 假 / 未知。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum TriBool {
    /// SQL `TRUE`
    True,
    /// SQL `FALSE`
    False,
    /// SQL `UNKNOWN`（由 `NULL` 参与比较产生）
    Unknown,
}

impl TriBool {
    /// SQL `WHERE` 只保留判定为真的行。
    #[must_use]
    pub fn retained_in_where(self) -> bool {
        matches!(self, TriBool::True)
    }
}

/// 三列行值，每列均可为 `NULL`（[`Option::None`]）。
pub type Row = (Option<i64>, Option<i64>, Option<i64>);

/// 索引键：列掩码 `sub` + 这些列上的值。
///
/// `col_mask` 的第 `i` 位为 1 表示该键携带第 `i` 列的值；`vals` 按列序存放被携带列的
/// 值，未被携带的位置固定为 0 且不参与相等性判定与散列。
#[derive(Debug, Clone, Copy, Eq)]
struct Entry {
    col_mask: u8,
    vals: [i64; 3],
}

// 手工实现 PartialEq/Eq/Hash，三者保持一致，只考虑实际携带的列。
impl PartialEq for Entry {
    fn eq(&self, other: &Self) -> bool {
        if self.col_mask != other.col_mask {
            return false;
        }
        for i in 0..3 {
            if self.col_mask & (1 << i) != 0 && self.vals[i] != other.vals[i] {
                return false;
            }
        }
        true
    }
}

impl Hash for Entry {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.col_mask.hash(state);
        for i in 0..3 {
            if self.col_mask & (1 << i) != 0 {
                self.vals[i].hash(state);
            }
        }
    }
}

/// 右表上的预建索引，可被任意多个左行重复探测。
///
/// 载荷 `u8` 的第 `m` 位为 1，表示右表中存在空值模式为 `m` 的行产生了该键
/// （即该行非空列包含 `sub` 且这些列上的值与键相等）。
#[derive(Debug, Clone)]
pub struct NotInIndex {
    entries: HashMap<Entry, u8>,
    right_rows: Vec<Row>,
}

/// 单次探测的明细，供调用方统计索引查找次数。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProbeReport {
    /// 本次探测实际执行的哈希查找次数。
    ///
    /// 仅取决于左行三列的空值组合：非空列数为 `p` 时恰为 `2^p`
    /// （`p = 0/1/2/3` 对应 `1/2/4/8`），与右表行数无关。
    pub index_lookups: usize,
    /// 本次探测命中“真匹配”（三列全等）时为 `true`。
    pub found_true_match: bool,
    /// 本次探测是否出现过“未知匹配”（非空列相等、其余列至少一侧含空）。
    pub saw_unknown: bool,
}

impl NotInIndex {
    /// 对固定右表预建索引。右表所有权被移入索引。
    #[must_use]
    pub fn build(right_table: Vec<Row>) -> Self {
        let mut entries: HashMap<Entry, u8> = HashMap::new();
        for row in &right_table {
            let cols = [row.0, row.1, row.2];
            let mut present: u8 = 0;
            let mut vals = [0_i64; 3];
            for (i, col) in cols.iter().enumerate() {
                if let Some(v) = col {
                    present |= 1 << i;
                    vals[i] = *v;
                }
            }
            // 枚举 present 的所有子掩码：该右行在其每组非空列上都可被定位，
            // 并把自身完整空值模式记入载荷位掩码。
            let mut sub = present;
            loop {
                *entries
                    .entry(Entry {
                        col_mask: sub,
                        vals,
                    })
                    .or_insert(0) |= 1 << present;
                if sub == 0 {
                    break;
                }
                sub = (sub - 1) & present;
            }
        }
        Self {
            entries,
            right_rows: right_table,
        }
    }

    /// 右表行数。
    #[must_use]
    pub fn len(&self) -> usize {
        self.right_rows.len()
    }

    /// 右表是否为空。空右表上任何探测结果都为 [`TriBool::True`]。
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.right_rows.is_empty()
    }

    /// 探测一个左行，返回 `NOT IN` 的三值结果与本次查找计数报告。
    ///
    /// 查找次数只取决于 `left` 的空值组合，与右表行数无关。
    pub fn probe(&self, left: Row) -> (TriBool, ProbeReport) {
        let cols = [left.0, left.1, left.2];
        let mut present: u8 = 0;
        let mut vals = [0_i64; 3];
        for (i, col) in cols.iter().enumerate() {
            if let Some(v) = col {
                present |= 1 << i;
                vals[i] = *v;
            }
        }

        let mut index_lookups = 0usize;
        let mut saw_unknown = false;
        let mut found_true_match = false;

        // 枚举左行非空列集合的所有子集；每个子集恰好一次哈希查找。
        let mut sub = present;
        loop {
            index_lookups += 1;
            if let Some(&right_masks) = self.entries.get(&Entry {
                col_mask: sub,
                vals,
            }) {
                if sub == 0b111 {
                    // 能产生 0b111 键的右行必然三列全非空且与左行全等 ⇒ 行相等为真。
                    found_true_match = true;
                } else {
                    // 未知匹配条件：存在右行模式 m，使 m 在左行非空、但子掩码未覆盖的
                    // 列上没有非空列（即这些列右行为空）。载荷中的模式天然满足 m ⊇ sub。
                    let uncovered = present & !sub;
                    let mut forbidden = 0u8;
                    for m in 0..8u8 {
                        if m & uncovered != 0 {
                            forbidden |= 1 << m;
                        }
                    }
                    if right_masks & !forbidden != 0 {
                        saw_unknown = true;
                    }
                }
            }
            if sub == 0 {
                break;
            }
            sub = (sub - 1) & present;
        }

        let result = if found_true_match {
            TriBool::False
        } else if saw_unknown {
            TriBool::Unknown
        } else {
            TriBool::True
        };

        (
            result,
            ProbeReport {
                index_lookups,
                found_true_match,
                saw_unknown,
            },
        )
    }

    /// 批量探测，返回与输入等长、顺序一致的三值结果。
    #[must_use]
    pub fn probe_all(&self, left_rows: &[Row]) -> Vec<TriBool> {
        left_rows.iter().map(|row| self.probe(*row).0).collect()
    }

    /// 模拟 `WHERE left NOT IN right_table`：返回被保留行（结果为 [`TriBool::True`]）
    /// 在输入切片中的原始下标（从 0 开始）。
    #[must_use]
    pub fn retain_where(&self, left_rows: &[Row]) -> Vec<usize> {
        left_rows
            .iter()
            .enumerate()
            .filter_map(|(idx, row)| {
                if self.probe(*row).0 == TriBool::True {
                    Some(idx)
                } else {
                    None
                }
            })
            .collect()
    }
}

/// 朴素行相等（三值逻辑），用于语义参照与测试交叉验证。
///
/// - 任一对非空值不等 → [`TriBool::False`]；
/// - 否则任一列含空 → [`TriBool::Unknown`]；
/// - 三列全部非空且相等 → [`TriBool::True`]。
#[must_use]
pub fn row_equals(left: Row, right: Row) -> TriBool {
    let l = [left.0, left.1, left.2];
    let r = [right.0, right.1, right.2];
    let mut has_null = false;
    for i in 0..3 {
        match (l[i], r[i]) {
            (Some(a), Some(b)) if a != b => return TriBool::False,
            (None, _) | (_, None) => has_null = true,
            _ => {}
        }
    }
    if has_null {
        TriBool::Unknown
    } else {
        TriBool::True
    }
}

/// 朴素 `NOT IN` 语义：逐条扫描右表，与索引实现结果应完全一致。
#[must_use]
pub fn naive_not_in(left: Row, right_table: &[Row]) -> TriBool {
    let mut saw_unknown = false;
    for right in right_table {
        match row_equals(left, *right) {
            TriBool::True => return TriBool::False,
            TriBool::Unknown => saw_unknown = true,
            TriBool::False => {}
        }
    }
    if saw_unknown {
        TriBool::Unknown
    } else {
        TriBool::True
    }
}

#[cfg(test)]
mod unit_tests {
    use super::*;

    #[test]
    fn row_equals_truth_table() {
        assert_eq!(
            row_equals((Some(1), Some(2), Some(3)), (Some(1), Some(2), Some(3))),
            TriBool::True
        );
        assert_eq!(
            row_equals((Some(1), Some(2), Some(3)), (Some(1), Some(9), Some(3))),
            TriBool::False
        );
        // 非空值不等优先于含空，仍为假。
        assert_eq!(
            row_equals((Some(1), None, Some(3)), (Some(9), Some(2), None)),
            TriBool::False
        );
        // 无矛盾但含空 → 未知。
        assert_eq!(
            row_equals((Some(1), None, Some(3)), (Some(1), Some(2), None)),
            TriBool::Unknown
        );
        assert_eq!(
            row_equals((None, None, None), (None, None, None)),
            TriBool::Unknown
        );
    }

    #[test]
    fn empty_right_table_is_always_true() {
        let idx = NotInIndex::build(vec![]);
        assert!(idx.is_empty());
        for row in [
            (None, None, None),
            (Some(1), None, None),
            (Some(1), Some(2), Some(3)),
        ] {
            assert_eq!(idx.probe(row).0, TriBool::True);
        }
    }
}
