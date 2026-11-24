//! 持久小对象堆：根指针、定长槽里的变长对象、空闲表，
//! 以及带代次句柄的对象替换与回收协议。
//!
//! # 内存布局（全部按 64 位字寻址，一个槽占一条缓存行）
//! - 第 0 行：根表，`ROOT_COUNT` 个根项，每个一项一个字，
//!   `0` 表示空根，否则编码 `(槽号, 代次)`。
//! - 第 1 行：空闲表，`SLOT_COUNT` 个空闲项，每项一个字，
//!   `0` 表示该表项空闲，否则编码一个空闲槽的 `(槽号, 代次)`。
//! - 第 2 行起：`SLOT_COUNT` 个槽，每槽一条缓存行（8 个字）。
//!   槽内第 0 字是头（状态 + 代次 + 有效载荷字数），其余 7 字为载荷，
//!   因此对象是 1..=7 个字的变长对象。
//!
//! # 协议不变量
//! - 每个多步协议阶段之间都有屏障；恢复后要么停在已提交旧根，
//!   要么停在完整新根，绝不会出现指向 PENDING 槽的持久根。
//! - 句柄携带代次；槽被释放并复用后代次递增，旧句柄必然校验失败。
//! - 恢复时独立扫描：根表 -> 槽头 -> 重建空闲表，
//!   可达槽与空闲槽互斥且并集为全部槽，无永久泄漏。

use crate::medium::{LineImage, PersistentMedia, LINE_WORDS};

/// 根表所在行。
pub const ROOT_LINE: usize = 0;
/// 空闲表所在行。
pub const FREE_TABLE_LINE: usize = 1;
/// 第一个槽所在行。
pub const FIRST_SLOT_LINE: usize = 2;
/// 根项数量（本布局下根表行可容纳 8 个，保留 4 个）。
pub const ROOT_COUNT: usize = 4;
/// 槽数量（也等于空闲表项数量，各占一整行）。
pub const SLOT_COUNT: usize = 6;
/// 每个槽的总字数（一条缓存行）。
pub const SLOT_WORDS: usize = LINE_WORDS;
/// 槽内最大有效载荷字数（第 0 字是头）。
pub const MAX_PAYLOAD_WORDS: usize = SLOT_WORDS - 1;
/// 介质需要的总行数。
pub const TOTAL_LINES: usize = FIRST_SLOT_LINE + SLOT_COUNT;

const STATE_FREE: u64 = 0;
const STATE_PENDING: u64 = 1;
const STATE_COMMITTED: u64 = 2;
const LOW32_MASK: u64 = 0xFFFF_FFFF; // 低 32 位放槽号，高 32 位放代次

/// 堆操作错误。
#[derive(Clone, PartialEq, Eq, Debug)]
pub enum HeapError {
    /// 载荷为空或超过单槽容量。
    BadPayloadLen { len: usize },
    /// 空闲表为空，没有可分配槽。
    NoSpace,
    /// 根下标越界。
    BadRoot { root: usize },
    /// 槽号越界，或句柄因槽复用/释放而失效。
    BadSlot { slot: usize },
}

impl core::fmt::Display for HeapError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            HeapError::BadPayloadLen { len } => {
                write!(f, "payload length {len} out of range 1..={MAX_PAYLOAD_WORDS}")
            }
            HeapError::NoSpace => write!(f, "no free slot available"),
            HeapError::BadRoot { root } => write!(f, "root index {root} out of range"),
            HeapError::BadSlot { slot } => write!(f, "slot {slot} invalid or handle generation stale"),
        }
    }
}

impl std::error::Error for HeapError {}

/// 读者持有的跨恢复句柄：槽号 + 代次。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Handle {
    pub slot: usize,
    pub generation: u32,
}

/// 一次读到的对象视图（拷贝出的载荷，避免借用介质）。
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct ObjectView {
    pub handle: Handle,
    pub payload: Vec<u64>,
}

/// 挂载/恢复报告。
#[derive(Clone, PartialEq, Eq, Debug, Default)]
pub struct RecoveryReport {
    /// 有效根数量。
    pub live_roots: usize,
    /// 从根可达的已提交槽数量。
    pub reachable_slots: usize,
    /// 被回滚的 PENDING 槽数量（途中替换的残骸）。
    pub rolled_back_pending: usize,
    /// 回收进空闲表的不可达已提交槽数量（泄漏/前次残骸）。
    pub reclaimed_committed: usize,
    /// 重建后的空闲槽数量。
    pub free_slots: usize,
}

/// 持久小对象堆。
pub struct PersistentHeap {
    media: PersistentMedia,
    /// 每个槽当前观察到的最大代次（恢复时从槽头重建）。
    generations: Vec<u32>,
}

// ---------- 字内编解码 ----------

fn pack_header(state: u64, generation: u32, len: usize) -> u64 {
    ((generation as u64) << 32) | (state << 8) | (len as u64)
}

fn header_state(word: u64) -> u64 {
    (word >> 8) & 0xFF
}

fn header_generation(word: u64) -> u32 {
    (word >> 32) as u32
}

fn header_len(word: u64) -> usize {
    (word & 0xFF) as usize
}

/// 根项/空闲项编码；存储值为 0 表示 NULL，因此槽号存 slot+1。
fn pack_handle(slot: usize, generation: u32) -> u64 {
    ((generation as u64) << 32) | ((slot as u64) + 1)
}

fn is_null(word: u64) -> bool {
    word & LOW32_MASK == 0
}

fn decode_handle(word: u64) -> Handle {
    Handle {
        slot: ((word & LOW32_MASK) as usize) - 1,
        generation: (word >> 32) as u32,
    }
}

fn slot_line(slot: usize) -> usize {
    FIRST_SLOT_LINE + slot
}

fn slot_word(slot: usize, within: usize) -> usize {
    slot_line(slot) * LINE_WORDS + within
}

fn root_word(root: usize) -> usize {
    ROOT_LINE * LINE_WORDS + root
}

fn free_word(index: usize) -> usize {
    FREE_TABLE_LINE * LINE_WORDS + index
}

impl PersistentHeap {
    /// 格式化一块冷介质并返回空堆（内容初始化也走显式刷出 + 屏障）。
    pub fn format(mut media: PersistentMedia) -> Self {
        assert_eq!(
            media.line_count(),
            TOTAL_LINES,
            "media must have exactly {TOTAL_LINES} lines for this heap layout"
        );

        // 根表保持全零；把每个槽头写成 FREE、代次 0、长度 0，
        // 同时把全部 FREE 槽登记进空闲表。
        for slot in 0..SLOT_COUNT {
            media.write_word(slot_word(slot, 0), pack_header(STATE_FREE, 0, 0));
            media.flush_line(slot_line(slot));
            media.write_word(free_word(slot), pack_handle(slot, 0));
        }
        media.flush_line(ROOT_LINE);
        media.flush_line(FREE_TABLE_LINE);
        media.barrier();

        Self {
            media,
            generations: vec![0u32; SLOT_COUNT],
        }
    }

    /// 挂载介质并执行崩溃恢复，返回恢复报告。
    pub fn mount(mut media: PersistentMedia) -> (Self, RecoveryReport) {
        assert_eq!(
            media.line_count(),
            TOTAL_LINES,
            "media must have exactly {TOTAL_LINES} lines for this heap layout"
        );

        let mut report = RecoveryReport::default();
        let mut generations = vec![0u32; SLOT_COUNT];
        let mut state = vec![STATE_FREE; SLOT_COUNT];

        // 1) 校验所有槽头并恢复代次。
        for slot in 0..SLOT_COUNT {
            let hdr = media.read_word(slot_word(slot, 0));
            let st = header_state(hdr);
            let len = header_len(hdr);
            assert!(
                st <= STATE_COMMITTED,
                "corrupt slot {slot}: unknown state {st}"
            );
            assert!(
                len <= MAX_PAYLOAD_WORDS,
                "corrupt slot {slot}: payload len {len} too large"
            );
            generations[slot] = header_generation(hdr);
            state[slot] = st;
            if st == STATE_PENDING {
                report.rolled_back_pending += 1;
            }
        }

        // 2) 校验根项：任何持久根都必须指向 COMMITTED 槽且代次匹配。
        let mut reachable = vec![false; SLOT_COUNT];
        for root in 0..ROOT_COUNT {
            let entry = media.read_word(root_word(root));
            if is_null(entry) {
                continue;
            }
            let h = decode_handle(entry);
            assert!(h.slot < SLOT_COUNT, "corrupt root {root}: bad slot");
            let hdr = media.read_word(slot_word(h.slot, 0));
            assert_eq!(
                header_state(hdr),
                STATE_COMMITTED,
                "corrupt root {root}: points at non-committed slot {} (in-flight replacement must never publish)",
                h.slot
            );
            assert_eq!(
                header_generation(hdr),
                h.generation,
                "corrupt root {root}: stale generation on slot {}",
                h.slot
            );
            if !reachable[h.slot] {
                reachable[h.slot] = true;
                report.reachable_slots += 1;
            }
            report.live_roots += 1;
        }

        // 3) 回滚所有 PENDING 槽（途中替换的残骸），回收不可达 COMMITTED 槽。
        for slot in 0..SLOT_COUNT {
            match state[slot] {
                STATE_PENDING => {
                    generations[slot] = generations[slot].wrapping_add(1);
                    media.write_word(
                        slot_word(slot, 0),
                        pack_header(STATE_FREE, generations[slot], 0),
                    );
                    media.flush_line(slot_line(slot));
                }
                STATE_COMMITTED if !reachable[slot] => {
                    report.reclaimed_committed += 1;
                    media.write_word(
                        slot_word(slot, 0),
                        pack_header(STATE_FREE, generations[slot], 0),
                    );
                    media.flush_line(slot_line(slot));
                }
                _ => {}
            }
        }

        // 4) 独立重建空闲表：恰为全部非可达槽。
        for index in 0..SLOT_COUNT {
            media.write_word(free_word(index), 0);
        }
        let mut free_count = 0usize;
        for slot in 0..SLOT_COUNT {
            if reachable[slot] {
                continue;
            }
            media.write_word(
                free_word(free_count),
                pack_handle(slot, generations[slot]),
            );
            free_count += 1;
        }
        report.free_slots = free_count;
        media.flush_line(FREE_TABLE_LINE);
        media.barrier();

        let heap = Self { media, generations };
        (heap, report)
    }

    /// 借用底层介质（测试需要枚举断电像与日志）。
    pub fn media(&self) -> &PersistentMedia {
        &self.media
    }

    /// 消耗堆并取回介质。
    pub fn into_media(self) -> PersistentMedia {
        self.media
    }

    fn check_payload(payload: &[u64]) -> Result<(), HeapError> {
        if payload.is_empty() || payload.len() > MAX_PAYLOAD_WORDS {
            return Err(HeapError::BadPayloadLen {
                len: payload.len(),
            });
        }
        Ok(())
    }

    /// 读取根项；空根返回 None。
    pub fn root_handle(&self, root: usize) -> Result<Option<Handle>, HeapError> {
        if root >= ROOT_COUNT {
            return Err(HeapError::BadRoot { root });
        }
        let entry = self.media.read_word(root_word(root));
        if is_null(entry) {
            Ok(None)
        } else {
            Ok(Some(decode_handle(entry)))
        }
    }

    /// 按句柄读对象。槽已复用（代次不符）、已释放或仍在途中都会报错。
    pub fn read(&self, handle: Handle) -> Result<ObjectView, HeapError> {
        if handle.slot >= SLOT_COUNT {
            return Err(HeapError::BadSlot {
                slot: handle.slot,
            });
        }
        let hdr = self.media.read_word(slot_word(handle.slot, 0));
        if header_state(hdr) != STATE_COMMITTED || header_generation(hdr) != handle.generation {
            // 旧句柄：对象已被释放，槽可能已被复用为更新代次的对象。
            return Err(HeapError::BadSlot {
                slot: handle.slot,
            });
        }
        let len = header_len(hdr);
        let mut payload = Vec::with_capacity(len);
        for i in 0..len {
            payload.push(self.media.read_word(slot_word(handle.slot, 1 + i)));
        }
        Ok(ObjectView { handle, payload })
    }

    /// 从空闲表取一个槽（first-fit），必要时递增代次并写 PENDING 头。
    fn allocate(&mut self, payload: &[u64]) -> Result<usize, HeapError> {
        Self::check_payload(payload)?;
        let mut chosen: Option<(usize, Handle)> = None;
        for index in 0..SLOT_COUNT {
            let entry = self.media.read_word(free_word(index));
            if !is_null(entry) {
                chosen = Some((index, decode_handle(entry)));
                break;
            }
        }
        let (free_index, free_h) = chosen.ok_or(HeapError::NoSpace)?;

        let slot = free_h.slot;
        // 空闲表项里的代次与槽头代次一致；分配即开启新一代。
        debug_assert_eq!(free_h.generation, self.generations[slot]);
        self.generations[slot] = self.generations[slot].wrapping_add(1);

        // 从空闲表摘除并压缩（把后面的有效项整体前移），避免表内空洞
        // 导致后续 first-fit 扫描漏项；先持久化“槽已不在空闲表”。
        for index in free_index..SLOT_COUNT - 1 {
            let next = self.media.read_word(free_word(index + 1));
            self.media.write_word(free_word(index), next);
        }
        self.media.write_word(free_word(SLOT_COUNT - 1), 0);
        self.media.flush_line(FREE_TABLE_LINE);
        self.media.barrier();

        // 写 PENDING 头与载荷（对象此刻对任何根都不可达）。
        self.media.write_word(
            slot_word(slot, 0),
            pack_header(STATE_PENDING, self.generations[slot], payload.len()),
        );
        for (i, value) in payload.iter().enumerate() {
            self.media.write_word(slot_word(slot, 1 + i), *value);
        }
        // 头与载荷同在一条缓存行，一次刷出得到完整行像。
        self.media.flush_line(slot_line(slot));
        self.media.barrier();
        Ok(slot)
    }

    /// 把槽从 PENDING 提升为 COMMITTED（载荷已在上一阶段完整持久）。
    fn commit_slot(&mut self, slot: usize) {
        let hdr = self.media.read_word(slot_word(slot, 0));
        assert_eq!(
            header_state(hdr),
            STATE_PENDING,
            "slot {slot} must be pending"
        );
        let gen = header_generation(hdr);
        let len = header_len(hdr);
        self.media
            .write_word(slot_word(slot, 0), pack_header(STATE_COMMITTED, gen, len));
        self.media.flush_line(slot_line(slot));
        self.media.barrier();
    }

    /// 发布根指针（字原子写 + 刷出 + 屏障）。
    fn publish_root(&mut self, root: usize, handle: Handle) {
        self.media
            .write_word(root_word(root), pack_handle(handle.slot, handle.generation));
        self.media.flush_line(ROOT_LINE);
        self.media.barrier();
    }

    /// 回收当前不被任何根引用的已提交槽。
    fn reclaim(&mut self) {
        let mut referenced = vec![false; SLOT_COUNT];
        for root in 0..ROOT_COUNT {
            if let Some(h) = self.root_handle(root).expect("root index is valid") {
                referenced[h.slot] = true;
            }
        }
        for slot in 0..SLOT_COUNT {
            if referenced[slot] {
                continue;
            }
            let hdr = self.media.read_word(slot_word(slot, 0));
            if header_state(hdr) != STATE_COMMITTED {
                continue;
            }
            // 先把槽标记为 FREE（代次保留并递增），再摘出引用方视野。
            self.generations[slot] = self.generations[slot].wrapping_add(1);
            self.media.write_word(
                slot_word(slot, 0),
                pack_header(STATE_FREE, self.generations[slot], 0),
            );
            self.media.flush_line(slot_line(slot));
        }
        self.media.barrier();

        // 重建空闲表：所有 FREE 槽入表，互斥且覆盖全部非可达槽。
        for index in 0..SLOT_COUNT {
            self.media.write_word(free_word(index), 0);
        }
        let mut free_count = 0usize;
        for slot in 0..SLOT_COUNT {
            let hdr = self.media.read_word(slot_word(slot, 0));
            if header_state(hdr) == STATE_FREE {
                self.media.write_word(
                    free_word(free_count),
                    pack_handle(slot, self.generations[slot]),
                );
                free_count += 1;
            }
        }
        self.media.flush_line(FREE_TABLE_LINE);
        self.media.barrier();
    }

    /// 原子替换某个根的对象。
    ///
    /// 阶段（每阶段之间都有屏障）：
    /// 1. 分配新槽并写入完整 PENDING 对象；
    /// 2. 新槽提升为 COMMITTED（完整新对象已持久）；
    /// 3. 字原子发布新根并屏障——成功点，此后整个新对象可见；
    /// 4. 回收旧槽（旧句柄代次失效，槽可被后续分配复用）。
    pub fn replace(&mut self, root: usize, payload: &[u64]) -> Result<Handle, HeapError> {
        if root >= ROOT_COUNT {
            return Err(HeapError::BadRoot { root });
        }
        let old = self.root_handle(root)?;
        let new_slot = self.allocate(payload)?;
        let new_handle = Handle {
            slot: new_slot,
            generation: self.generations[new_slot],
        };
        self.commit_slot(new_slot);
        self.publish_root(root, new_handle);
        let _ = old;
        self.reclaim();
        Ok(new_handle)
    }

    /// 在一个空根上发布新对象（替换的“初次分配”形态）。
    pub fn publish(&mut self, root: usize, payload: &[u64]) -> Result<Handle, HeapError> {
        if root >= ROOT_COUNT {
            return Err(HeapError::BadRoot { root });
        }
        assert!(
            self.root_handle(root)?.is_none(),
            "root {root} already occupied; use replace"
        );
        let slot = self.allocate(payload)?;
        let handle = Handle {
            slot,
            generation: self.generations[slot],
        };
        self.commit_slot(slot);
        self.publish_root(root, handle);
        Ok(handle)
    }

    /// 撤销根：清掉根项（屏障保证持久）后立即回收，
    /// 槽回到空闲表并可被下一次分配立刻复用。
    pub fn drop_root(&mut self, root: usize) -> Result<(), HeapError> {
        if root >= ROOT_COUNT {
            return Err(HeapError::BadRoot { root });
        }
        self.media.write_word(root_word(root), 0);
        self.media.flush_line(ROOT_LINE);
        self.media.barrier();
        self.reclaim();
        Ok(())
    }

    /// 调试/测试用：当前空闲表中的句柄（槽号 + 代次）。
    pub fn free_table(&self) -> Vec<Handle> {
        let mut out = Vec::new();
        for index in 0..SLOT_COUNT {
            let entry = self.media.read_word(free_word(index));
            if !is_null(entry) {
                out.push(decode_handle(entry))
            }
        }
        out
    }

    /// 调试/测试用：槽头状态（0=FREE, 1=PENDING, 2=COMMITTED）。
    pub fn slot_state(&self, slot: usize) -> u64 {
        header_state(self.media.read_word(slot_word(slot, 0)))
    }

    /// 独立遍历审计：恢复后可达集合与空闲集合必须互斥且并集为全部槽。
    pub fn audit(&self) -> Audit {
        let mut reachable = vec![false; SLOT_COUNT];
        let mut roots = Vec::new();
        for root in 0..ROOT_COUNT {
            let entry = self.media.read_word(root_word(root));
            if !is_null(entry) {
                let h = decode_handle(entry);
                roots.push(h);
                reachable[h.slot] = true;
            }
        }
        let mut free = vec![false; SLOT_COUNT];
        for index in 0..SLOT_COUNT {
            let entry = self.media.read_word(free_word(index));
            if !is_null(entry) {
                let h = decode_handle(entry);
                free[h.slot] = true;
            }
        }
        let disjoint = reachable.iter().zip(&free).all(|(a, b)| !(a & b));
        let covers_all_slots = reachable
            .iter()
            .zip(&free)
            .all(|(a, b)| *a || *b);
        // 每个可达槽必须确实是 COMMITTED 且代次与根一致。
        let headers_ok = roots.iter().all(|h| {
            let hdr = self.media.read_word(slot_word(h.slot, 0));
            header_state(hdr) == STATE_COMMITTED && header_generation(hdr) == h.generation
        });
        Audit {
            reachable,
            free,
            disjoint,
            covers_all_slots,
            headers_ok,
        }
    }

    /// 测试用：直接读取某条缓存行的易失镜像。
    pub fn line_image(&self, line: usize) -> LineImage {
        self.media.line_image(line)
    }
}

/// [`PersistentHeap::audit`] 的结果。
#[derive(Clone, Debug)]
pub struct Audit {
    pub reachable: Vec<bool>,
    pub free: Vec<bool>,
    pub disjoint: bool,
    pub covers_all_slots: bool,
    pub headers_ok: bool,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::medium::PersistentMedia;

    fn heap() -> PersistentHeap {
        PersistentHeap::format(PersistentMedia::cold_boot(TOTAL_LINES))
    }

    #[test]
    fn allocate_reports_no_space_when_free_table_empty() {
        let mut heap = heap();
        // 白盒：把空闲表整体清零，模拟所有槽都不可分配。
        for index in 0..SLOT_COUNT {
            heap.media.write_word(free_word(index), 0);
        }
        assert_eq!(
            heap.allocate(&[1, 2, 3]).unwrap_err(),
            HeapError::NoSpace
        );
        // 失败后没有任何槽头被改动。
        for slot in 0..SLOT_COUNT {
            assert_eq!(heap.slot_state(slot), STATE_FREE);
        }
    }

    #[test]
    fn generation_increments_monotonically_across_alloc_reclaim_cycles() {
        let mut heap = heap();
        let h1 = heap.publish(0, &[1]).unwrap();
        assert_eq!(h1.generation, 1);

        heap.drop_root(0).unwrap();
        let h2 = heap.publish(0, &[2]).unwrap();
        assert_eq!(h2.slot, h1.slot);
        // 分配 +1，撤根回收再 +1，再次分配又 +1。
        assert_eq!(h2.generation, 3);

        heap.drop_root(0).unwrap();
        let h3 = heap.publish(0, &[3]).unwrap();
        assert_eq!(h3.generation, 5);
        // 所有旧句柄都因代次落后而失效。
        for stale in [h1, h2] {
            assert_eq!(
                heap.read(stale),
                Err(HeapError::BadSlot {
                    slot: stale.slot
                })
            );
        }
    }

    #[test]
    fn bad_root_and_bad_payload_are_rejected_before_touching_slots() {
        let mut heap = heap();
        assert!(matches!(
            heap.publish(ROOT_COUNT, &[1]),
            Err(HeapError::BadRoot { .. })
        ));
        assert!(matches!(
            heap.replace(0, &[]),
            Err(HeapError::BadPayloadLen { len: 0 })
        ));
        let too_big = vec![0u64; MAX_PAYLOAD_WORDS + 1];
        assert!(matches!(
            heap.publish(0, &too_big),
            Err(HeapError::BadPayloadLen { .. })
        ));
        // 全部槽仍空闲。
        assert_eq!(heap.free_table().len(), SLOT_COUNT);
    }
}
