//! 热键切块全外连接的调度算子。
//!
//! 算子负责：
//!
//! 1. 把两侧输入按给定块容量切块，空键行直接输出为未匹配行；
//! 2. 按等值键生成「左块 × 右块」的块对任务并维护其状态机；
//! 3. 处理成功/失败回调：只采纳当前尝试的回调，过期尝试的回调一律丢弃；
//! 4. 只在块参与的所有块对都确认后，才把块中未命中的行作为未匹配行输出；
//! 5. 通过 [`Metrics`] 记录任务输入峰值、尝试数、确认数、过期回调数等指标。

use std::collections::{BTreeMap, BTreeSet, HashSet};

use crate::block::{partition, Block};
use crate::row::{JoinOut, Row};

/// 左侧块 ID，按块创建顺序分配。
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct LeftBlockId(pub usize);

/// 右侧块 ID，按块创建顺序分配。
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct RightBlockId(pub usize);

/// 块对 ID，按块对创建顺序分配。
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct PairId(pub usize);

/// 尝试 ID（1 起），每次（重新）派发递增，用于识别过期回调。
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct AttemptId(pub u32);

/// 一个调度任务：同一块对的一次尝试，只持有一个左块和一个右块。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Task<K: Ord, V> {
    pub pair: PairId,
    pub attempt: AttemptId,
    pub left: Block<K, V, LeftBlockId>,
    pub right: Block<K, V, RightBlockId>,
}

impl<K: Ord, V> Task<K, V> {
    /// 该任务当前持有的输入行数（两个块行数之和）。
    pub fn input_rows(&self) -> usize {
        self.left.len() + self.right.len()
    }
}

/// 执行节点对一次尝试的成功回报。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PairResult {
    pub pair: PairId,
    pub attempt: AttemptId,
    /// 命中的（左行 ID, 右行 ID）对，每一对在该块对中至多出现一次。
    pub matches: Vec<(u64, u64)>,
    /// 该块对中至少命中过一次的左行 ID。
    pub left_hit_rows: Vec<u64>,
    /// 该块对中至少命中过一次的右行 ID。
    pub right_hit_rows: Vec<u64>,
}

/// 回调处理结果，用于区分「已采纳」和「过期丢弃」。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CallbackStatus {
    /// 回调属于当前尝试，已采纳并推进状态。
    Accepted,
    /// 回调属于过期尝试，或块对已确认，已丢弃。
    Stale,
}

/// 块对的内部状态机。
#[derive(Clone, Debug, PartialEq, Eq)]
enum PairState {
    /// 尚未派发（含失败后重置回待派发的情况）。
    Unsent,
    /// 某次尝试在途。
    InFlight(AttemptId),
    /// 已由某次成功尝试确认，终态。
    Confirmed(AttemptId),
}

/// 单个块的确认进度。
#[derive(Clone, Debug)]
struct BlockProgress {
    /// 该块参与的块对总数。
    total_pairs: usize,
    /// 已经确认的块对数。
    confirmed_pairs: usize,
}

/// 算子运行指标，用于检查内存上界与重试行为。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Metrics {
    /// 累计派发的尝试次数（含首次派发与重试/取代派发）。
    pub dispatched_attempts: u64,
    /// 已确认的块对数（确认是终态，每对至多一次）。
    pub confirmed_pairs: u64,
    /// 被采纳的失败回调数。
    pub accepted_failures: u64,
    /// 被采纳的成功回调数。
    pub accepted_successes: u64,
    /// 被丢弃的过期回调数（成功或失败）。
    pub stale_callbacks: u64,
    /// 任一单次任务持有的最大输入行数。
    pub peak_task_input_rows: usize,
    /// 直接走未匹配路径的空键行数（左右合计）。
    pub null_key_rows: u64,
    /// 输出的匹配对总数。
    pub matched_outputs: u64,
    /// 输出的左侧补空行总数。
    pub left_only_outputs: u64,
    /// 输出的右侧补空行总数。
    pub right_only_outputs: u64,
}

/// 块对的静态描述。
#[derive(Clone, Debug)]
struct PairInternal {
    left: LeftBlockId,
    right: RightBlockId,
    state: PairState,
    next_attempt: u32,
}

/// 热键切块全外连接算子。
///
/// 类型参数：
///
/// - `K`：等值键类型，需要可排序、可克隆；
/// - `V`：行载荷类型，附加条件由执行节点在块对上求值，算子本身不解释载荷。
pub struct FullOuterJoin<K: Ord, V> {
    block_cap: usize,
    left_blocks: Vec<Block<K, V, LeftBlockId>>,
    right_blocks: Vec<Block<K, V, RightBlockId>>,
    left_archive: BTreeMap<u64, Row<K, V>>,
    right_archive: BTreeMap<u64, Row<K, V>>,
    pairs: Vec<PairInternal>,
    left_progress: BTreeMap<LeftBlockId, BlockProgress>,
    right_progress: BTreeMap<RightBlockId, BlockProgress>,
    left_hits: HashSet<u64>,
    right_hits: HashSet<u64>,
    emitted_matches: HashSet<(u64, u64)>,
    output: Vec<JoinOut<K, V>>,
    metrics: Metrics,
    loaded: bool,
}

impl<K: Ord + Clone, V: Clone> FullOuterJoin<K, V> {
    /// 创建算子；`block_cap` 是每个块允许持有的最大行数，必须为正数。
    pub fn new(block_cap: usize) -> Self {
        assert!(block_cap > 0, "block capacity must be positive");
        Self {
            block_cap,
            left_blocks: Vec::new(),
            right_blocks: Vec::new(),
            left_archive: BTreeMap::new(),
            right_archive: BTreeMap::new(),
            pairs: Vec::new(),
            left_progress: BTreeMap::new(),
            right_progress: BTreeMap::new(),
            left_hits: HashSet::new(),
            right_hits: HashSet::new(),
            emitted_matches: HashSet::new(),
            output: Vec::new(),
            metrics: Metrics::default(),
            loaded: false,
        }
    }

    /// 载入两侧输入并完成切块与块对编排。每个算子只能调用一次。
    pub fn load(&mut self, left: &[Row<K, V>], right: &[Row<K, V>]) {
        assert!(!self.loaded, "inputs can only be loaded once");
        self.loaded = true;

        // 空键不参与任何块对，各自直接走未匹配路径。
        for row in left {
            if row.key.is_none() {
                self.metrics.null_key_rows += 1;
                self.metrics.left_only_outputs += 1;
                self.output.push(JoinOut::LeftOnly {
                    key: None,
                    left_id: row.id,
                    left_value: row.value.clone(),
                });
            }
        }
        for row in right {
            if row.key.is_none() {
                self.metrics.null_key_rows += 1;
                self.metrics.right_only_outputs += 1;
                self.output.push(JoinOut::RightOnly {
                    key: None,
                    right_id: row.id,
                    right_value: row.value.clone(),
                });
            }
        }

        // 按等值键分组（保持稳定顺序），只保留非空键。
        let mut left_by_key: BTreeMap<K, Vec<Row<K, V>>> = BTreeMap::new();
        let mut right_by_key: BTreeMap<K, Vec<Row<K, V>>> = BTreeMap::new();
        for row in left {
            if let Some(key) = &row.key {
                left_by_key.entry(key.clone()).or_default().push(row.clone());
            }
        }
        for row in right {
            if let Some(key) = &row.key {
                right_by_key.entry(key.clone()).or_default().push(row.clone());
            }
        }

        let all_keys: BTreeSet<K> = left_by_key
            .keys()
            .chain(right_by_key.keys())
            .cloned()
            .collect();

        for key in all_keys {
            let left_rows = left_by_key.get(&key).cloned().unwrap_or_default();
            let right_rows = right_by_key.get(&key).cloned().unwrap_or_default();

            // 切块并分配块 ID，同时归档行以便之后补空输出。
            let mut lids: Vec<LeftBlockId> = Vec::new();
            for chunk in partition(&left_rows, self.block_cap) {
                let id = LeftBlockId(self.left_blocks.len());
                for row in &chunk {
                    self.left_archive.insert(row.id, row.clone());
                }
                self.left_blocks.push(Block {
                    id,
                    rows: chunk,
                });
                lids.push(id);
            }

            let mut rids: Vec<RightBlockId> = Vec::new();
            for chunk in partition(&right_rows, self.block_cap) {
                let id = RightBlockId(self.right_blocks.len());
                for row in &chunk {
                    self.right_archive.insert(row.id, row.clone());
                }
                self.right_blocks.push(Block {
                    id,
                    rows: chunk,
                });
                rids.push(id);
            }

            // 单侧存在的键：没有任何可能包含配对的块，块可立即终态，全部补空。
            if lids.is_empty() {
                for rid in rids {
                    self.right_progress.insert(rid, BlockProgress {
                        total_pairs: 0,
                        confirmed_pairs: 0,
                    });
                    self.finalize_right_block(rid);
                }
                continue;
            }
            if rids.is_empty() {
                for lid in lids {
                    self.left_progress.insert(lid, BlockProgress {
                        total_pairs: 0,
                        confirmed_pairs: 0,
                    });
                    self.finalize_left_block(lid);
                }
                continue;
            }

            for &lid in &lids {
                self.left_progress.insert(
                    lid,
                    BlockProgress {
                        total_pairs: rids.len(),
                        confirmed_pairs: 0,
                    },
                );
            }
            for &rid in &rids {
                self.right_progress.insert(
                    rid,
                    BlockProgress {
                        total_pairs: lids.len(),
                        confirmed_pairs: 0,
                    },
                );
            }

            // 同键的左块 × 右块构成全部块对；不同键之间不产生块对。
            for &lid in &lids {
                for &rid in &rids {
                    self.pairs.push(PairInternal {
                        left: lid,
                        right: rid,
                        state: PairState::Unsent,
                        next_attempt: 1,
                    });
                }
            }
        }
    }

    /// 派发至多 `max_tasks` 个尚未在途/未确认的块对（首次派发或失败后重新派发）。
    pub fn schedule(&mut self, max_tasks: usize) -> Vec<Task<K, V>> {
        let pair_count = self.pairs.len();
        let mut tasks = Vec::new();
        for idx in 0..pair_count {
            if tasks.len() >= max_tasks {
                break;
            }
            let pair = PairId(idx);
            if matches!(self.pairs[idx].state, PairState::Unsent) {
                if let Some(task) = self.dispatch(pair) {
                    tasks.push(task);
                }
            }
        }
        tasks
    }

    /// 内部派发：分配尝试号、更新状态并统计输入峰值。
    fn dispatch(&mut self, pair: PairId) -> Option<Task<K, V>> {
        let internal = &mut self.pairs[pair.0];
        let attempt = AttemptId(internal.next_attempt);
        internal.next_attempt += 1;
        internal.state = PairState::InFlight(attempt);

        let left = self.left_blocks[internal.left.0].clone();
        let right = self.right_blocks[internal.right.0].clone();

        debug_assert!(left.len() <= self.block_cap);
        debug_assert!(right.len() <= self.block_cap);

        self.metrics.dispatched_attempts += 1;
        self.metrics.peak_task_input_rows =
            self.metrics.peak_task_input_rows.max(left.len() + right.len());

        Some(Task {
            pair,
            attempt,
            left,
            right,
        })
    }

    /// 对一个在途块对发起取代性重试：旧尝试立即作废，返回携带新尝试号的任务。
    ///
    /// 用于「同一块对用新尝试取代失败尝试」的主动调度：旧尝试之后到达的任何
    /// 回调都会因为尝试号过期而被丢弃。
    pub fn supersede_pair(&mut self, pair: PairId) -> Task<K, V> {
        assert!(
            matches!(self.pairs[pair.0].state, PairState::InFlight(_)),
            "can only supersede an in-flight pair"
        );
        self.dispatch(pair).expect("pair exists")
    }

    /// 对一个失败后回到待派发状态的块对重新派发。
    pub fn retry_pair(&mut self, pair: PairId) -> Task<K, V> {
        assert!(
            matches!(self.pairs[pair.0].state, PairState::Unsent),
            "can only retry a pair waiting for dispatch"
        );
        self.dispatch(pair).expect("pair exists")
    }

    /// 处理一次成功回调。过期回调（旧尝试或块对已确认）返回 [`CallbackStatus::Stale`]。
    pub fn on_success(&mut self, result: PairResult) -> CallbackStatus {
        let internal = &self.pairs[result.pair.0];
        match internal.state {
            PairState::InFlight(current) if current == result.attempt => {}
            _ => {
                self.metrics.stale_callbacks += 1;
                return CallbackStatus::Stale;
            }
        }

        // 采纳：块对进入确认终态。只采纳此前未输出过的匹配对，保证每对只输出一次。
        self.pairs[result.pair.0].state = PairState::Confirmed(result.attempt);
        self.metrics.confirmed_pairs += 1;
        self.metrics.accepted_successes += 1;

        let (lid, rid) = {
            let internal = &self.pairs[result.pair.0];
            (internal.left, internal.right)
        };

        for (left_id, right_id) in result.matches {
            if self.emitted_matches.insert((left_id, right_id)) {
                let left_row = self
                    .left_archive
                    .get(&left_id)
                    .expect("left row must exist");
                let right_row = self
                    .right_archive
                    .get(&right_id)
                    .expect("right row must exist");
                let key = left_row
                    .key
                    .clone()
                    .expect("matched rows cannot have null keys");
                self.metrics.matched_outputs += 1;
                self.output.push(JoinOut::Matched {
                    key,
                    left_id,
                    left_value: left_row.value.clone(),
                    right_id,
                    right_value: right_row.value.clone(),
                });
            }
        }

        for id in result.left_hit_rows {
            self.left_hits.insert(id);
        }
        for id in result.right_hit_rows {
            self.right_hits.insert(id);
        }

        // 先记录命中，再推进确认进度；推进可能立即触发块终态补空。
        self.advance_left(lid);
        self.advance_right(rid);
        CallbackStatus::Accepted
    }

    /// 处理一次失败回调。过期失败同样丢弃；采纳后块对回到待派发状态等待重试。
    pub fn on_failure(&mut self, pair: PairId, attempt: AttemptId) -> CallbackStatus {
        match self.pairs[pair.0].state {
            PairState::InFlight(current) if current == attempt => {}
            _ => {
                self.metrics.stale_callbacks += 1;
                return CallbackStatus::Stale;
            }
        }
        self.pairs[pair.0].state = PairState::Unsent;
        self.metrics.accepted_failures += 1;
        CallbackStatus::Accepted
    }

    fn advance_left(&mut self, lid: LeftBlockId) {
        let progress = self
            .left_progress
            .get_mut(&lid)
            .expect("left block progress must exist");
        progress.confirmed_pairs += 1;
        debug_assert!(progress.confirmed_pairs <= progress.total_pairs);
        if progress.confirmed_pairs == progress.total_pairs {
            self.finalize_left_block(lid);
        }
    }

    fn advance_right(&mut self, rid: RightBlockId) {
        let progress = self
            .right_progress
            .get_mut(&rid)
            .expect("right block progress must exist");
        progress.confirmed_pairs += 1;
        debug_assert!(progress.confirmed_pairs <= progress.total_pairs);
        if progress.confirmed_pairs == progress.total_pairs {
            self.finalize_right_block(rid);
        }
    }

    /// 块参与的所有块对都已确认：未命中的行才能作为未匹配行输出。
    fn finalize_left_block(&mut self, lid: LeftBlockId) {
        let block = self.left_blocks[lid.0].clone();
        for row in block.rows {
            if !self.left_hits.contains(&row.id) {
                self.metrics.left_only_outputs += 1;
                self.output.push(JoinOut::LeftOnly {
                    key: row.key,
                    left_id: row.id,
                    left_value: row.value,
                });
            }
        }
    }

    fn finalize_right_block(&mut self, rid: RightBlockId) {
        let block = self.right_blocks[rid.0].clone();
        for row in block.rows {
            if !self.right_hits.contains(&row.id) {
                self.metrics.right_only_outputs += 1;
                self.output.push(JoinOut::RightOnly {
                    key: row.key,
                    right_id: row.id,
                    right_value: row.value,
                });
            }
        }
    }

    /// 取出目前已经产生的输出（匹配对可在块对确认时随时产出，补空行在块终态时产出）。
    pub fn drain_output(&mut self) -> Vec<JoinOut<K, V>> {
        std::mem::take(&mut self.output)
    }

    /// 所有块对都已确认（没有在途或待派发任务）。
    pub fn is_done(&self) -> bool {
        self.pairs
            .iter()
            .all(|p| matches!(p.state, PairState::Confirmed(_)))
    }

    /// 运行指标快照。
    pub fn metrics(&self) -> Metrics {
        self.metrics.clone()
    }

    /// 块对总数。
    pub fn pair_count(&self) -> usize {
        self.pairs.len()
    }

    /// 左侧块数。
    pub fn left_block_count(&self) -> usize {
        self.left_blocks.len()
    }

    /// 右侧块数。
    pub fn right_block_count(&self) -> usize {
        self.right_blocks.len()
    }

    /// 查询块对当前的尝试号（下一次派发将使用的编号），测试与调度器可据此识别回调归属。
    pub fn next_attempt_of(&self, pair: PairId) -> AttemptId {
        AttemptId(self.pairs[pair.0].next_attempt)
    }

    /// 查询块对包含的左右块 ID。
    pub fn pair_blocks(&self, pair: PairId) -> (LeftBlockId, RightBlockId) {
        let internal = &self.pairs[pair.0];
        (internal.left, internal.right)
    }

    /// 块对是否处于待派发状态（含失败后等待重试）。
    pub fn pair_is_unsent(&self, pair: PairId) -> bool {
        matches!(self.pairs[pair.0].state, PairState::Unsent)
    }
}
