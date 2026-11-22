//! 输入行与全外连接输出行的定义。

/// 输入侧的一行。
///
/// - `id`：两侧各自唯一的行 ID；
/// - `key`：等值连接键，[`None`] 表示空键，空键不参与匹配，直接走未匹配路径；
/// - `value`：行载荷，附加条件可以基于它做比较。
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Row<K: Ord, V> {
    pub id: u64,
    pub key: Option<K>,
    pub value: V,
}

/// 全外连接的一行输出。
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum JoinOut<K: Ord, V> {
    /// 两侧命中：等值键相同且附加条件成立。
    Matched {
        key: K,
        left_id: u64,
        left_value: V,
        right_id: u64,
        right_value: V,
    },
    /// 左侧未命中，右侧补空（保留可能为空的键）。
    LeftOnly {
        key: Option<K>,
        left_id: u64,
        left_value: V,
    },
    /// 右侧未命中，左侧补空（保留可能为空的键）。
    RightOnly {
        key: Option<K>,
        right_id: u64,
        right_value: V,
    },
}

impl<K: Ord, V> JoinOut<K, V> {
    /// 输出中的左侧行 ID（若该输出包含左侧行）。
    pub fn left_id(&self) -> Option<u64> {
        match self {
            JoinOut::Matched { left_id, .. } | JoinOut::LeftOnly { left_id, .. } => {
                Some(*left_id)
            }
            JoinOut::RightOnly { .. } => None,
        }
    }

    /// 输出中的右侧行 ID（若该输出包含右侧行）。
    pub fn right_id(&self) -> Option<u64> {
        match self {
            JoinOut::Matched { right_id, .. } | JoinOut::RightOnly { right_id, .. } => {
                Some(*right_id)
            }
            JoinOut::LeftOnly { .. } => None,
        }
    }
}
