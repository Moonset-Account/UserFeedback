//! 可注入断电的持久内存介质后端。
//!
//! # 介质语义（题目约定）
//! - 介质按缓存行（64 字节 = 8 个字）组织；字大小对齐写是原子的，
//!   非对齐写或跨行写会直接 panic（协议不会发起这种写）。
//! - 普通写只修改**易失镜像**；只有显式 [`PersistentMedia::flush_line`]
//!   才会把当时的整行快照加入“待持久集合”，同一行按发起次序排队。
//! - [`PersistentMedia::barrier`] 完成后，可保证此前刷出的全部行像
//!   都已持久化；不同缓存行之间可以按任意顺序落盘。
//! - 断电时，未刷出的易失写全部丢失；每个待持久行像可以落盘、也可以
//!   不落盘，但同一行只能以前缀方式生效（按发起次序，不能跳号）。
//! - 完整事件日志会在内存中保留，用于枚举合法断电像、以及从失败像
//!   反推导致它的落盘顺序。

use std::fmt;

/// 每个缓存行包含的 64 位字数。
pub const LINE_WORDS: usize = 8;
/// 缓存行字节数。
pub const LINE_BYTES: usize = LINE_WORDS * 8;
/// 机器字字节数。
pub const WORD_BYTES: usize = 8;

/// 一整条缓存行的不可变快照（行像）。
#[derive(Clone, Copy, PartialEq, Eq)]
pub struct LineImage(pub [u64; LINE_WORDS]);

impl LineImage {
    /// 全零行像（冷介质的初始内容）。
    pub const ZERO: LineImage = LineImage([0u64; LINE_WORDS]);

    /// 读取行内第 `word` 个字。
    #[inline]
    pub fn word(&self, word: usize) -> u64 {
        self.0[word]
    }
}

impl fmt::Debug for LineImage {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_tuple("LineImage").field(&self.0).finish()
    }
}

/// 一次显式刷行事件：把 `line` 在当时的完整行像登记为待持久。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct FlushEvent {
    /// 全局事件序号（与 [`PersistentMedia::log`] 中的位置一致）。
    pub seq: usize,
    /// 目标缓存行号。
    pub line: usize,
    /// 刷出时的行像。
    pub image: LineImage,
}

/// 介质生命周期内的可持久化事件。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Event {
    /// 一次刷行（进入待持久集合）。
    Flush(FlushEvent),
    /// 一次屏障；完成后此前所有刷出都保证持久。
    Barrier,
}

impl Event {
    /// 若是刷行事件，返回其内容。
    pub fn as_flush(&self) -> Option<FlushEvent> {
        match self {
            Event::Flush(f) => Some(*f),
            Event::Barrier => None,
        }
    }
}

/// 一次断电切割。
///
/// `chosen[line] = k` 表示：在 `region` 号屏障区间内，该行第 1..=k 个
/// 待持久行像已经落盘（`k = 0` 表示该区间内该行一个都没落）。
/// 同一行的选择天然是前缀；不同行互相独立。
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct CrashCut {
    /// 断电发生在第 `region` 个屏障区间（0 = 第一次屏障之前）。
    pub region: usize,
    /// 每个行在该区间内已落盘的待持久行像个数。
    pub chosen: Vec<usize>,
}

/// 重建落盘顺序时的行间仲裁策略（不同行可任意先后）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OrderStrategy {
    /// 行号小的行先落盘。
    LineAscending,
    /// 行号大的行先落盘。
    LineDescending,
}

/// 从失败像反推落盘顺序时的错误。
#[derive(Clone, PartialEq, Eq, Debug)]
pub enum ReconstructError {
    /// 目标行像在该行任何已刷出的快照中都不存在。
    NoMatchingImage { line: usize },
    /// 目标行像来自第 `found_region` 区间，但给定区间是 `given_region`。
    OutsideRegion {
        line: usize,
        given_region: usize,
        found_region: usize,
    },
}

/// 一次反推结果。
#[derive(Clone, Debug)]
pub struct Reconstruction {
    /// 目标断电切割。
    pub cut: CrashCut,
    /// 满足约束的一种合法全局落盘顺序（行号, 行像）。
    pub replay_order: Vec<(usize, LineImage)>,
    /// 至少需要完成的屏障数：在第 `min_barriers` 道屏障之后这些落盘才被允许持久。
    pub min_barriers: usize,
}

/// 持久内存介质（带易失缓存与断电注入）。
#[derive(Debug)]
pub struct PersistentMedia {
    line_count: usize,
    /// 易失镜像：所有写先到这里。
    volatile: Vec<LineImage>,
    /// 持久镜像：仅由断电恢复或屏障推进改变。
    persistent: Vec<LineImage>,
    /// 完整事件日志（跨断电保留，介质本身不掉电时只在内存里）。
    log: Vec<Event>,
    /// 是否处于上电状态；断电后为 false，重新上电恢复。
    powered: bool,
}

impl PersistentMedia {
    /// 创建一块全新的零介质并上电。
    pub fn cold_boot(line_count: usize) -> Self {
        assert!(line_count > 0, "media must contain at least one line");
        Self {
            line_count,
            volatile: vec![LineImage::ZERO; line_count],
            persistent: vec![LineImage::ZERO; line_count],
            log: Vec::new(),
            powered: true,
        }
    }

    /// 介质的缓存行总数。
    pub fn line_count(&self) -> usize {
        self.line_count
    }

    /// 当前是否处于上电状态。
    pub fn is_powered(&self) -> bool {
        self.powered
    }

    /// 完整事件日志（刷行与屏障，按发起次序）。
    pub fn log(&self) -> &[Event] {
        &self.log
    }

    /// 当前日志里的屏障区间数量（= 屏障数 + 1）。
    pub fn region_count(&self) -> usize {
        1 + self.log.iter().filter(|e| **e == Event::Barrier).count()
    }

    /// 复刻一份完全相同的介质（含事件日志），用于对同一历史分别注入
    /// 不同断电像，避免一次演练的崩溃恢复污染后续枚举。
    pub fn fork(&self) -> PersistentMedia {
        PersistentMedia {
            line_count: self.line_count,
            volatile: self.volatile.clone(),
            persistent: self.persistent.clone(),
            log: self.log.clone(),
            powered: self.powered,
        }
    }

    fn require_powered(&self) {
        assert!(self.powered, "media has lost power; remount before use");
    }

    fn check_line(&self, line: usize) {
        assert!(line < self.line_count, "line {line} out of range");
    }

    /// 把全局字下标拆成 (缓存行, 行内字)。
    #[inline]
    pub fn locate_word(global_word: usize) -> (usize, usize) {
        (global_word / LINE_WORDS, global_word % LINE_WORDS)
    }

    /// 原子写一个对齐字。仅改易失镜像，不会落盘。
    pub fn write_word(&mut self, global_word: usize, value: u64) {
        self.require_powered();
        assert!(
            global_word < self.line_count * LINE_WORDS,
            "word {global_word} out of range"
        );
        let (line, within) = Self::locate_word(global_word);
        self.volatile[line].0[within] = value;
    }

    /// 读取易失镜像中的一个对齐字。
    pub fn read_word(&self, global_word: usize) -> u64 {
        let (line, within) = Self::locate_word(global_word);
        self.check_line(line);
        self.volatile[line].0[within]
    }

    /// 读一整行的易失镜像。
    pub fn line_image(&self, line: usize) -> LineImage {
        self.check_line(line);
        self.volatile[line]
    }

    /// 显式刷出一行：把**当时**的行像加入待持久集合。
    /// 同一行按发起次序排队，顺序记录在事件日志里。
    pub fn flush_line(&mut self, line: usize) {
        self.require_powered();
        self.check_line(line);
        let image = self.volatile[line];
        self.log.push(Event::Flush(FlushEvent {
            seq: self.log.len(),
            line,
            image,
        }));
    }

    /// 屏障：完成后此前刷出的所有行像保证已持久化。
    pub fn barrier(&mut self) {
        self.require_powered();
        self.log.push(Event::Barrier);
        // 屏障完成：把此前全部待持久刷出推进到持久镜像。
        for ev in &self.log {
            if let Event::Flush(f) = ev {
                self.persistent[f.line] = f.image;
            }
        }
    }

    /// 把事件日志按屏障切分成区间；区间 r 内是第 r 道屏障之后、
    /// 第 r+1 道屏障之前发起的全部刷行（区间 0 在第一道屏障之前）。
    fn group_regions(log: &[Event]) -> Vec<Vec<FlushEvent>> {
        let mut regions: Vec<Vec<FlushEvent>> = vec![Vec::new()];
        for ev in log {
            match ev {
                Event::Flush(f) => regions
                    .last_mut()
                    .expect("regions always contains at least one entry")
                    .push(*f),
                Event::Barrier => regions.push(Vec::new()),
            }
        }
        regions
    }

    /// 枚举当前日志下、每两个屏障之间全部**合法**断电像。
    ///
    /// 返回每个屏障区间的一个枚举块：区间基线像（上一道屏障保证的
    /// 持久内容）加上该区间内待持久行像的所有合法前缀组合。
    /// 不同行的选择互相独立，同行严格按发起次序取前缀。
    pub fn legal_cuts(&self) -> Vec<CrashRegion> {
        let regions = Self::group_regions(&self.log);
        let line_count = self.line_count;

        // durable[line]：进入当前区间时，上一道屏障保证的行像。
        let mut durable = vec![LineImage::ZERO; line_count];
        let mut out = Vec::with_capacity(regions.len());

        for (region_id, flushes) in regions.iter().enumerate() {
            // 该区间内每行的待持久行像队列（按发起次序）。
            let mut pending: Vec<Vec<LineImage>> = vec![Vec::new(); line_count];
            for f in flushes {
                pending[f.line].push(f.image);
            }

            let mut images: Vec<CrashImage> = Vec::new();
            let mut chosen = vec![0usize; line_count];
            loop {
                let mut lines = durable.clone();
                for (line, choice) in chosen.iter().enumerate() {
                    if *choice > 0 {
                        lines[line] = pending[line][choice - 1];
                    }
                }
                images.push(CrashImage {
                    cut: CrashCut {
                        region: region_id,
                        chosen: chosen.clone(),
                    },
                    lines,
                });

                // 对选择向量做混合进制 +1（每位进制 = 该行待持久个数 + 1）。
                let mut digit = 0usize;
                loop {
                    if chosen[digit] < pending[digit].len() {
                        chosen[digit] += 1;
                        break;
                    }
                    chosen[digit] = 0;
                    digit += 1;
                    if digit == line_count {
                        break;
                    }
                }
                if digit == line_count {
                    break;
                }
            }

            out.push(CrashRegion {
                region: region_id,
                baseline: durable.clone(),
                pending,
                images,
            });

            // 区间结束时的屏障（若存在）会把该区间全部刷出推进为持久。
            for f in flushes {
                durable[f.line] = f.image;
            }
        }
        out
    }

    /// 注入断电并按给定合法切割恢复到重新上电状态。
    ///
    /// 易失镜像被丢弃，重建为持久内容；只有切割允许的待持久行像
    /// （同行前缀、不同行独立）会出现在新的镜像里。
    /// 事件日志保留，供 [`Self::reconstruct`] 反推使用。
    pub fn crash(&mut self, cut: &CrashCut) {
        assert!(self.powered, "already powered off");
        let regions = Self::group_regions(&self.log);
        assert!(
            cut.region < regions.len(),
            "crash region {} out of range (have {})",
            cut.region,
            regions.len()
        );
        assert_eq!(cut.chosen.len(), self.line_count, "bad cut width");

        // 前缀合法性校验。
        for (line, choice) in cut.chosen.iter().enumerate() {
            let pending_len = regions[cut.region]
                .iter()
                .filter(|f| f.line == line)
                .count();
            assert!(*choice <= pending_len, "cut for line {line} is not a prefix");
        }

        let mut recovered = vec![LineImage::ZERO; self.line_count];
        // 当前区间之前的区间都有屏障收尾，全部持久。
        for flushes in regions.iter().take(cut.region) {
            for f in flushes {
                recovered[f.line] = f.image;
            }
        }
        // 当前区间按同行前缀应用。
        let mut local_ord = vec![0usize; self.line_count];
        for f in &regions[cut.region] {
            local_ord[f.line] += 1;
            if local_ord[f.line] <= cut.chosen[f.line] {
                recovered[f.line] = f.image;
            }
        }

        self.volatile = recovered.clone();
        self.persistent = recovered;
        self.powered = false;
        // 重新上电：恢复后的镜像就是新的易失起点。
        self.powered = true;
    }

    /// 从一个观测到的失败行像集合，反推一种必然导致它的全局落盘顺序。
    ///
    /// - 每行的目标像必须能在该区间内该行的待持久队列（或区间基线）中找到；
    /// - 同行严格按发起次序取前缀，跨行用 `strategy` 仲裁先后；
    /// - 返回的顺序从零介质重放后必须逐行等于 `target`（内部已复验）。
    pub fn reconstruct(
        &self,
        region: usize,
        target: &[LineImage],
        strategy: OrderStrategy,
    ) -> Result<Reconstruction, ReconstructError> {
        assert_eq!(target.len(), self.line_count, "bad target width");
        let regions = Self::group_regions(&self.log);
        assert!(region < regions.len(), "region out of range");

        // 区间基线：此前所有区间都有屏障收尾。
        let mut durable = vec![LineImage::ZERO; self.line_count];
        for flushes in regions.iter().take(region) {
            for f in flushes {
                durable[f.line] = f.image;
            }
        }

        let mut chosen = vec![0usize; self.line_count];
        let mut min_barriers = 0usize;
        for line in 0..self.line_count {
            let queue: Vec<&FlushEvent> = regions[region]
                .iter()
                .filter(|f| f.line == line)
                .collect();
            // 优先在当前区间的待持久队列里找。取**最后**一个匹配：
            // 同一行可能刷出相同行像，合法切割只能是前缀，目标像对应的
            // 必然是队列里最后一次同值刷出（否则就跳过了中间的同值刷出）。
            if let Some(p) = queue.iter().rposition(|f| f.image == target[line]) {
                chosen[line] = p + 1; // 前缀长度
                min_barriers = min_barriers.max(region + 1);
            } else if target[line] == durable[line] {
                chosen[line] = 0;
            } else {
                let elsewhere = regions.iter().enumerate().find_map(|(r, fs)| {
                    fs.iter()
                        .find(|f| f.line == line && f.image == target[line])
                        .map(|_| r)
                });
                return Err(match elsewhere {
                    Some(found_region) => ReconstructError::OutsideRegion {
                        line,
                        given_region: region,
                        found_region,
                    },
                    None => ReconstructError::NoMatchingImage { line },
                });
            }
        }

        // 用行间仲裁策略生成一种全局顺序：
        // 先重放此前区间已被屏障保证持久的每行最后一个行像（基线），
        // 再按仲裁顺序重放当前区间的同行前缀——基线必须在前，
        // 否则会覆盖当前区间刚落盘的新行像。
        let line_order: Vec<usize> = {
            let mut order: Vec<usize> = (0..self.line_count).collect();
            if strategy == OrderStrategy::LineDescending {
                order.reverse();
            }
            order
        };
        let mut replay_order = Vec::new();
        for &line in &line_order {
            if durable[line] != LineImage::ZERO {
                replay_order.push((line, durable[line]));
            }
        }
        for &line in &line_order {
            let queue: Vec<&FlushEvent> = regions[region]
                .iter()
                .filter(|f| f.line == line)
                .take(chosen[line])
                .collect();
            for f in queue {
                replay_order.push((line, f.image));
            }
        }

        // 从零介质重放验证：得到的必须正是目标像。
        let check = Self::replay_order(&replay_order, self.line_count);
        assert_eq!(check, target, "reconstructed order does not reproduce target");

        Ok(Reconstruction {
            cut: CrashCut { region, chosen },
            replay_order,
            min_barriers,
        })
    }

    /// 重放一个落盘顺序（通常来自 [`Self::reconstruct`]），返回得到的行像集合。
    pub fn replay_order(order: &[(usize, LineImage)], line_count: usize) -> Vec<LineImage> {
        let mut out = vec![LineImage::ZERO; line_count];
        for &(line, image) in order {
            out[line] = image;
        }
        out
    }
}

/// 一个屏障区间内的合法断电像枚举。
#[derive(Clone, Debug)]
pub struct CrashRegion {
    /// 区间号（0 = 第一道屏障之前）。
    pub region: usize,
    /// 进入该区间时上一道屏障保证持久的行像。
    pub baseline: Vec<LineImage>,
    /// 每行在该区间内的待持久行像队列。
    pub pending: Vec<Vec<LineImage>>,
    /// 全部合法切割及其完整行像。
    pub images: Vec<CrashImage>,
}

/// 一次具体断电切割对应的完整介质行像。
#[derive(Clone, Debug)]
pub struct CrashImage {
    /// 切割描述（区间 + 每行前缀长度）。
    pub cut: CrashCut,
    /// 该切割下恢复后看到的全部行像。
    pub lines: Vec<LineImage>,
}
