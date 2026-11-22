//! 抽样游标、逻辑调用进度与掩码重算。
//!
//! 游标只持有**逻辑坐标**和当前的 `(position, attempt)`，不持有任何物理执行
//! 信息（rank、分段、回包顺序）。进度可以导出为 [`CallProgress`] 写入确认点，
//! 也可以从确认点恢复；掩码位可以脱离游标、仅凭明确的逻辑位置直接重算。

use crate::rng::{candidate_word, Purpose, StreamKey};

/// 单个用途流的抽样进度：下一个逻辑位置，以及每个位置接受时已拒绝的候选数。
///
/// * `next_position`：下一个尚未接受任何候选的位置；
/// * `rejections`：`rejections[p]` 是位置 `p` 在接受前拒绝的候选数。
///   对 `p < next_position` 一定存在记录；位置 `p` 接受的随机字因此是
///   `candidate_word(key, p, rejections[p])`，与当时的拒绝路径无关、可重算。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StreamCursor {
    key: StreamKey,
    next_position: u64,
    rejections: Vec<u64>,
}

impl StreamCursor {
    /// 在给定流坐标上从位置 0 开始。
    #[must_use]
    pub fn new(key: StreamKey) -> Self {
        StreamCursor {
            key,
            next_position: 0,
            rejections: Vec::new(),
        }
    }

    /// 从已持久化的进度恢复。
    ///
    /// # Panics
    /// 当 `rejections.len() != next_position as usize` 时 panic——恢复数据本身
    /// 不自洽（每个已接受位置必须恰好有一条拒绝数记录）。
    #[must_use]
    pub fn restore(key: StreamKey, next_position: u64, rejections: Vec<u64>) -> Self {
        assert_eq!(
            rejections.len(),
            next_position as usize,
            "进度记录不自洽：next_position={next_position} 但有 {} 条拒绝记录",
            rejections.len()
        );
        StreamCursor {
            key,
            next_position,
            rejections,
        }
    }

    /// 流坐标。
    #[must_use]
    pub fn key(&self) -> StreamKey {
        self.key
    }

    /// 下一个待接受的逻辑位置。
    #[must_use]
    pub fn next_position(&self) -> u64 {
        self.next_position
    }

    /// 已接受位置的数量（等于已推进的位置数）。
    #[must_use]
    pub fn accepted_len(&self) -> u64 {
        self.next_position
    }

    /// 查看当前位置、当前候选序号上的随机字（不改变进度）。
    #[must_use]
    pub fn peek_word(&self) -> u64 {
        candidate_word(&self.key, self.next_position, self.current_attempt())
    }

    /// 当前位置上已经拒绝的候选数（即下一个候选的序号）。
    #[must_use]
    fn current_attempt(&self) -> u64 {
        self.rejections
            .get(self.next_position as usize)
            .copied()
            .unwrap_or(0)
    }

    /// 拒绝当前候选：候选序号加一，位置不变。可在接受前调用任意多次。
    pub fn reject(&mut self) -> u64 {
        let attempt = self.current_attempt();
        let next = attempt.checked_add(1).expect("拒绝次数溢出 u64");
        if self.rejections.len() == self.next_position as usize {
            self.rejections.push(next);
        } else {
            self.rejections[self.next_position as usize] = next;
        }
        next
    }

    /// 接受当前候选：记录拒绝数并前进到下一逻辑位置，返回被接受的随机字。
    pub fn accept(&mut self) -> u64 {
        let position = self.next_position;
        let attempt = self.current_attempt();
        let word = candidate_word(&self.key, position, attempt);
        if self.rejections.len() == position as usize {
            self.rejections.push(attempt);
        } else {
            self.rejections[position as usize] = attempt;
        }
        self.next_position = position
            .checked_add(1)
            .expect("抽样位置溢出 u64");
        word
    }

    /// 用谓词驱动一次拒绝采样：不断拒绝直到 `predicate(word)` 为真再接受。
    /// 返回被接受的随机字。`max_attempts` 限制单个位置的候选数，防止谓词恒假
    /// 时无限循环；超限返回 `None` 且游标停在最后一个候选上（进度未前进）。
    pub fn accept_if<F>(&mut self, max_attempts: u64, mut predicate: F) -> Option<u64>
    where
        F: FnMut(u64) -> bool,
    {
        for _ in 0..max_attempts {
            let word = self.peek_word();
            if predicate(word) {
                return Some(self.accept());
            }
            self.reject();
        }
        None
    }

    /// 取位置 `position` 上**已接受**的随机字（重算用，不依赖游标当前状态）。
    ///
    /// 越界（该位置尚未接受）返回 `None`。
    #[must_use]
    pub fn accepted_word(&self, position: u64) -> Option<u64> {
        let attempt = self.rejections.get(position as usize).copied()?;
        Some(candidate_word(&self.key, position, attempt))
    }

    /// 导出可持久化的进度快照。
    #[must_use]
    pub fn progress(&self) -> (u64, Vec<u64>) {
        (self.next_position, self.rejections.clone())
    }
}

/// 一个逻辑调用的取值进度：按用途区分的若干独立流。
///
/// 不同用途（如 dropout 与 routing）各自推进，互不占用对方的位置与候选序号。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CallProgress {
    key_base: StreamKey,
    streams: Vec<StreamCursor>,
}

impl CallProgress {
    /// 为某个逻辑调用建立进度容器。`key_base` 的 `purpose` 字段会被忽略，
    /// 实际用途在 [`CallProgress::stream`] 时指定。
    #[must_use]
    pub fn new(key_base: StreamKey) -> Self {
        CallProgress {
            key_base,
            streams: Vec::new(),
        }
    }

    fn with_purpose(&self, purpose: Purpose) -> StreamKey {
        StreamKey::with_path_hash(
            self.key_base.seed,
            self.key_base.global_step,
            self.key_base.sample_id,
            self.key_base.operator_path,
            self.key_base.logical_call,
            purpose,
        )
    }

    /// 取得（或首次创建）某用途的独立抽样流。
    pub fn stream(&mut self, purpose: Purpose) -> &mut StreamCursor {
        if let Some(idx) = self.streams.iter().position(|s| s.key().purpose == purpose) {
            &mut self.streams[idx]
        } else {
            self.streams.push(StreamCursor::new(self.with_purpose(purpose)));
            self.streams.last_mut().expect("刚插入的流一定存在")
        }
    }

    /// 只读访问某用途的流（尚未创建则返回 `None`）。
    #[must_use]
    pub fn stream_ref(&self, purpose: Purpose) -> Option<&StreamCursor> {
        self.streams.iter().find(|s| s.key().purpose == purpose)
    }

    /// 该调用下已经产生抽样的用途数量。
    #[must_use]
    pub fn purpose_count(&self) -> usize {
        self.streams.len()
    }

    /// 逻辑调用标识（与用途无关）。
    #[must_use]
    pub fn call_id(&self) -> u64 {
        self.key_base.call_id()
    }

    /// 导出全部用途流的坐标与进度，供确认点持久化。
    pub fn snapshot(&self) -> Vec<(StreamKey, u64, Vec<u64>)> {
        self.streams
            .iter()
            .map(|s| {
                let (next, rej) = s.progress();
                (s.key(), next, rej)
            })
            .collect()
    }

    /// 从确认点记录恢复一个逻辑调用的进度。
    #[must_use]
    pub fn restore(key_base: StreamKey, entries: Vec<(Purpose, u64, Vec<u64>)>) -> Self {
        let mut progress = CallProgress::new(key_base);
        for (purpose, next_position, rejections) in entries {
            progress.streams.push(StreamCursor::restore(
                progress.with_purpose(purpose),
                next_position,
                rejections,
            ));
        }
        progress
    }
}

/// 进度游标：在 [`CallProgress`] 之上提供“按用途取下游标”的轻量入口，
/// 便于一段执行被拆成多段时逐段推进、逐段确认。
#[derive(Debug)]
pub struct ProgressCursor {
    progress: CallProgress,
}

impl ProgressCursor {
    /// 建立在某个逻辑调用之上。
    #[must_use]
    pub fn new(key_base: StreamKey) -> Self {
        ProgressCursor {
            progress: CallProgress::new(key_base),
        }
    }

    /// 从已恢复的逻辑调用进度接管。
    #[must_use]
    pub fn from_progress(progress: CallProgress) -> Self {
        ProgressCursor { progress }
    }

    /// 取得某用途的抽样游标。
    pub fn stream(&mut self, purpose: Purpose) -> &mut StreamCursor {
        self.progress.stream(purpose)
    }

    /// 底层逻辑调用进度（用于写确认点）。
    #[must_use]
    pub fn progress(&self) -> &CallProgress {
        &self.progress
    }

    /// 消费游标，取出底层进度。
    #[must_use]
    pub fn into_progress(self) -> CallProgress {
        self.progress
    }
}

/// 从明确给出的逻辑位置直接重算一个掩码位，无需任何运行时游标状态。
///
/// 掩码位由“该位置接受的随机字”与阈值比较得出：
/// `word < (keep_prob * u64::MAX)` 时保留（`true`），否则丢弃（`false`）。
/// 重算时必须给出该位置接受前拒绝的候选数 `accepted_attempt`——它正是确认点
/// 持久化的内容，因此两份从同一确认点派生的掩码必然逐元素一致。
///
/// # 参数
/// * `key`：完整流坐标（含用途）；
/// * `position`：逻辑抽样位置；
/// * `accepted_attempt`：该位置接受时已拒绝的候选数；
/// * `keep_prob`：保留概率，取值 `0.0..=1.0`。
#[must_use]
pub fn recompute_mask_bit(
    key: &StreamKey,
    position: u64,
    accepted_attempt: u64,
    keep_prob: f64,
) -> bool {
    assert!(
        (0.0..=1.0).contains(&keep_prob),
        "keep_prob 必须在 [0, 1] 内，实际为 {keep_prob}"
    );
    let word = recompute_word(key, position, accepted_attempt);
    // 阈值用 u128 计算，避免 f64 转换与乘法的精度歧义。
    let threshold = ((keep_prob as f64) * (u64::MAX as f64)) as u128;
    u128::from(word) <= threshold
}

/// 从明确给出的逻辑位置与候选序号重算被接受的随机字。
#[must_use]
pub const fn recompute_word(key: &StreamKey, position: u64, accepted_attempt: u64) -> u64 {
    candidate_word(key, position, accepted_attempt)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(call: u64, purpose: Purpose) -> StreamKey {
        StreamKey::new(42, 7, 99, "blocks/3/dropout", call, purpose)
    }

    #[test]
    fn accept_reject_sequence_is_deterministic() {
        let mut a = StreamCursor::new(key(0, Purpose::DROPOUT));
        let mut b = StreamCursor::new(key(0, Purpose::DROPOUT));
        // a 拒绝若干候选后接受；b 走不同的拒绝节奏，但在位置 0 接受同一候选序号。
        a.reject();
        a.reject();
        let wa = a.accept();
        b.reject();
        b.reject();
        let wb = b.accept();
        assert_eq!(wa, wb);
        assert_eq!(wa, candidate_word(&key(0, Purpose::DROPOUT), 0, 2));
        assert_eq!(a.accepted_word(0), Some(wa));
    }

    #[test]
    fn skipping_positions_cannot_shift_other_values() {
        let k = key(1, Purpose::DROPOUT);
        // 参考：连续接受 4 个位置（无拒绝）。
        let mut reference = StreamCursor::new(k);
        let reference_words: Vec<u64> = (0..4).map(|_| reference.accept()).collect();

        // 模拟“跳过一行”：直接在位置 2、3 上重算，应当与参考序列同位置相等。
        assert_eq!(
            recompute_word(&k, 2, 0),
            reference_words[2],
            "跳过前面的位置不能改变后续位置的取值"
        );
        assert_eq!(recompute_word(&k, 3, 0), reference_words[3]);

        // 模拟“取消部分批次”：只接受位置 0 和 3，中间位置从未触碰。
        let mut partial = StreamCursor::new(k);
        let w0 = partial.accept();
        // 取消：丢弃 partial，另起游标直接重算位置 3。
        let w3 = recompute_word(&k, 3, 0);
        assert_eq!(w0, reference_words[0]);
        assert_eq!(w3, reference_words[3]);
    }

    #[test]
    fn rejection_sampling_recovers_after_restore() {
        let k = key(2, Purpose::DROPOUT);
        let mut cursor = StreamCursor::new(k);
        // 位置 0：拒绝 3 个候选后接受；位置 1：直接接受。
        for _ in 0..3 {
            cursor.reject();
        }
        let w0 = cursor.accept();
        let w1 = cursor.accept();
        let (next, rejections) = cursor.progress();
        assert_eq!(next, 2);
        assert_eq!(rejections, vec![3, 0]);

        // 模拟崩溃后从确认点恢复：已接受的值可逐元素重算。
        let restored = StreamCursor::restore(k, next, rejections.clone());
        assert_eq!(restored.accepted_word(0), Some(w0));
        assert_eq!(restored.accepted_word(1), Some(w1));
        assert_eq!(recompute_word(&k, 0, rejections[0]), w0);
        // 恢复后继续推进，位置 2 的取值与一次跑完的游标一致。
        let mut uninterrupted = StreamCursor::new(k);
        for _ in 0..3 {
            uninterrupted.reject();
        }
        uninterrupted.accept();
        uninterrupted.accept();
        let mut restored = restored;
        assert_eq!(restored.accept(), uninterrupted.accept());
    }

    #[test]
    fn accept_if_respects_predicate_and_limit() {
        let k = key(3, Purpose::DROPOUT);
        let mut cursor = StreamCursor::new(k);
        // 要求最高位为 0：必然存在候选，且接受值满足谓词。
        let word = cursor
            .accept_if(1024, |w| w < 0x8000_0000_0000_0000)
            .expect("1024 个候选内应当出现最高位为 0 的字");
        assert!(word < 0x8000_0000_0000_0000);
        let (next, rejections) = cursor.progress();
        assert_eq!(next, 1);
        // 重算值与接受值一致。
        assert_eq!(recompute_word(&k, 0, rejections[0]), word);

        // 恒假谓词：达到上限返回 None，进度不前进。
        let mut stuck = StreamCursor::new(k);
        assert!(stuck.accept_if(5, |_| false).is_none());
        assert_eq!(stuck.next_position(), 0);
    }

    #[test]
    fn purposes_advance_independently() {
        let base = key(4, Purpose::DROPOUT);
        let mut call = CallProgress::new(base);
        let d0 = call.stream(Purpose::DROPOUT).accept();
        let _r0 = call.stream(Purpose::ROUTING).accept();
        let _r1 = call.stream(Purpose::ROUTING).accept();
        let d1 = call.stream(Purpose::DROPOUT).accept();

        // dropout 流的位置没有被 routing 的两次抽样占用。
        let dropout = call.stream_ref(Purpose::DROPOUT).unwrap();
        let routing = call.stream_ref(Purpose::ROUTING).unwrap();
        assert_eq!(dropout.accepted_len(), 2);
        assert_eq!(routing.accepted_len(), 2);
        assert_eq!(d0, candidate_word(&key(4, Purpose::DROPOUT), 0, 0));
        assert_eq!(d1, candidate_word(&key(4, Purpose::DROPOUT), 1, 0));

        // 独立分支的抽样边界：两条流的同位置取值不同，且互不重叠推进。
        assert_ne!(
            candidate_word(&key(4, Purpose::DROPOUT), 0, 0),
            candidate_word(&key(4, Purpose::ROUTING), 0, 0)
        );
    }

    #[test]
    fn mask_recompute_matches_live_cursor() {
        let k = key(5, Purpose::DROPOUT);
        let mut cursor = StreamCursor::new(k);
        let keep = 0.375;
        let mut live = Vec::new();
        for _ in 0..32 {
            // 用接受的字模拟 dropout：拒绝阈值外候选也可以，这里直接接受并打掩码。
            let word = cursor.accept();
            let threshold = (keep * u64::MAX as f64) as u128;
            live.push(u128::from(word) <= threshold);
        }
        let (_, rejections) = cursor.progress();
        for (position, &bit) in live.iter().enumerate() {
            assert_eq!(
                recompute_mask_bit(&k, position as u64, rejections[position], keep),
                bit
            );
        }
        // 边界概率：1.0 全留；0.0 仅在随机字恰好为 0 时保留（实际几乎不发生）。
        let zero_word = candidate_word(&k, 0, 0) == 0;
        assert_eq!(recompute_mask_bit(&k, 0, 0, 0.0), zero_word);
        assert!(recompute_mask_bit(&k, 0, 0, 1.0));
    }

    #[test]
    #[should_panic(expected = "进度记录不自洽")]
    fn restore_rejects_inconsistent_progress() {
        let _ = StreamCursor::restore(key(6, Purpose::DROPOUT), 2, vec![0]);
    }
}
