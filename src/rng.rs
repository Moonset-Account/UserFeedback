//! 确定性随机原语与流坐标。
//!
//! 所有随机字都由 [`mix64`]（SplitMix64 风格的双射混合）组合得出。混合函数是
//! `u64 -> u64` 的双射，配合各计数器字段折叠，保证不同逻辑位置 / 候选序号 /
//! 用途的输入不会塌缩到同一个计数器槽。

/// 抽样用途。
///
/// 不同用途拥有各自独立推进的序列。既可以直接用数值编号，也可以用
/// [`Purpose::named`] 从用途名字（如 `"dropout"`）派生稳定编号。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Purpose(pub u64);

impl Purpose {
    /// Dropout 掩码（内置常用用途，编号在编译期由 FNV-1a 摘要派生）。
    pub const DROPOUT: Purpose = Purpose(fnv1a_64(b"dropout"));
    /// 专家路由 / MoE 采样（内置常用用途，编号在编译期由 FNV-1a 摘要派生）。
    pub const ROUTING: Purpose = Purpose(fnv1a_64(b"routing"));

    /// 从用途名字派生稳定编号：`FNV-1a_64(name)`。
    #[must_use]
    pub fn named(name: &str) -> Self {
        Purpose(fnv1a_64(name.as_bytes()))
    }

    /// 直接使用数值编号（由调用方约定编号含义）。
    #[must_use]
    pub const fn number(value: u64) -> Self {
        Purpose(value)
    }

    /// 用途的原始编号。
    #[must_use]
    pub const fn as_u64(self) -> u64 {
        self.0
    }
}

/// 一条随机流的完整逻辑坐标。
///
/// 字段里**没有**实际 rank、回包顺序或执行分段信息——这些物理执行因素按需求
/// 不得影响取值。`operator_path` 在构造时就被折叠成 64 位摘要，因此本结构可
/// `Copy`、可放进确认点、可在进程间传递。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct StreamKey {
    /// 固定实验种子。
    pub seed: u64,
    /// 全局训练步。
    pub global_step: u64,
    /// 样本 ID。
    pub sample_id: u64,
    /// 算子路径（如层名），构造时折叠为摘要。
    pub operator_path: u64,
    /// 该路径的逻辑调用序号（物理重试不改变它）。
    pub logical_call: u64,
    /// 抽样用途。
    pub purpose: Purpose,
}

impl StreamKey {
    /// 从字符串算子路径构造流坐标（路径经 FNV-1a 折叠）。
    #[must_use]
    pub fn new(
        seed: u64,
        global_step: u64,
        sample_id: u64,
        operator_path: &str,
        logical_call: u64,
        purpose: Purpose,
    ) -> Self {
        Self::with_path_hash(
            seed,
            global_step,
            sample_id,
            fnv1a_64(operator_path.as_bytes()),
            logical_call,
            purpose,
        )
    }

    /// 从已经折叠好的路径摘要构造流坐标。
    #[must_use]
    pub const fn with_path_hash(
        seed: u64,
        global_step: u64,
        sample_id: u64,
        operator_path_hash: u64,
        logical_call: u64,
        purpose: Purpose,
    ) -> Self {
        StreamKey {
            seed,
            global_step,
            sample_id,
            operator_path: operator_path_hash,
            logical_call,
            purpose,
        }
    }

    /// 逻辑调用标识：不含 `purpose`，同一调用下不同用途共享同一个标识，
    /// 便于确认点以“逻辑调用”为单位组织进度。
    #[must_use]
    pub fn call_id(&self) -> u64 {
        mix64(fold_many(&[
            self.seed,
            self.global_step,
            self.sample_id,
            self.operator_path,
            self.logical_call,
        ]))
    }

    /// 整条流坐标的 64 位摘要，用作确认点里的键。
    #[must_use]
    pub fn stream_id(&self) -> u64 {
        mix64(fold_many(&[
            self.seed,
            self.global_step,
            self.sample_id,
            self.operator_path,
            self.logical_call,
            self.purpose.0,
        ]))
    }
}

/// SplitMix64 风格的双射混合：对 64 位输入做一次完整 avalanche。
///
/// 该函数是 `u64 -> u64` 的双射（可逆），因此不同计数器输入永远不会在混合前
/// 塌缩；输出满足雪崩特性，可作为确定性伪随机字使用。
#[must_use]
pub const fn mix64(x: u64) -> u64 {
    let mut z = x.wrapping_add(0x9e37_79b9_7f4a_7c15);
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    z ^ (z >> 31)
}

/// FNV-1a 64 位哈希（对字节序列），用于把算子路径 / 用途名字折叠成 `u64`。
#[must_use]
pub const fn fnv1a_64(bytes: &[u8]) -> u64 {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    let mut i = 0;
    while i < bytes.len() {
        hash ^= bytes[i] as u64;
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        i += 1;
    }
    hash
}

/// 把多个计数器字段折叠成单个 64 位基值。
///
/// 每轮先混入字段下标，再混入字段值，避免 `[a, b]` 与 `[b, a]` 等排列产生
/// 相同折叠结果；折叠结果再经 [`mix64`] 双射混合。
#[must_use]
pub const fn fold_many(fields: &[u64]) -> u64 {
    let mut acc: u64 = 0;
    let mut i = 0;
    while i < fields.len() {
        acc = mix64(acc ^ mix64(fields[i].wrapping_add((i as u64) << 56)));
        i += 1;
    }
    acc
}

/// 计数器模式随机字：`(流坐标, 抽样位置, 候选序号)` 的纯函数。
///
/// * `position` 是该用途流中的逻辑抽样位置；
/// * `attempt` 是该位置上被拒绝的候选数（首次抽样为 `0`，每拒绝一个候选加一）。
///
/// 同一个 `(key, position, attempt)` 在任何进程、任何 rank、任何时间调用都
/// 返回同一个值；不同位置 / 候选序号对应不同计数器槽。
#[must_use]
pub const fn candidate_word(key: &StreamKey, position: u64, attempt: u64) -> u64 {
    let base = fold_many(&[
        key.seed,
        key.global_step,
        key.sample_id,
        key.operator_path,
        key.logical_call,
        key.purpose.0,
    ]);
    mix64(fold_many(&[base, position, attempt]))
}

/// 在给定位置、给定候选序号上取随机字（[`candidate_word`] 的别名式入口）。
#[must_use]
pub const fn word_at(key: &StreamKey, position: u64, attempt: u64) -> u64 {
    candidate_word(key, position, attempt)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fnv1a_known_vectors() {
        // 标准 FNV-1a 64 测试向量。
        assert_eq!(fnv1a_64(b""), 0xcbf29ce484222325);
        assert_eq!(fnv1a_64(b"a"), 0xaf63dc4c8601ec8c);
        assert_eq!(fnv1a_64(b"foobar"), 0x85944171f73967e8);
    }

    #[test]
    fn mix64_is_bijective_on_small_cycle() {
        // 双射性质：对一组连续输入，输出两两不同（完整双射由算法常量保证）。
        let mut seen = std::collections::HashSet::new();
        for x in 0..10_000u64 {
            assert!(seen.insert(mix64(x)), "mix64 在小范围内发生碰撞");
        }
        // 已知回归向量，锁定实现不被意外改动。
        assert_eq!(mix64(0), 0xe220a8397b1dcdaf);
    }

    #[test]
    fn words_are_pure_and_position_attempt_sensitive() {
        let k = StreamKey::new(7, 3, 11, "layer/attn", 2, Purpose::DROPOUT);
        assert_eq!(candidate_word(&k, 0, 0), candidate_word(&k, 0, 0));
        assert_ne!(candidate_word(&k, 0, 0), candidate_word(&k, 1, 0));
        assert_ne!(candidate_word(&k, 0, 0), candidate_word(&k, 0, 1));
        assert_ne!(candidate_word(&k, 5, 2), candidate_word(&k, 5, 3));
    }

    #[test]
    fn purpose_named_matches_direct_hash() {
        assert_eq!(Purpose::named("dropout"), Purpose::DROPOUT);
        assert_eq!(Purpose::named("routing"), Purpose::ROUTING);
        assert_ne!(Purpose::named("dropout"), Purpose::named("routing"));
    }

    #[test]
    fn candidate_slots_do_not_collide_under_counter_variation() {
        // 固定流坐标，扫描 position/attempt 网格，不同计数器组合必须落到不同输出。
        let k = StreamKey::new(11, 22, 33, "op/x", 1, Purpose::DROPOUT);
        let mut seen = std::collections::HashSet::new();
        for position in 0..200u64 {
            for attempt in 0..200u64 {
                assert!(
                    seen.insert(candidate_word(&k, position, attempt)),
                    "位置 {position} 候选 {attempt} 与其他计数器槽碰撞"
                );
            }
        }
        // 同一槽位在多次调用间稳定（可重算性的最小性质）。
        for (p, a) in [(0u64, 0u64), (7, 3), (199, 199)] {
            assert_eq!(
                candidate_word(&k, p, a),
                candidate_word(&k, p, a)
            );
        }
    }

    #[test]
    fn distinct_coordinates_get_distinct_streams() {
        let base = StreamKey::new(1, 2, 3, "p", 4, Purpose::DROPOUT);
        let routing = StreamKey::new(1, 2, 3, "p", 4, Purpose::ROUTING);
        let variants = [
            StreamKey::new(2, 2, 3, "p", 4, Purpose::DROPOUT),
            StreamKey::new(1, 9, 3, "p", 4, Purpose::DROPOUT),
            StreamKey::new(1, 2, 8, "p", 4, Purpose::DROPOUT),
            StreamKey::new(1, 2, 3, "q", 4, Purpose::DROPOUT),
            StreamKey::new(1, 2, 3, "p", 5, Purpose::DROPOUT),
            routing,
        ];
        let mut ids: Vec<u64> = variants.iter().map(StreamKey::stream_id).collect();
        ids.push(base.stream_id());
        let unique = ids.iter().copied().collect::<std::collections::HashSet<_>>();
        assert_eq!(unique.len(), ids.len(), "不同坐标必须派生不同流");
        // call_id 与用途无关：同一逻辑调用换用途，call_id 不变。
        assert_eq!(base.call_id(), routing.call_id());
        assert_ne!(base.stream_id(), routing.stream_id());
    }
}
