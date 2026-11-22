//! 模拟执行节点：对一个块对任务执行真实的嵌套循环连接。
//!
//! 节点只看到任务携带的两个块（看不到算子的其他状态），对左右行做笛卡尔扫描，
//! 先校验等值键、再应用调用方给出的附加条件，产出匹配对与逐行命中记录。
//! 失败注入由测试驱动通过回调编排，节点本身只负责计算。

use std::collections::HashSet;

use crate::operator::{PairResult, Task};
use crate::row::Row;

/// 无状态的模拟执行节点。`residual` 是比较左右两行的附加条件。
pub struct SimulatedNode<K: Ord, V, F> {
    residual: F,
    _marker: std::marker::PhantomData<fn() -> (K, V)>,
}

impl<K: Ord, V, F> SimulatedNode<K, V, F>
where
    F: Fn(&Row<K, V>, &Row<K, V>) -> bool,
{
    /// 用给定附加条件创建节点。
    pub fn new(residual: F) -> Self {
        Self {
            residual,
            _marker: std::marker::PhantomData,
        }
    }

    /// 执行一次块对尝试，返回成功回报。
    ///
    /// 匹配判定：
    ///
    /// 1. 两侧键均非空且相等（块对本身已按键编排，这里做防御性校验）；
    /// 2. 附加条件 `residual(left, right)` 成立。
    ///
    /// 返回结果包含去重后的匹配对以及「逐行命中」记录（一行在块对内命中
    /// 任意一次即记入）。
    pub fn run(&self, task: &Task<K, V>) -> PairResult
    where
        K: PartialEq,
    {
        let mut matches: Vec<(u64, u64)> = Vec::new();
        let mut left_hit: HashSet<u64> = HashSet::new();
        let mut right_hit: HashSet<u64> = HashSet::new();

        for left in &task.left.rows {
            for right in &task.right.rows {
                let same_key = match (&left.key, &right.key) {
                    (Some(lk), Some(rk)) => lk == rk,
                    _ => false,
                };
                if same_key && (self.residual)(left, right) {
                    matches.push((left.id, right.id));
                    left_hit.insert(left.id);
                    right_hit.insert(right.id);
                }
            }
        }

        PairResult {
            pair: task.pair,
            attempt: task.attempt,
            matches,
            left_hit_rows: left_hit.into_iter().collect(),
            right_hit_rows: right_hit.into_iter().collect(),
        }
    }
}
