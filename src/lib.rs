//! # mirror-recovery
//!
//! 按分区换算恢复位点的故障切换模块：消费者从来源日志 **A** 切到消息镜像 **B**，
//! 之后还要能切回 A（并可再次切到 B）。
//!
//! ## 领域模型
//!
//! - A 的每条消息拥有 **来源位点（source offset）** 与 **稳定身份（stable id）**。
//! - B 的实际位点由到达顺序决定（`b_offset`），B 中可以混入 B 侧原生消息，
//!   同一条 A 消息也可能被重复镜像。
//! - A 可以报告 **明确被裁剪的区间**（[`TruncatedRange`]）。“尚未镜像”与
//!   “明确裁剪”必须分别处理：裁剪区间可以直接折叠跳过，未镜像位点则形成待补区间。
//!
//! ## 核心不变量
//!
//! 1. 每个分区维护一个连续确认前沿 [`PartitionState::frontier`]：所有 `<= frontier`
//!    的来源位点要么已经投递给消费者并确认，要么属于已报告的裁剪区间。
//! 2. 切换时从 `frontier + 1` 开始读取仍存在于 B 的消息，严格按来源位点顺序投递，
//!    在第一个“既无法证明存在（无镜像实体）、也无法证明被裁剪”的位点停止，
//!    并通过 [`Gap`] 给出应补的区间。
//! 3. B 侧确认只能推进“已经读到且连续”的来源前缀；乱序确认、确认未投递的位点
//!    都会被拒绝（[`RecoveryError::NotDelivered`] /
//!    [`RecoveryError::OutOfOrderAck`]）。
//! 4. 重开后的重放与切回 A 都以 `frontier` 为唯一前沿：低于前沿的镜像事件
//!    直接判定为 [`IngestOutcome::Replayed`] 而不会再次投递。
//!
//! 整个 crate 仅依赖 Rust 1.95.0 标准库。

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt;
use std::sync::Mutex;

pub mod snapshot;

/// 位点类型。A 的来源位点与 B 的到达位点都使用该类型，但语义互相独立。
pub type Offset = u64;
/// 分区标识。
pub type Partition = u32;
/// A 消息的稳定身份（在镜像重复、重放时保持不变）。
pub type MessageId = String;

// ---------------------------------------------------------------------------
// 错误类型
// ---------------------------------------------------------------------------

/// 模块对外返回的所有错误。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecoveryError {
    /// 操作的分区尚未通过 [`RecoveryManager::ensure_partition`] 建立。
    UnknownPartition(Partition),
    /// 裁剪区间非法：`from > to`。
    InvalidTruncation { from: Offset, to: Offset },
    /// 确认了一个从未投递给当前消费者的来源位点（B 侧确认只能确认在途消息）。
    NotDelivered {
        partition: Partition,
        source_offset: Offset,
        frontier: Offset,
    },
    /// 切回 A 时的确认不是紧接前沿的下一位点（A 侧确认必须连续）。
    OutOfOrderAck {
        partition: Partition,
        source_offset: Offset,
        expected: Offset,
    },
    /// 快照文本无法解析。
    Snapshot(String),
}

impl fmt::Display for RecoveryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RecoveryError::UnknownPartition(p) => write!(f, "unknown partition {p}"),
            RecoveryError::InvalidTruncation { from, to } => {
                write!(f, "invalid truncation range [{from}, {to}]: from > to")
            }
            RecoveryError::NotDelivered {
                partition,
                source_offset,
                frontier,
            } => write!(
                f,
                "source offset {source_offset} on partition {partition} was never delivered (frontier={frontier})"
            ),
            RecoveryError::OutOfOrderAck {
                partition,
                source_offset,
                expected,
            } => write!(
                f,
                "out-of-order ack on partition {partition}: got {source_offset}, expected {expected}"
            ),
            RecoveryError::Snapshot(msg) => write!(f, "invalid snapshot: {msg}"),
        }
    }
}

impl std::error::Error for RecoveryError {}

// ---------------------------------------------------------------------------
// 输入数据：裁剪区间与镜像事件
// ---------------------------------------------------------------------------

/// A 明确报告的已裁剪来源位点闭区间 `[from, to]`。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct TruncatedRange {
    pub from: Offset,
    pub to: Offset,
}

impl TruncatedRange {
    /// 构造闭区间；`from > to` 时返回 [`RecoveryError::InvalidTruncation`]。
    pub fn new(from: Offset, to: Offset) -> Result<Self, RecoveryError> {
        if from > to {
            return Err(RecoveryError::InvalidTruncation { from, to });
        }
        Ok(Self { from, to })
    }

    /// 位点是否落在该裁剪闭区间内。
    pub fn contains(&self, offset: Offset) -> bool {
        offset >= self.from && offset <= self.to
    }
}

/// B 侧观察到的一条事件。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MirrorEvent {
    /// A 消息的一次镜像：携带 B 实际位点、A 来源位点、稳定身份与负载。
    Mirrored {
        b_offset: Offset,
        source_offset: Offset,
        id: MessageId,
        payload: Vec<u8>,
    },
    /// B 侧原生消息：没有 A 来源位点，独立于来源前沿确认。
    Native {
        b_offset: Offset,
        payload: Vec<u8>,
    },
}

// ---------------------------------------------------------------------------
// 写入结果
// ---------------------------------------------------------------------------

/// 写入一条 B 侧事件后的归类结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum IngestOutcome {
    /// 新事件已纳入分区缓冲。
    Accepted {
        b_offset: Offset,
        source_offset: Offset,
    },
    /// 同一稳定身份、同一来源位点的重复镜像：保留首次到达的副本。
    Duplicate {
        b_offset: Offset,
        source_offset: Offset,
        id: MessageId,
    },
    /// 事件对应的来源位点不高于当前前沿（或此前已确认）：重放数据，直接丢弃。
    Replayed {
        b_offset: Offset,
        source_offset: Offset,
    },
    /// 身份/位点映射与既有证据矛盾，事件被隔离，绝不投递给消费者。
    Conflict {
        /// 发生冲突的稳定身份（若为同身份不同位点，则是该身份；
        /// 若为同位点不同身份，则是已存事件的身份）。
        id: MessageId,
        /// 既有证据记录的来源位点。
        existing_source_offset: Offset,
        /// 被拒绝事件在 B 上的实际位点。
        rejected_b_offset: Offset,
    },
}

// ---------------------------------------------------------------------------
// 投递结果
// ---------------------------------------------------------------------------

/// 投递条目类别。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeliveryKind {
    /// A 消息的镜像，按来源位点顺序投递。
    Mirrored,
    /// B 侧原生消息，按 B 到达位点顺序投递。
    Native,
}

/// 一次 drain 中投递给消费者的一条消息。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Delivery {
    pub kind: DeliveryKind,
    /// B 上的实际位点（到达顺序）。
    pub b_offset: Offset,
    /// A 来源位点；B 侧原生消息为 `None`。
    pub source_offset: Option<Offset>,
    /// A 消息稳定身份；B 侧原生消息为 `None`。
    pub id: Option<MessageId>,
    pub payload: Vec<u8>,
}

/// 待补区间：从 `from` 开始无法证明存在或裁剪；`to` 为下一份已知证据
/// （镜像实体或裁剪区间起点）的位点，`None` 表示已知证据到此为止。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Gap {
    pub from: Offset,
    pub to: Option<Offset>,
}

/// 一次 drain 结束时的状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DrainStatus {
    /// 遇到了无法证明存在或裁剪的位点，见 [`DrainReport::gap`]。
    Gap,
    /// 没有缺口：要么在途消息等待消费者确认，要么本批次投递量达到上限，
    /// 要么暂时没有任何可投递数据。
    AwaitingAck,
}

/// [`RecoveryManager::drain`] 的完整报告。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DrainReport {
    pub partition: Partition,
    /// drain 结束后的连续确认前沿。
    pub frontier: Offset,
    /// 本批次实际投递给消费者的消息（镜像按来源位点升序，原生按 B 位点升序）。
    pub delivered: Vec<Delivery>,
    /// 第一个无法证明存在或裁剪的位点所界定的待补区间（若有）。
    pub gap: Option<Gap>,
    /// 本次 drain 中被折叠跳过的裁剪区间。
    pub skipped_truncations: Vec<TruncatedRange>,
    pub status: DrainStatus,
}

// ---------------------------------------------------------------------------
// 确认结果
// ---------------------------------------------------------------------------

/// 确认处理结果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AckOutcome {
    /// 该位点此前已经确认（或已低于前沿），幂等返回。
    AlreadyConfirmed {
        source_offset: Offset,
        frontier: Offset,
    },
    /// 确认已被记录；`frontier` 是处理后的连续前沿。注意 B 侧确认只能推进
    /// 连续前缀：若前方仍有在途消息，本次确认虽已入账，前沿可能暂不推进。
    Advanced { frontier: Offset },
}

// ---------------------------------------------------------------------------
// 分区内部状态
// ---------------------------------------------------------------------------

/// 一条已接收的 A 消息镜像的内部记录。
#[derive(Debug, Clone, PartialEq, Eq)]
struct MirroredEntry {
    b_offset: Offset,
    source_offset: Offset,
    id: MessageId,
    payload: Vec<u8>,
    /// 是否已投递给当前会话的消费者（投递后进入 pending，等待确认）。
    delivered: bool,
    /// 是否为重开恢复出来的在途消息：此类消息尚未被当前会话确认，允许重投一次。
    restored_inflight: bool,
}

/// 单个分区的完整恢复状态。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PartitionState {
    /// 连续确认前沿：所有 `<= frontier` 的来源位点均已确认或已裁剪。
    frontier: Offset,
    /// 仍在前沿之后的镜像实体，按来源位点索引（首个副本获胜）。
    entries: BTreeMap<Offset, MirroredEntry>,
    /// 已投递并确认、但因后方存在缺口尚未并入前沿的来源位点。
    acked: BTreeSet<Offset>,
    /// 已投递但尚未确认的来源位点。
    pending: BTreeSet<Offset>,
    /// A 报告过的裁剪区间，按起点升序、互不重叠（相邻区间会合并）。
    truncations: Vec<TruncatedRange>,
    /// 已接收但尚未经 drain 投递的 B 侧原生消息（B 位点 -> 负载）。
    native_pending: BTreeMap<Offset, Vec<u8>>,
    /// 已投递但尚未确认的 B 侧原生消息（在途，重开后需重投）。
    native_inflight: BTreeMap<Offset, Vec<u8>>,
    /// 已确认的 B 侧原生消息位点。
    native_acked: BTreeSet<Offset>,
    /// 稳定身份 -> 来源位点，用于重复镜像识别与冲突检测。
    id_index: HashMap<MessageId, Offset>,
    /// 上次 drain 报告时的前沿：用于归集两次 drain 之间由 ack/fold
    /// 隐式跨过的裁剪区间，保证每个被折叠的区间至少被报告一次。
    last_drain_frontier: Option<Offset>,
}

impl PartitionState {
    fn new(initial_frontier: Offset) -> Self {
        Self {
            frontier: initial_frontier,
            entries: BTreeMap::new(),
            acked: BTreeSet::new(),
            pending: BTreeSet::new(),
            truncations: Vec::new(),
            native_pending: BTreeMap::new(),
            native_inflight: BTreeMap::new(),
            native_acked: BTreeSet::new(),
            id_index: HashMap::new(),
            last_drain_frontier: None,
        }
    }

    /// 当前连续确认前沿。
    pub fn frontier(&self) -> Offset {
        self.frontier
    }

    /// 已报告的裁剪区间快照（按起点升序）。
    pub fn truncations(&self) -> &[TruncatedRange] {
        &self.truncations
    }

    /// 已接收、尚未投递或投递后未确认的 B 侧原生消息位点（重开后均需重投）。
    pub fn native_pending_offsets(&self) -> BTreeSet<Offset> {
        self.native_pending
            .keys()
            .chain(self.native_inflight.keys())
            .copied()
            .collect()
    }

    /// 已确认的 B 侧原生消息位点。
    pub fn native_acked_offsets(&self) -> BTreeSet<Offset> {
        self.native_acked.clone()
    }

    /// 位点是否被某个已报告区间明确裁剪。
    fn is_truncated(&self, offset: Offset) -> bool {
        self.truncations.iter().any(|r| r.contains(offset))
    }

    /// 组装 drain 报告并推进“上次报告前沿”，保证跨 drain 的裁剪折叠可被归集。
    fn finish_drain(
        &mut self,
        partition: Partition,
        report_from: Offset,
        delivered: Vec<Delivery>,
        gap: Option<Gap>,
        explicit: BTreeSet<TruncatedRange>,
        status: DrainStatus,
    ) -> DrainReport {
        let skipped =
            self.collect_crossed_truncations(report_from, self.frontier, explicit);
        self.last_drain_frontier = Some(self.frontier);
        DrainReport {
            partition,
            frontier: self.frontier,
            delivered,
            gap,
            skipped_truncations: skipped,
            status,
        }
    }

    /// 归集本次 drain 期间前沿跨过的裁剪区间。
    ///
    /// 既包含 drain 主循环显式折叠的 `explicit`，也包含确认消息后由
    /// [`Self::fold`] 隐式跨过的、落在 `(from_frontier, to_frontier]` 内的区间。
    fn collect_crossed_truncations(
        &self,
        from_frontier: Offset,
        to_frontier: Offset,
        explicit: BTreeSet<TruncatedRange>,
    ) -> Vec<TruncatedRange> {
        let mut out = explicit;
        if to_frontier > from_frontier {
            for range in &self.truncations {
                // 区间与新跨过的来源位点范围 (from_frontier, to_frontier] 有交集。
                if range.to > from_frontier && range.from <= to_frontier {
                    let clipped = TruncatedRange {
                        from: range.from.max(from_frontier.saturating_add(1)),
                        to: range.to.min(to_frontier),
                    };
                    out.insert(clipped);
                }
            }
        }
        out.into_iter().collect()
    }

    /// 扫描被在途消息阻断后、更远处的第一个缺口。
    ///
    /// 从 `blocked_at` 开始跳过所有连续的在途（pending）来源位点，
    /// 然后继续跳过所有有镜像实体（无论是否在途）或被裁剪的位点，
    /// 返回第一个“无镜像实体且未裁剪”的位点所界定的 [`Gap`]；
    /// 若证据链一直完整则返回 `None`。
    fn peek_gap_behind_pending(&self, blocked_at: Offset) -> Option<Gap> {
        let mut cursor = blocked_at;
        // 跳过连续的在途消息。
        while self.pending.contains(&cursor) {
            if cursor == Offset::MAX {
                return None;
            }
            cursor = cursor.saturating_add(1);
        }
        // cursor 现在是在途段之后第一个未在途的位点；继续沿证据链前进。
        loop {
            if self.entries.contains_key(&cursor) {
                // 有实体证据（无论是否已在途）：该位点可证明存在，继续看一位点。
                if cursor == Offset::MAX {
                    return None;
                }
                cursor = cursor.saturating_add(1);
                continue;
            }
            if self.is_truncated(cursor) {
                // 裁剪证据：跳到该裁剪区间之后继续。
                let range = self
                    .truncations
                    .iter()
                    .find(|r| r.contains(cursor))
                    .copied()
                    .expect("truncation range present");
                if range.to == Offset::MAX {
                    return None;
                }
                cursor = range.to.saturating_add(1);
                continue;
            }
            // 第一个无法证明存在或裁剪的位点。
            let next_entry = self.entries.range(cursor..).next().map(|(off, _)| *off);
            let next_truncation = self
                .truncations
                .iter()
                .filter(|r| r.from >= cursor)
                .map(|r| r.from)
                .min();
            let to = match (next_entry, next_truncation) {
                (Some(a), Some(b)) => Some(a.min(b)),
                (Some(a), None) => Some(a),
                (None, Some(b)) => Some(b),
                (None, None) => None,
            };
            return Some(Gap { from: cursor, to });
        }
    }

    /// 登记一个裁剪区间，与既有区间做有序合并（重叠或相邻即合并）。
    fn add_truncation(&mut self, range: TruncatedRange) {
        let mut merged: Vec<TruncatedRange> = Vec::with_capacity(self.truncations.len() + 1);
        let mut incoming = range;
        let mut inserted = false;

        for existing in std::mem::take(&mut self.truncations) {
            let overlap_or_adjacent =
                existing.to.saturating_add(1) >= incoming.from && incoming.to.saturating_add(1) >= existing.from;
            if overlap_or_adjacent {
                incoming = TruncatedRange {
                    from: existing.from.min(incoming.from),
                    to: existing.to.max(incoming.to),
                };
                continue;
            }
            if !inserted && incoming.to < existing.from {
                merged.push(incoming);
                inserted = true;
            }
            merged.push(existing);
        }
        if !inserted {
            merged.push(incoming);
        }
        self.truncations = merged;
    }

    /// 从前沿开始，把所有“已确认的连续来源位点”与“连续命中的裁剪区间”并入前沿。
    /// 遇到第一个缺口（未确认、未裁剪，且没有镜像实体可以投递）即停止。
    fn fold(&mut self) {
        // 前沿已到达位点空间尽头：saturating_add(1) 会回到自身，直接停止。
        while self.frontier != Offset::MAX {
            let want = self.frontier.saturating_add(1);
            if self.acked.contains(&want) {
                // 已确认本身就是“存在且已处理”的证据：无论实体是否还在缓冲，
                // 连续前缀都可以推进，并顺手清理可能残留的实体与标记。
                if let Some(entry) = self.entries.remove(&want) {
                    self.id_index.remove(&entry.id);
                }
                self.pending.remove(&want);
                self.acked.remove(&want);
                self.frontier = want;
            } else if self.entries.contains_key(&want) {
                // 实体仍在但尚未确认，连续确认不能越过它。
                break;
            } else if let Some(range) =
                self.truncations.iter().find(|r| r.contains(want)).copied()
            {
                // 明确裁剪：整个区间一次性折叠；清理理论上可能残留的标记。
                for off in range.from..=range.to {
                    self.acked.remove(&off);
                    self.pending.remove(&off);
                }
                self.frontier = range.to;
            } else {
                break;
            }
        }
    }

    /// 写入一条 B 侧事件。
    fn ingest(&mut self, event: MirrorEvent) -> IngestOutcome {
        match event {
            MirrorEvent::Mirrored {
                b_offset,
                source_offset,
                id,
                payload,
            } => {
                // 重开后的重放：来源位点已处于连续前沿之内，或此前已确认。
                if source_offset <= self.frontier || self.acked.contains(&source_offset) {
                    return IngestOutcome::Replayed {
                        b_offset,
                        source_offset,
                    };
                }
                // 稳定身份冲突检测。
                if let Some(&existing_off) = self.id_index.get(&id) {
                    if existing_off == source_offset {
                        return IngestOutcome::Duplicate {
                            b_offset,
                            source_offset,
                            id,
                        };
                    }
                    return IngestOutcome::Conflict {
                        id,
                        existing_source_offset: existing_off,
                        rejected_b_offset: b_offset,
                    };
                }
                // 同一来源位点出现不同身份：证据矛盾，隔离后到者。
                if let Some(existing) = self.entries.get(&source_offset) {
                    return IngestOutcome::Conflict {
                        id: existing.id.clone(),
                        existing_source_offset: source_offset,
                        rejected_b_offset: b_offset,
                    };
                }
                self.id_index.insert(id.clone(), source_offset);
                self.entries.insert(
                    source_offset,
                    MirroredEntry {
                        b_offset,
                        source_offset,
                        id,
                        payload,
                        delivered: false,
                        restored_inflight: false,
                    },
                );
                IngestOutcome::Accepted {
                    b_offset,
                    source_offset,
                }
            }
            MirrorEvent::Native { b_offset, payload } => {
                if self.native_acked.contains(&b_offset) {
                    return IngestOutcome::Replayed {
                        b_offset,
                        source_offset: b_offset,
                    };
                }
                if self.native_pending.contains_key(&b_offset)
                    || self.native_inflight.contains_key(&b_offset)
                {
                    return IngestOutcome::Duplicate {
                        b_offset,
                        source_offset: b_offset,
                        id: String::new(),
                    };
                }
                self.native_pending.insert(b_offset, payload);
                IngestOutcome::Accepted {
                    b_offset,
                    source_offset: b_offset,
                }
            }
        }
    }

    /// 按来源位点顺序投递可证明的消息，直到第一个缺口。
    ///
    /// `max_deliveries` 限制本批次投递总条数（含 B 侧原生消息）；
    /// 原生消息总是先按 B 位点顺序投递，随后处理来源位点流。
    fn drain(&mut self, partition: Partition, max_deliveries: usize) -> DrainReport {
        let mut delivered: Vec<Delivery> = Vec::new();
        let mut skipped_truncations: BTreeSet<TruncatedRange> = BTreeSet::new();
        let mut budget = max_deliveries;
        // 归集裁剪区间的起点：优先取上次 drain 结束时的前沿，
        // 这样两次 drain 之间由 ack/fold 隐式跨过的裁剪区间也能被报告。
        let report_from = self.last_drain_frontier.unwrap_or(self.frontier);

        // 1) B 侧原生消息：与来源前沿无关，按 B 到达位点顺序投递。
        //    既包含从未投递的消息，也包含投递后未确认（含重开恢复）的在途副本。
        let native_offsets: BTreeSet<Offset> = self
            .native_pending
            .keys()
            .chain(self.native_inflight.keys())
            .copied()
            .collect();
        for b_offset in native_offsets {
            if budget == 0 {
                break;
            }
            // 在途副本保留在 native_inflight 中直到确认；新副本则从 pending 移入。
            let payload = match self.native_pending.remove(&b_offset) {
                Some(payload) => {
                    self.native_inflight.insert(b_offset, payload.clone());
                    payload
                }
                None => self
                    .native_inflight
                    .get(&b_offset)
                    .expect("inflight native entry present")
                    .clone(),
            };
            delivered.push(Delivery {
                kind: DeliveryKind::Native,
                b_offset,
                source_offset: None,
                id: None,
                payload,
            });
            budget -= 1;
        }

        // 2) 来源位点流：严格 frontier+1 递进。
        let status = DrainStatus::AwaitingAck;
        let mut gap: Option<Gap> = None;

        // 位点空间已耗尽且所有位点都已折叠：没有任何待处理内容。
        if self.frontier == Offset::MAX {
            return self.finish_drain(
                partition,
                report_from,
                delivered,
                None,
                skipped_truncations,
                status,
            );
        }

        loop {
            // 折叠裁剪区间可能把前沿推到位点空间尽头，下一轮必须停止。
            if self.frontier == Offset::MAX {
                break;
            }
            let want = self.frontier.saturating_add(1);

            if let Some(entry) = self.entries.get(&want).cloned() {
                // 镜像实体是最强证据，优先于裁剪报告。
                if self.pending.contains(&want) {
                    if entry.restored_inflight {
                        // 快照恢复出来的在途消息：消费者已失联，按 at-least-once 重投一次，
                        // 重投后清除恢复标记，后续在本会话内不再重复投递。
                        if budget == 0 {
                            gap = self.peek_gap_behind_pending(want);
                            break;
                        }
                        delivered.push(Delivery {
                            kind: DeliveryKind::Mirrored,
                            b_offset: entry.b_offset,
                            source_offset: Some(entry.source_offset),
                            id: Some(entry.id),
                            payload: entry.payload,
                        });
                        let stored = self.entries.get_mut(&want).expect("entry present");
                        stored.restored_inflight = false;
                        self.pending.insert(want);
                        budget -= 1;
                        continue;
                    }
                    // 本会话已投递、等待确认：不能重复发。
                    // 越过在途消息向前看，把后续第一个缺口（若有）一并报告。
                    gap = self.peek_gap_behind_pending(want);
                    break;
                }
                if budget == 0 {
                    // 预算耗尽：want 处的实体本批投不出去，从 want 起向后探查缺口。
                    gap = self.peek_gap_behind_pending(want);
                    break;
                }
                delivered.push(Delivery {
                    kind: DeliveryKind::Mirrored,
                    b_offset: entry.b_offset,
                    source_offset: Some(entry.source_offset),
                    id: Some(entry.id),
                    payload: entry.payload,
                });
                let stored = self.entries.get_mut(&want).expect("entry present");
                stored.delivered = true;
                stored.restored_inflight = false;
                self.pending.insert(want);
                budget -= 1;
                continue;
            } else if self.is_truncated(want) {
                // 明确裁剪：逐位点折叠，直到区间结束或遇到下一个镜像实体。
                // 区间内若存在实体，它是更强证据，留给下一轮实体分支投递。
                let range = self
                    .truncations
                    .iter()
                    .find(|r| r.contains(want))
                    .copied()
                    .expect("truncation range present");
                let fold_start = want;
                let mut cursor = want;
                while cursor <= range.to && !self.entries.contains_key(&cursor) {
                    self.acked.remove(&cursor);
                    self.pending.remove(&cursor);
                    self.frontier = cursor;
                    if cursor == Offset::MAX {
                        break;
                    }
                    cursor = cursor.saturating_add(1);
                }
                // 本次实际折叠过的裁剪位点（fold_start..=frontier）。
                if self.frontier >= fold_start {
                    skipped_truncations.insert(TruncatedRange {
                        from: fold_start,
                        to: self.frontier,
                    });
                }
                continue;
            } else {
                // 第一个无法证明存在或裁剪的位点：界定待补区间后停止。
                let next_entry = self.entries.range(want..).next().map(|(off, _)| *off);
                let next_truncation = self
                    .truncations
                    .iter()
                    .filter(|r| r.from >= want)
                    .map(|r| r.from)
                    .min();
                let to = match (next_entry, next_truncation) {
                    (Some(a), Some(b)) => Some(a.min(b)),
                    (Some(a), None) => Some(a),
                    (None, Some(b)) => Some(b),
                    (None, None) => None,
                };
                gap = Some(Gap { from: want, to });
                return self.finish_drain(
                    partition,
                    report_from,
                    delivered,
                    gap,
                    skipped_truncations,
                    DrainStatus::Gap,
                );
            }
        }

        // 走到这里说明被在途消息或预算阻断：若此前/远处存在缺口则报 Gap，
        // 否则消费者当前只需要确认在途消息，状态为 AwaitingAck。
        let final_status = if gap.is_some() {
            DrainStatus::Gap
        } else {
            DrainStatus::AwaitingAck
        };
        self.finish_drain(
            partition,
            report_from,
            delivered,
            gap,
            skipped_truncations,
            final_status,
        )
    }

    /// B 侧确认一条已投递的镜像消息，只能推进连续来源前缀。
    fn ack_mirrored(
        &mut self,
        partition: Partition,
        source_offset: Offset,
    ) -> Result<AckOutcome, RecoveryError> {
        if source_offset <= self.frontier || self.acked.contains(&source_offset) {
            return Ok(AckOutcome::AlreadyConfirmed {
                source_offset,
                frontier: self.frontier,
            });
        }
        if !self.pending.contains(&source_offset) {
            return Err(RecoveryError::NotDelivered {
                partition,
                source_offset,
                frontier: self.frontier,
            });
        }
        self.pending.remove(&source_offset);
        self.acked.insert(source_offset);
        self.fold();
        Ok(AckOutcome::Advanced {
            frontier: self.frontier,
        })
    }

    /// 确认一条 B 侧原生消息。
    fn ack_native(
        &mut self,
        partition: Partition,
        b_offset: Offset,
    ) -> Result<AckOutcome, RecoveryError> {
        if self.native_acked.contains(&b_offset) {
            return Ok(AckOutcome::AlreadyConfirmed {
                source_offset: b_offset,
                frontier: self.frontier,
            });
        }
        // 仍在 native_pending 说明从未经 drain 投递：拒绝确认未读消息。
        if self.native_pending.contains_key(&b_offset) {
            return Err(RecoveryError::NotDelivered {
                partition,
                source_offset: b_offset,
                frontier: self.frontier,
            });
        }
        // 能走到这里且存在于 native_inflight，说明已投递在途；
        // 若两个集合都没有则属于未知位点，同样拒绝。
        if self.native_inflight.remove(&b_offset).is_none() {
            return Err(RecoveryError::NotDelivered {
                partition,
                source_offset: b_offset,
                frontier: self.frontier,
            });
        }
        self.native_acked.insert(b_offset);
        Ok(AckOutcome::Advanced {
            frontier: self.frontier,
        })
    }

    /// 切回 A 后的确认：必须严格紧接前沿，可一次性跨过已报告的裁剪区间。
    fn switch_back_ack(
        &mut self,
        partition: Partition,
        source_offset: Offset,
    ) -> Result<AckOutcome, RecoveryError> {
        if source_offset <= self.frontier || self.acked.contains(&source_offset) {
            return Ok(AckOutcome::AlreadyConfirmed {
                source_offset,
                frontier: self.frontier,
            });
        }
        let expected = self.frontier.saturating_add(1);
        if source_offset != expected {
            return Err(RecoveryError::OutOfOrderAck {
                partition,
                source_offset,
                expected,
            });
        }
        self.acked.insert(source_offset);
        self.fold();
        Ok(AckOutcome::Advanced {
            frontier: self.frontier,
        })
    }
}

// ---------------------------------------------------------------------------
// 管理器
// ---------------------------------------------------------------------------

/// 分区恢复状态的所有者（非线程安全包装见 [`ThreadSafeRecovery`]）。
#[derive(Debug, Clone, Default)]
pub struct RecoveryManager {
    partitions: BTreeMap<Partition, PartitionState>,
}

impl RecoveryManager {
    pub fn new() -> Self {
        Self::default()
    }

    /// 建立分区恢复状态。`initial_frontier` 是切换瞬间最后确认的 A 位点；
    /// 重复建立时若前沿不小于既有前沿则保持幂等，更小的前沿会被拒绝以避免回退。
    pub fn ensure_partition(
        &mut self,
        partition: Partition,
        initial_frontier: Offset,
    ) -> Result<(), RecoveryError> {
        match self.partitions.get(&partition) {
            Some(state) if initial_frontier < state.frontier => {
                Err(RecoveryError::OutOfOrderAck {
                    partition,
                    source_offset: initial_frontier,
                    expected: state.frontier,
                })
            }
            Some(_) => Ok(()),
            None => {
                self.partitions
                    .insert(partition, PartitionState::new(initial_frontier));
                Ok(())
            }
        }
    }

    /// 当前受管分区集合。
    pub fn partitions(&self) -> BTreeSet<Partition> {
        self.partitions.keys().copied().collect()
    }

    /// 分区连续确认前沿；未知分区返回 [`RecoveryError::UnknownPartition`]。
    pub fn frontier(&self, partition: Partition) -> Result<Offset, RecoveryError> {
        self.partitions
            .get(&partition)
            .map(PartitionState::frontier)
            .ok_or(RecoveryError::UnknownPartition(partition))
    }

    /// 分区已报告的裁剪区间。
    pub fn truncations(
        &self,
        partition: Partition,
    ) -> Result<&[TruncatedRange], RecoveryError> {
        self.partitions
            .get(&partition)
            .map(PartitionState::truncations)
            .ok_or(RecoveryError::UnknownPartition(partition))
    }

    /// 写入一条 B 侧事件（镜像或原生消息）。
    pub fn ingest(
        &mut self,
        partition: Partition,
        event: MirrorEvent,
    ) -> Result<IngestOutcome, RecoveryError> {
        self.partitions
            .get_mut(&partition)
            .map(|state| state.ingest(event))
            .ok_or(RecoveryError::UnknownPartition(partition))
    }

    /// 登记 A 报告的明确裁剪区间。
    pub fn report_truncation(
        &mut self,
        partition: Partition,
        range: TruncatedRange,
    ) -> Result<(), RecoveryError> {
        // 先校验区间本身，再查找分区，保证非法区间无论分区是否存在都报错。
        let range = TruncatedRange::new(range.from, range.to)?;
        self.partitions
            .get_mut(&partition)
            .map(|state| state.add_truncation(range))
            .ok_or(RecoveryError::UnknownPartition(partition))
    }

    /// 投递当前可证明的消息并给出待补区间。
    pub fn drain(
        &mut self,
        partition: Partition,
        max_deliveries: usize,
    ) -> Result<DrainReport, RecoveryError> {
        self.partitions
            .get_mut(&partition)
            .map(|state| state.drain(partition, max_deliveries))
            .ok_or(RecoveryError::UnknownPartition(partition))
    }

    /// B 侧确认镜像消息。
    pub fn ack_mirrored(
        &mut self,
        partition: Partition,
        source_offset: Offset,
    ) -> Result<AckOutcome, RecoveryError> {
        self.partitions
            .get_mut(&partition)
            .ok_or(RecoveryError::UnknownPartition(partition))
            .and_then(|state| state.ack_mirrored(partition, source_offset))
    }

    /// B 侧确认原生消息。
    pub fn ack_native(
        &mut self,
        partition: Partition,
        b_offset: Offset,
    ) -> Result<AckOutcome, RecoveryError> {
        self.partitions
            .get_mut(&partition)
            .ok_or(RecoveryError::UnknownPartition(partition))
            .and_then(|state| state.ack_native(partition, b_offset))
    }

    /// 切回 A 后确认 A 位点（必须连续）。
    pub fn switch_back_ack(
        &mut self,
        partition: Partition,
        source_offset: Offset,
    ) -> Result<AckOutcome, RecoveryError> {
        self.partitions
            .get_mut(&partition)
            .ok_or(RecoveryError::UnknownPartition(partition))
            .and_then(|state| state.switch_back_ack(partition, source_offset))
    }

    /// 导出可持久化快照（只含权威状态，其余来源镜像缓冲可由 B 日志重放重建）。
    pub fn snapshot(&self) -> RecoverySnapshot {
        let partitions = self
            .partitions
            .iter()
            .map(|(p, state)| {
                // 在途（已投递未确认）的镜像消息必须随快照持久化：
                // 重开后这些消息要按 at-least-once 重新投递给消费者。
                let mirrored_pending = state
                    .pending
                    .iter()
                    .filter_map(|off| state.entries.get(off))
                    .map(|entry| PendingMirrored {
                        b_offset: entry.b_offset,
                        source_offset: entry.source_offset,
                        id: entry.id.clone(),
                        payload: entry.payload.clone(),
                    })
                    .collect();
                (
                    *p,
                    PartitionSnapshot {
                        partition: *p,
                        frontier: state.frontier,
                        truncations: state.truncations.clone(),
                        mirrored_pending,
                        native_pending: state.native_pending.clone(),
                        native_inflight: state.native_inflight.clone(),
                        native_acked: state.native_acked.clone(),
                    },
                )
            })
            .collect();
        RecoverySnapshot { partitions }
    }

    /// 从快照恢复权威状态（非在途的来源镜像缓冲留空，等待 B 日志重放回填）。
    pub fn restore(snapshot: RecoverySnapshot) -> Self {
        let mut manager = Self::new();
        for (p, snap) in snapshot.partitions {
            let mut state = PartitionState::new(snap.frontier);
            state.truncations = snap.truncations;
            state.native_pending = snap.native_pending;
            state.native_inflight = snap.native_inflight;
            state.native_acked = snap.native_acked;
            for pending in snap.mirrored_pending {
                state.id_index.insert(pending.id.clone(), pending.source_offset);
                // 对新会话而言这些消息尚未投递：放进缓冲并打上恢复标记，
                // 不进入 pending；首次 drain 时重新投递后才进入 pending。
                state.entries.insert(
                    pending.source_offset,
                    MirroredEntry {
                        b_offset: pending.b_offset,
                        source_offset: pending.source_offset,
                        id: pending.id,
                        payload: pending.payload,
                        delivered: true,
                        restored_inflight: true,
                    },
                );
            }
            manager.partitions.insert(p, state);
        }
        manager
    }
}

/// 快照中持久化的一条在途镜像消息（已投递、尚未确认）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingMirrored {
    pub b_offset: Offset,
    pub source_offset: Offset,
    pub id: MessageId,
    pub payload: Vec<u8>,
}

/// 单个分区的可持久化状态快照。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PartitionSnapshot {
    pub partition: Partition,
    pub frontier: Offset,
    pub truncations: Vec<TruncatedRange>,
    /// 已投递但尚未确认的镜像消息（重开后需重投）。
    pub mirrored_pending: Vec<PendingMirrored>,
    /// 已接收但尚未投递的 B 侧原生消息（B 位点 -> 负载）。
    pub native_pending: BTreeMap<Offset, Vec<u8>>,
    /// 已投递但尚未确认的 B 侧原生消息（重开后需重投）。
    pub native_inflight: BTreeMap<Offset, Vec<u8>>,
    pub native_acked: BTreeSet<Offset>,
}

/// 全部分区的快照集合。
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct RecoverySnapshot {
    pub partitions: BTreeMap<Partition, PartitionSnapshot>,
}

// ---------------------------------------------------------------------------
// 线程安全包装（并发交错场景）
// ---------------------------------------------------------------------------

/// 基于 [`Mutex`] 的线程安全包装，支持多线程交错 ingest/drain/ack。
#[derive(Debug, Default)]
pub struct ThreadSafeRecovery {
    inner: Mutex<RecoveryManager>,
}

impl ThreadSafeRecovery {
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(RecoveryManager::new()),
        }
    }

    pub fn ensure_partition(
        &self,
        partition: Partition,
        initial_frontier: Offset,
    ) -> Result<(), RecoveryError> {
        self.inner
            .lock()
            .expect("recovery mutex poisoned")
            .ensure_partition(partition, initial_frontier)
    }

    pub fn ingest(
        &self,
        partition: Partition,
        event: MirrorEvent,
    ) -> Result<IngestOutcome, RecoveryError> {
        self.inner
            .lock()
            .expect("recovery mutex poisoned")
            .ingest(partition, event)
    }

    pub fn report_truncation(
        &self,
        partition: Partition,
        range: TruncatedRange,
    ) -> Result<(), RecoveryError> {
        self.inner
            .lock()
            .expect("recovery mutex poisoned")
            .report_truncation(partition, range)
    }

    pub fn drain(
        &self,
        partition: Partition,
        max_deliveries: usize,
    ) -> Result<DrainReport, RecoveryError> {
        self.inner
            .lock()
            .expect("recovery mutex poisoned")
            .drain(partition, max_deliveries)
    }

    pub fn ack_mirrored(
        &self,
        partition: Partition,
        source_offset: Offset,
    ) -> Result<AckOutcome, RecoveryError> {
        self.inner
            .lock()
            .expect("recovery mutex poisoned")
            .ack_mirrored(partition, source_offset)
    }

    pub fn ack_native(
        &self,
        partition: Partition,
        b_offset: Offset,
    ) -> Result<AckOutcome, RecoveryError> {
        self.inner
            .lock()
            .expect("recovery mutex poisoned")
            .ack_native(partition, b_offset)
    }

    pub fn switch_back_ack(
        &self,
        partition: Partition,
        source_offset: Offset,
    ) -> Result<AckOutcome, RecoveryError> {
        self.inner
            .lock()
            .expect("recovery mutex poisoned")
            .switch_back_ack(partition, source_offset)
    }

    pub fn frontier(&self, partition: Partition) -> Result<Offset, RecoveryError> {
        self.inner
            .lock()
            .expect("recovery mutex poisoned")
            .frontier(partition)
    }

    pub fn snapshot(&self) -> RecoverySnapshot {
        self.inner.lock().expect("recovery mutex poisoned").snapshot()
    }
}
