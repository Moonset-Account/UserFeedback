//! 可靠传输发送端的重传状态模块。
//!
//! # 建模要点
//!
//! - 握手与应用数据使用彼此独立的包号空间（[`SpaceId`]），两个空间可以
//!   同时使用相同的包号而互不干扰；每个空间有独立的丢失时限与计时。
//! - 流数据帧用流号与半开字节区间 [`StreamRange`] 标识；同一段字节区间
//!   允许出现在多个（重传）承载包中。
//! - 包在“在途中”期间只占用一次在途字节/包数：发送时增加，离开在途
//!   （确认或判丢）时只减少一次；判丢后迟到的确认不再重复扣减。
//! - 流字节在任意一个承载包被确认后即视为完成，后续其他承载包的确认、
//!   判丢都不会改变这一事实。
//! - 旧包判丢后又收到确认（迟到确认）时，撤去该包各字节区间上“尚未
//!   发送”的重传计划，同时保持其他包的在途计数不变。
//! - 关闭一个包号空间只撤销该空间自身的包与计时；仍需在存续空间完成
//!   的数据由调用方在关闭事件中通过 `still_needed` 明确列出。

mod interval;

pub use interval::{InvalidRange, IntervalSet, StreamRange};

/// 包号空间：握手与应用数据互不相关，包号各自编号。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SpaceId {
    /// 握手包号空间。
    Handshake,
    /// 应用数据包号空间。
    AppData,
}

impl SpaceId {
    /// 全部空间的规范顺序，可用于遍历计费结果。
    pub const ALL: [SpaceId; 2] = [SpaceId::Handshake, SpaceId::AppData];

    const fn index(self) -> usize {
        match self {
            SpaceId::Handshake => 0,
            SpaceId::AppData => 1,
        }
    }
}

/// 一条发送记录：某个包在某个空间以某包号、某时钟时刻发出。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SentPacket {
    /// 所属包号空间。
    pub space: SpaceId,
    /// 空间内的包号。
    pub packet_number: u64,
    /// 发送时的模拟时钟读数。
    pub time_sent: u64,
    /// 包占用的在途字节（计入在途计费；0 表示纯 ACK 之类的空包）。
    pub size: u64,
    /// 包承载的流数据帧区间，允许为空、允许跨多条流。
    pub frames: Vec<StreamRange>,
}

/// 按空间给出的确认区间集合，区间为含两端的包号区间。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AckRanges {
    /// 确认到的包号闭区间 `[smallest, largest]` 列表。
    pub ranges: Vec<(u64, u64)>,
}

impl AckRanges {
    /// 构造确认区间，自动校验并归并重叠/相邻区间（输入顺序任意）。
    pub fn new(mut ranges: Vec<(u64, u64)>) -> Result<Self, SenderError> {
        for &(start, end) in &ranges {
            if start > end {
                return Err(SenderError::InvalidAckRange { start, end });
            }
        }
        ranges.sort_unstable_by_key(|&(start, _)| start);
        let mut normalized: Vec<(u64, u64)> = Vec::new();
        for (start, end) in ranges {
            if let Some(last) = normalized.last_mut() {
                if start <= last.1.saturating_add(1) {
                    last.1 = last.1.max(end);
                    continue;
                }
            }
            normalized.push((start, end));
        }
        Ok(Self { ranges: normalized })
    }

    /// 与 `new` 相同，但便于在测试中直接书写。
    pub fn from_pairs(pairs: &[(u64, u64)]) -> Result<Self, SenderError> {
        Self::new(pairs.to_vec())
    }

    fn contains(&self, packet_number: u64) -> bool {
        self.ranges
            .iter()
            .any(|&(start, end)| start <= packet_number && packet_number <= end)
    }
}

/// 状态机操作失败的原因。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SenderError {
    /// 起始偏移大于结束偏移的流区间。
    InvalidStreamRange {
        /// 起始偏移。
        start: u64,
        /// 结束偏移。
        end: u64,
    },
    /// 确认区间下界大于上界。
    InvalidAckRange {
        /// 区间下界。
        start: u64,
        /// 区间上界。
        end: u64,
    },
    /// 同一空间内出现重复包号。
    DuplicatePacket {
        /// 发生冲突的空间。
        space: SpaceId,
        /// 冲突包号。
        packet_number: u64,
    },
    /// 目标空间已经关闭。
    SpaceClosed {
        /// 被关闭的空间。
        space: SpaceId,
    },
    /// 模拟时钟回退（新事件时刻早于已观测时刻）。
    TimeWentBackwards {
        /// 已观测到的时刻。
        previous: u64,
        /// 本次事件给出的时刻。
        given: u64,
    },
}

/// 单个包在状态机中的生命周期状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PktStatus {
    /// 仍在途中，占用在途字节与包数。
    InFlight,
    /// 已被判丢并离开在途，其流字节进入重传计划。
    Lost,
    /// 在途中被确认并离开在途。
    Acked,
    /// 判丢之后才收到确认（迟到确认），不重复扣减在途。
    AckedAfterLoss,
}

#[derive(Debug, Clone)]
struct PacketEntry {
    size: u64,
    time_sent: u64,
    frames: Vec<StreamRange>,
    status: PktStatus,
}

#[derive(Debug, Clone)]
struct SpaceState {
    closed: bool,
    /// 本空间丢失时限：`time_sent + loss_time` 之后仍在途即判丢。
    loss_time: u64,
    /// 包号 -> 发送记录（两个空间各自独立，允许同号）。
    packets: std::collections::BTreeMap<u64, PacketEntry>,
    /// 当前在途字节数。
    in_flight_bytes: u64,
    /// 当前在途包数。
    in_flight_packets: u64,
    /// 已发送字节累计（含重传包）。
    sent_bytes: u64,
    /// 已发送包数累计（含重传包）。
    sent_packets: u64,
    /// 已确认字节累计（在途确认计入；迟到确认不重复计费）。
    acked_bytes: u64,
    /// 判丢包数累计。
    lost_packets: u64,
    /// 在途确认的包数。
    acked_packets: u64,
    /// 判丢后才收到确认的包数。
    spurious_ack_packets: u64,
    /// 本空间尚未发出的重传字节计划（按流区间）。
    pending: IntervalSet,
}

impl SpaceState {
    fn new(loss_time: u64) -> Self {
        Self {
            closed: false,
            loss_time,
            packets: std::collections::BTreeMap::new(),
            in_flight_bytes: 0,
            in_flight_packets: 0,
            sent_bytes: 0,
            sent_packets: 0,
            acked_bytes: 0,
            lost_packets: 0,
            acked_packets: 0,
            spurious_ack_packets: 0,
            pending: IntervalSet::new(),
        }
    }
}

/// 按空间输出的计费快照。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SpaceAccount {
    /// 对应空间。
    pub space: SpaceId,
    /// 空间是否已关闭。
    pub closed: bool,
    /// 配置的丢失时限。
    pub loss_time: u64,
    /// 当前在途字节（离开在途只扣一次）。
    pub in_flight_bytes: u64,
    /// 当前在途包数。
    pub in_flight_packets: u64,
    /// 累计发送字节（含重传）。
    pub sent_bytes: u64,
    /// 累计发送包数（含重传）。
    pub sent_packets: u64,
    /// 累计确认字节（仅在途确认计入）。
    pub acked_bytes: u64,
    /// 在途确认包数。
    pub acked_packets: u64,
    /// 判丢包数。
    pub lost_packets: u64,
    /// 判丢后迟到确认的包数。
    pub spurious_ack_packets: u64,
    /// 本空间尚未发出的重传字节数。
    pub pending_bytes: u64,
}

/// 发送端重传状态机。
#[derive(Debug, Clone)]
pub struct Sender {
    spaces: [SpaceState; 2],
    now: u64,
    /// 所有已确认承载过的流字节并集：任一包确认即完成。
    completed: IntervalSet,
}

impl Sender {
    /// 建立状态机，并为两个空间分别设置丢失时限。
    pub fn new(handshake_loss_time: u64, app_data_loss_time: u64) -> Self {
        Self {
            spaces: [
                SpaceState::new(handshake_loss_time),
                SpaceState::new(app_data_loss_time),
            ],
            now: 0,
            completed: IntervalSet::new(),
        }
    }

    /// 调整某空间的丢失时限，不改变已观测时钟。
    pub fn set_loss_time(&mut self, space: SpaceId, loss_time: u64) {
        self.spaces[space.index()].loss_time = loss_time;
    }

    /// 当前模拟时钟读数。
    pub fn now(&self) -> u64 {
        self.now
    }

    fn check_time(&mut self, t: u64) -> Result<(), SenderError> {
        if t < self.now {
            return Err(SenderError::TimeWentBackwards {
                previous: self.now,
                given: t,
            });
        }
        self.now = t;
        Ok(())
    }

    /// 录入一条发送记录。
    ///
    /// 该包计入在途与累计发送；若它承载的字节区间此前已在本空间的
    /// 待发重传计划中，则从计划中扣除已发出的部分（重传包发出）。
    pub fn on_sent(&mut self, packet: SentPacket) -> Result<(), SenderError> {
        for frame in &packet.frames {
            validate_frame(*frame)?;
        }
        // 先做全部校验：被拒绝的事件不推进时钟、不改变任何状态。
        let state = &self.spaces[packet.space.index()];
        if state.closed {
            return Err(SenderError::SpaceClosed { space: packet.space });
        }
        if state.packets.contains_key(&packet.packet_number) {
            return Err(SenderError::DuplicatePacket {
                space: packet.space,
                packet_number: packet.packet_number,
            });
        }
        self.check_time(packet.time_sent)?;
        let state = &mut self.spaces[packet.space.index()];
        for frame in &packet.frames {
            state.pending.remove(*frame);
        }
        state.packets.insert(
            packet.packet_number,
            PacketEntry {
                size: packet.size,
                time_sent: packet.time_sent,
                frames: packet.frames,
                status: PktStatus::InFlight,
            },
        );
        state.in_flight_bytes = state.in_flight_bytes.saturating_add(packet.size);
        state.in_flight_packets = state.in_flight_packets.saturating_add(1);
        state.sent_bytes = state.sent_bytes.saturating_add(packet.size);
        state.sent_packets = state.sent_packets.saturating_add(1);
        Ok(())
    }

    /// 处理某空间的确认事件。
    ///
    /// 先判丢再确认与先确认再判丢对最终状态等价：在途包被确认即离开
    /// 在途；已判丢的包迟到确认时，其仍未发送的同区间重传计划被撤去，
    /// 但在途计数不发生变化，其他包的在途计数同样不受影响。
    pub fn on_ack(
        &mut self,
        space: SpaceId,
        ack: &AckRanges,
        at_time: u64,
    ) -> Result<(), SenderError> {
        // 已关闭空间的确认静默忽略，且不推进时钟。
        if self.spaces[space.index()].closed {
            return Ok(());
        }
        self.check_time(at_time)?;

        let acked_numbers: Vec<u64> = self.spaces[space.index()]
            .packets
            .iter()
            .filter(|(number, entry)| ack.contains(**number) && entry.status == PktStatus::InFlight)
            .map(|(number, _)| *number)
            .collect();

        let mut newly_completed = IntervalSet::new();
        let mut spurious = Vec::new();
        {
            let state = &mut self.spaces[space.index()];
            for number in acked_numbers {
                let entry = state.packets.get_mut(&number).expect("entry exists");
                entry.status = PktStatus::Acked;
                state.in_flight_bytes = state.in_flight_bytes.saturating_sub(entry.size);
                state.in_flight_packets = state.in_flight_packets.saturating_sub(1);
                state.acked_bytes = state.acked_bytes.saturating_add(entry.size);
                state.acked_packets = state.acked_packets.saturating_add(1);
                for frame in &entry.frames {
                    newly_completed.insert(*frame);
                }
            }

            // 迟到确认：包此前已判丢并离开在途，这里只撤重传计划，不动在途。
            for (&number, entry) in state.packets.iter_mut() {
                if entry.status == PktStatus::Lost && ack.contains(number) {
                    entry.status = PktStatus::AckedAfterLoss;
                    state.spurious_ack_packets = state.spurious_ack_packets.saturating_add(1);
                    for frame in &entry.frames {
                        spurious.push(*frame);
                    }
                }
            }
        }

        // 任一承载包确认，流字节即完成；完成字节从所有空间的待发计划撤去。
        for frame in newly_completed.iter().copied() {
            self.completed.insert(frame);
        }
        for frame in &spurious {
            self.completed.insert(*frame);
        }
        for state in &mut self.spaces {
            for frame in newly_completed.iter().copied() {
                state.pending.remove(frame);
            }
            for frame in &spurious {
                state.pending.remove(*frame);
            }
        }

        self.detect_loss(at_time);
        Ok(())
    }

    /// 推进模拟时钟并对超过丢失时限的在途包判丢。
    pub fn on_timeout(&mut self, at_time: u64) -> Result<(), SenderError> {
        self.check_time(at_time)?;
        self.detect_loss(at_time);
        Ok(())
    }

    /// [`Sender::on_timeout`] 的语义别名：仅推进时钟。
    pub fn advance(&mut self, at_time: u64) -> Result<(), SenderError> {
        self.on_timeout(at_time)
    }

    /// 判丢：在途时间超过所属空间时限的包离开在途，其未完成流字节
    /// 进入该空间的待发重传计划。
    fn detect_loss(&mut self, at_time: u64) {
        let mut lost_frames: Vec<(usize, StreamRange)> = Vec::new();
        for (idx, state) in self.spaces.iter_mut().enumerate() {
            let deadline_base = state.loss_time;
            let mut expired = Vec::new();
            for (&number, entry) in state.packets.iter_mut() {
                if entry.status != PktStatus::InFlight {
                    continue;
                }
                let deadline = entry.time_sent.saturating_add(deadline_base);
                if at_time >= deadline {
                    entry.status = PktStatus::Lost;
                    state.in_flight_bytes = state.in_flight_bytes.saturating_sub(entry.size);
                    state.in_flight_packets = state.in_flight_packets.saturating_sub(1);
                    state.lost_packets = state.lost_packets.saturating_add(1);
                    expired.push(number);
                }
            }
            for number in expired {
                let entry = state.packets.get(&number).expect("entry exists");
                for frame in &entry.frames {
                    lost_frames.push((idx, *frame));
                }
            }
        }
        for (idx, frame) in lost_frames {
            // 已由其他承载包确认的字节不再需要重传。
            if !self.completed.covers(&frame) {
                self.spaces[idx].pending.insert(frame);
            }
        }
    }

    /// 关闭一个包号空间。
    ///
    /// 只撤销该空间自身的包、在途占用与计时；另一空间完全不动。
    /// `still_needed` 由调用方明确列出：这些仍需完成的流字节会转入
    /// 存续空间的待发计划（已经完成的部分会自动剔除）。
    pub fn discard_space(
        &mut self,
        space: SpaceId,
        at_time: u64,
        still_needed: &[StreamRange],
    ) -> Result<(), SenderError> {
        for frame in still_needed {
            validate_frame(*frame)?;
        }
        if self.spaces[space.index()].closed {
            return Err(SenderError::SpaceClosed { space });
        }
        self.check_time(at_time)?;
        let idx = space.index();
        self.spaces[idx] = SpaceState {
            closed: true,
            ..SpaceState::new(self.spaces[idx].loss_time)
        };

        let survivors: Vec<usize> = self
            .spaces
            .iter()
            .enumerate()
            .filter(|(i, state)| *i != idx && !state.closed)
            .map(|(i, _)| i)
            .collect();
        if let Some(target) = survivors.first().copied() {
            for frame in still_needed {
                if !self.completed.covers(frame) {
                    self.spaces[target].pending.insert(*frame);
                }
            }
        }
        Ok(())
    }

    /// 下次需要处理的计时时刻：所有在途包中最早的丢失判定时刻；
    /// 没有在途包时返回 `None`（关闭的空间不产生计时）。
    pub fn next_timeout(&self) -> Option<u64> {
        let mut earliest: Option<u64> = None;
        for state in &self.spaces {
            if state.closed {
                continue;
            }
            for entry in state.packets.values() {
                if entry.status == PktStatus::InFlight {
                    let deadline = entry.time_sent.saturating_add(state.loss_time);
                    earliest = Some(earliest.map_or(deadline, |t| t.min(deadline)));
                }
            }
        }
        earliest
    }

    /// 某空间当前待发（尚未发出的重传）流区间，已完成字节自动剔除。
    pub fn pending_ranges(&self, space: SpaceId) -> Vec<StreamRange> {
        let state = &self.spaces[space.index()];
        let mut effective = state.pending.clone();
        for frame in state.pending.iter().copied() {
            if self.completed.covers(&frame) {
                effective.remove(frame);
            }
        }
        effective.iter().copied().collect()
    }

    /// 全部存续空间待发区间的并集。
    pub fn all_pending_ranges(&self) -> Vec<StreamRange> {
        let mut union = IntervalSet::new();
        for space in SpaceId::ALL {
            if !self.spaces[space.index()].closed {
                union.union_with(&self.spaces[space.index()].pending);
            }
        }
        let mut effective = union.clone();
        for frame in union.iter().copied() {
            if self.completed.covers(&frame) {
                effective.remove(frame);
            }
        }
        effective.iter().copied().collect()
    }

    /// 已完成的流字节集合（任一承载包确认即计入）。
    pub fn completed_ranges(&self) -> Vec<StreamRange> {
        self.completed.iter().copied().collect()
    }

    /// 按空间输出计费快照，顺序与 [`SpaceId::ALL`] 一致。
    pub fn accounts(&self) -> [SpaceAccount; 2] {
        let mut out: Vec<SpaceAccount> = self
            .spaces
            .iter()
            .zip(SpaceId::ALL)
            .map(|(state, space)| SpaceAccount {
                space,
                closed: state.closed,
                loss_time: state.loss_time,
                in_flight_bytes: state.in_flight_bytes,
                in_flight_packets: state.in_flight_packets,
                sent_bytes: state.sent_bytes,
                sent_packets: state.sent_packets,
                acked_bytes: state.acked_bytes,
                acked_packets: state.acked_packets,
                lost_packets: state.lost_packets,
                spurious_ack_packets: state.spurious_ack_packets,
                pending_bytes: state.pending.total_bytes(),
            })
            .collect();
        let app = out.pop().expect("app account");
        let hs = out.pop().expect("handshake account");
        [hs, app]
    }

    /// 查询单个包当前的生命周期状态（主要用于测试与可观测性）。
    pub fn packet_status(&self, space: SpaceId, packet_number: u64) -> Option<PacketView> {
        self.spaces[space.index()]
            .packets
            .get(&packet_number)
            .map(|entry| PacketView {
                space,
                packet_number,
                time_sent: entry.time_sent,
                size: entry.size,
                frames: entry.frames.clone(),
                status: entry.status,
            })
    }

    /// 以事件流方式驱动状态机，便于按时间顺序组织交错场景。
    pub fn handle(&mut self, event: Event) -> Result<(), SenderError> {
        match event {
            Event::Sent(packet) => self.on_sent(packet),
            Event::Ack(space, ack, at_time) => self.on_ack(space, &ack, at_time),
            Event::Timeout(at_time) => self.on_timeout(at_time),
            Event::DiscardSpace {
                space,
                at_time,
                still_needed,
            } => self.discard_space(space, at_time, &still_needed),
        }
    }
}

/// 单个包的可观测状态视图。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PacketView {
    /// 所属空间。
    pub space: SpaceId,
    /// 包号。
    pub packet_number: u64,
    /// 发送时刻。
    pub time_sent: u64,
    /// 在途字节。
    pub size: u64,
    /// 承载的流区间。
    pub frames: Vec<StreamRange>,
    /// 生命周期状态。
    pub status: PktStatus,
}

/// 驱动状态机的输入事件。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    /// 录入一条发送记录。
    Sent(SentPacket),
    /// 某空间在某时刻收到确认。
    Ack(SpaceId, AckRanges, u64),
    /// 时钟推进到某时刻（触发判丢）。
    Timeout(u64),
    /// 关闭某空间；`still_needed` 为仍需在存续空间完成的数据。
    DiscardSpace {
        /// 被关闭的空间。
        space: SpaceId,
        /// 关闭时刻。
        at_time: u64,
        /// 调用方明确列出的仍需完成的流区间。
        still_needed: Vec<StreamRange>,
    },
}

fn validate_frame(frame: StreamRange) -> Result<(), SenderError> {
    if frame.start > frame.end {
        return Err(SenderError::InvalidStreamRange {
            start: frame.start,
            end: frame.end,
        });
    }
    Ok(())
}

impl std::fmt::Display for SenderError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SenderError::InvalidStreamRange { start, end } => {
                write!(f, "invalid stream range: start {start} > end {end}")
            }
            SenderError::InvalidAckRange { start, end } => {
                write!(f, "invalid ack range: start {start} > end {end}")
            }
            SenderError::DuplicatePacket {
                space,
                packet_number,
            } => write!(
                f,
                "duplicate packet number {packet_number} in space {space:?}"
            ),
            SenderError::SpaceClosed { space } => {
                write!(f, "space {space:?} is closed")
            }
            SenderError::TimeWentBackwards { previous, given } => write!(
                f,
                "simulated clock went backwards: {given} < previous {previous}"
            ),
        }
    }
}

impl std::error::Error for SenderError {}
